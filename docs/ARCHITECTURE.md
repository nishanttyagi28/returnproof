# ReturnProof architecture

## Goals

ReturnProof is built around three guarantees:

1. **Tenant isolation.** A request authenticated for organisation A can never
   read or mutate organisation B data, including generated pack files.
2. **Evidence integrity.** Every evidence file is hashed and chained, and the
   chain can be recomputed from disk at any time.
3. **Money traceability.** Every detected loss and every payout is a ledger
   entry, grouped by marketplace and month, with a claim link where relevant.

The stack is deliberately small: Python 3.12, FastAPI, SQLAlchemy 2, SQLite
(Postgres-ready), Pydantic v2, Jinja2, ReportLab and pytest. The UI is
server-rendered with shadeless forms and HTMX-compatible markup; there is no
Node build.

## Runtime composition

```mermaid
flowchart TB
    subgraph HTTP
      Browser[Browser<br/>session cookie + CSRF]
      Client[API client<br/>Bearer API key]
    end
    subgraph FastAPI
      Web[auth_routes · orders/returns · evidence · claims · settlements · recovery · audit]
      API[api/routes · claim_routes · recovery_routes · export_routes · job_routes]
      MW[Request ID + JSON access log]
      EH[HTML/JSON error handlers]
    end
    subgraph Domain services
      AUTH[auth.py<br/>argon2 · sessions · API keys]
      EV[evidence.py<br/>SHA-256 + chain]
      VER[verification.py<br/>identifiers · dHash · checklist]
      CL[claims.py<br/>templates · HTML/PDF · lifecycle]
      ST[settlements.py<br/>CSV · leakage · ledger]
      EX[exports.py]
      AU[audit.py]
      JOBS[jobs.py<br/>in-process queue]
    end
    DB[(SQLAlchemy<br/>SQLite/Postgres)]
    DISK[(Storage protocol<br/>local disk now)]
    Browser --> MW --> Web
    Client --> MW --> API
    MW --> EH
    Web --> AUTH
    Web --> Domain services
    API --> AUTH
    API --> Domain services
    AUTH --> DB
    Domain services --> DB
    EV --> DISK
    CL --> DISK
    JOBS --> DB
    JOBS --> DISK
    AU --> DB
```

## Data model

All timestamps are UTC (`DateTime(timezone=True)`); the Jinja layer converts to
IST. Money columns end in `_paise` and are integers.

| Area | Tables |
|---|---|
| Tenancy/auth | `organisations`, `users`, `api_keys` |
| Catalogue | `marketplaces`, `products` |
| Fulfilment | `orders`, `order_items`, `shipments` |
| Returns | `return_requests`, `evidence`, `verification_results` |
| Recovery | `claims`, `settlements`, `settlement_lines`, `recovery_entries` |
| Ops | `audit_events`, `alembic_version` |

Every business table carries `org_id`. `ApiKey` stores only SHA-256 of the
token; the plaintext token is returned once. Sessions are signed cookies with
`uid`, `org`, `role`, `auth_at` and a CSRF token; the user is re-read from the
database on every request and is invalidated if the role, org or active flag no
longer matches.

## Evidence chain

For each order, evidence lives in a single append-only sequence:

```text
chain_index=0  previous_hash=GENESIS (64 zeros)
               chain_hash = SHA256(canonical_json(metadata + sha256(file)))
chain_index=1  previous_hash=chain_hash(0)
               ...
```

The canonical payload includes version, order, index, stage, kind, filename,
content type, size, file SHA-256, previous hash, captured timestamp, uploader
label and device note. `GET /api/v1/orders/{id}/evidence/check` recomputes:

- presence and SHA-256/size of each file on disk,
- `previous_hash` links and sequential indexes,
- `chain_hash` from metadata.

Problems are returned as `FILE_MISSING`, `FILE_HASH_MISMATCH`,
`FILE_SIZE_MISMATCH`, `CHAIN_LINK_BROKEN`, `CHAIN_INDEX_GAP` or
`CHAIN_HASH_MISMATCH`. This detects accidental or direct database/disk edits.
It is not remote notarisation; anchor the head hash externally if that threat
matters.

Storage sits behind `Storage`/`LocalDiskStorage` with save/read/exists/delete/
local-path methods. The evidence service never opens paths directly, so S3 can
replace local disk without changing routes or tests. Keys are generated
server-side under `org_<id>/order_<id>/<uuid><suffix>` and resolved against the
storage root to prevent traversal.

## Product-identity verification

`verify_return` combines three signals:

1. **Identifiers** — serial number, IMEI, barcode, batch code. Expected values
   come from `order_items`; returned values come from typed API/UI input or CSV.
   Comparison normalises whitespace, hyphens and case. Any mismatch is a hard
   `MISMATCH`. A missing expected/submitted value yields `NEEDS_REVIEW`.
2. **Images** — latest dispatch photo and latest return-receipt photo for the
   return. dHash distance is compared with `RETURNPROOF_PHASH_THRESHOLD`
   (default 8). Distance above threshold is `MISMATCH`; missing images are
   `NEEDS_REVIEW`.
3. **Checklist** — seal, accessories, tags and returned weight against the
   expected item weight with gram + percent tolerance. Failed physical checks
   are `MISMATCH`; missing checks are `NEEDS_REVIEW`.

The score is a weighted average of available components (identifiers 45%, image
35%, checklist 20%), rounded to four decimals. Results are upserted per return.
The reasons list explains every failed or missing check and is embedded in claim
packs.

CSV verification rows accept `rma_number`/`order_number`, identifier columns,
booleans (`yes/no`, `true/false`, `1/0`) and `weight_grams`. Unknown RMAs are
reported per row without aborting the batch.

## Claim packs and lifecycle

Claim templates are **data**, not code: `returnproof/data/claim_templates.json`
defines names, titles, accents, section order, labels, declarations, submission
notes and legal notes. The standalone `claim_pack.html` template renders one
pack using those fields; ReportLab builds the PDF from the same context, with
embedded evidence thumbnails and hash tables.

Packs are written under `pack_dir/org_<id>/<reference>.html|.pdf`. `Claim.pack_path`
stores the HTML path relative to the pack root; the PDF is derived. API and UI
downloads re-check tenancy before resolving a file.

Lifecycle:

```text
DRAFT --file--> FILED --resolve--> APPROVED | REJECTED | PARTIAL
```

Filing sets `filed_at` and `deadline_at = filed_at + marketplace.claim_window_days`.
Resolution records `amount_approved_paise` and emits a recovery ledger entry
(`approved_not_paid`, at risk) for positive approvals; rejected claims emit no
payout. Only filed claims can resolve, and partial approvals must be between
zero and the claimed amount.

Eligibility requires a `MISMATCH`/`NEEDS_REVIEW` verification or a damage-related
return reason (`damaged`, `empty_box`, `wrong_item`, `not_as_described`,
`missing_accessories`).

## Settlement reconciliation

CSV import accepts the canonical headers documented in
`docs/SETTLEMENT_FORMATS.md`, plus Amazon/Flipkart-style aliases. Header matching
is case-insensitive and punctuation-insensitive. Amounts are parsed from INR
decimals, commas, currency symbols and parenthesised negatives into paise. The
file SHA-256 prevents duplicate imports per organisation.

Every import creates a `settlement` and `settlement_lines`, then runs
reconciliation:

| Rule | Condition | Entry |
|---|---|---|
| Refund without return | refund line and no received return | `refund_without_return`, at risk |
| Reverse shipping error | reverse fee with no return, unreceived return, or seller-protected reason | `reverse_shipping_error`, at risk |
| Commission mismatch | commission above configured rate + tolerance | `commission_mismatch`, at risk |
| Fee mismatch | net below `gross - fees - refund` + tolerance | `fee_mismatch`, at risk |
| Approved not paid | approved/partial claim with no enough `claim_payout` rows | `approved_not_paid`, at risk |
| Payout found | payouts cover the approved amount | existing claim entry marked recovered |

Line-level detection is idempotent by `(settlement_line_id, category)`.
Approved-claim detection is idempotent by `(claim_id, category)`. This makes
re-running reconciliation safe.

Ledger entries are either `AT_RISK/OPEN` or `RECOVERED/RECOVERED`; the latter
stores `recovered_at`. The summary groups open at-risk entries by detection month
and recovered entries by recovery month, per marketplace, with totals.

## API and UI split

- `/api/v1/*` is API-key authenticated, tenant-scoped and versioned. Routes are
  grouped in OpenAPI by tags: orders, returns, evidence, verification, claims,
  recovery, exports, jobs, api-keys.
- `/app/*` is session/CSRF authenticated and server-rendered. Viewers can read;
  owners and ops can mutate. Logout is POST-only.
- `/app/exports/{dataset}.csv` and `/api/v1/exports/{dataset}.csv` share the
  exporter. Cells starting with `=`, `+`, `-` or `@` are prefixed with `'` to
  neutralise spreadsheet formulas.
- `/healthz` is unauthenticated liveness. `/docs` and `/redoc` expose OpenAPI.

## Operations

- **Audit** — `audit_and_commit` writes append-only events for login/logout,
  evidence upload, verification, claim create/file/resolve, settlement import,
  recovery reconciliation/recovery, and API-key creation.
- **Logging** — every request gets `X-Request-ID`; `returnproof.access` emits one
  JSON line per request with method, path, status and duration. Exceptions are
  logged with the request id. Burst-free dependencies are not logged.
- **Rate limiting** — a per-process sliding window limits login attempts per IP
  (`RETURNPROOF_RATE_LIMIT_AUTH_PER_MINUTE`, default 10/min) and returns 429
  with `Retry-After`. Use a shared store when running multiple workers.
- **Jobs** — `InProcessJobRunner` wraps a thread pool and exposes job ids,
  status, result and error. Settlement import and claim-pack regeneration have
  queue endpoints. Job tasks open their own engine/session. Replace the runner
  with Celery/RQ behind the same HTTP contract for multi-process deployments.
- **Errors** — browser requests get a styled page with status, message and
  request id; `/api/*` returns JSON `{"detail", "request_id"}`. Unhandled 500s
  never expose internals.

## Deployment notes

- Set a strong `RETURNPROOF_SECRET_KEY`, `RETURNPROOF_ENVIRONMENT=production`
  and HTTPS; session cookies become `Secure` in production.
- Run `alembic upgrade head` before starting the app.
- Postgres: set `DATABASE_URL` to a `postgresql+psycopg://...` URL. The models
  avoid SQLite-specific types.
- Local evidence/pack directories must be persistent and backed up. For
  multi-instance deployments, implement the `Storage` protocol on S3 and move
  pack files to object storage too.
- The in-process job runner and rate limiter are per-worker. Use a shared queue
  and shared limiter (Redis) when scaling horizontally.

## Test strategy

Tests use FastAPI's `TestClient`, a temporary SQLite file per test, generated
PNGs and synthetic CSV files. They cover tenant isolation for pages, API keys
and files; evidence chain tamper cases; deterministic verification outcomes;
claim HTML/PDF generation and lifecycle; settlement leakage detection and
idempotency; audit/export/job/rate-limit/error behaviour. No test performs
network I/O.
