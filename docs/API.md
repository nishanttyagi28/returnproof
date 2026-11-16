# ReturnProof API

Base URL: `http://localhost:8000/api/v1`

Interactive docs: `/docs` (grouped by tag) and `/redoc`. The OpenAPI schema
marks operations with `orders`, `returns`, `evidence`, `verification`, `claims`,
`recovery`, `exports`, `jobs` and `api-keys`.

## Authentication

API endpoints require an organisation API key:

```http
Authorization: Bearer rp_<token>
```

`X-API-Key: rp_<token>` is also accepted. Tokens are shown once and stored as
SHA-256; only the first 12 characters are kept as a lookup prefix. The key's
effective role is the role of its creator (owner/ops). Default deny: a missing,
invalid or revoked key gets `401`.

Create a key on a seeded local database:

```bash
python -m returnproof.apikey_cli \
  --email owner@nirvaanretail.example \
  --name local-dev
```

The same operation is available at `POST /api/v1/api-keys` for an existing
owner API key. Key creation by a viewer key returns `403`.

### Error shape

```json
{
  "detail": "Order not found",
  "request_id": "8c4f1f4f7d8a4e67a0f6d55d61a1f719"
}
```

Status codes: `401` missing/invalid key, `403` role denied, `404` not found or
cross-tenant resource, `409` invalid lifecycle/duplicate import or ineligible
claim, `413` upload too large, `422` validation/CSV/file error, `429` login rate
limited (web), `500` unexpected error. `X-Request-ID` is always returned.

## System

| Method | Path | Description |
|---|---|---|
| GET | `/healthz` | Liveness JSON (outside `/api/v1`) |

## Principal and keys

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/api/v1/me` | any key | Organisation, role, auth method |
| POST | `/api/v1/api-keys` | owner | Create a key; plaintext returned once |

```bash
curl -H "Authorization: Bearer $RP_KEY" \
  http://localhost:8000/api/v1/me
```

## Orders

| Method | Path | Description |
|---|---|---|
| GET | `/orders` | List orders; query `status`, `marketplace_id`, `search`, `limit` (1–200), `offset` |
| GET | `/orders/{order_id}` | Order with items and returns |

```bash
curl -H "Authorization: Bearer $RP_KEY" \
  "http://localhost:8000/api/v1/orders?status=returned&limit=20"
```

Money fields end in `_paise` (integer). Dates are ISO-8601 UTC.

## Returns

| Method | Path | Description |
|---|---|---|
| GET | `/returns` | List returns; query `status`, `limit`, `offset` |
| GET | `/returns/{return_id}` | One return with verification result |

Verification is included as `{status, score, reasons, image_distance}` when it
exists.

## Evidence

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/orders/{order_id}/evidence` | any key | Evidence ordered by chain index |
| POST | `/orders/{order_id}/evidence` | owner/ops | Multipart upload to the hash chain |
| GET | `/orders/{order_id}/evidence/check` | any key | Recompute chain and files |

Multipart fields:

| Field | Required | Notes |
|---|---|---|
| `file` | yes | Max `RETURNPROOF_MAX_UPLOAD_MB` (50 MB default) |
| `stage` | yes | `dispatch` or `return_receipt` |
| `kind` | yes | `photo`, `video`, `document` |
| `return_request_id` | for receipt | Must belong to the order |
| `device_note` | no | Up to 240 chars |

Allowed MIME types: photos (`image/jpeg`, `image/png`, `image/webp`), video
(`video/mp4`, `video/quicktime`, `video/webm`), documents (`application/pdf`,
`text/csv`, `text/plain`, `application/json`). Photos are decoded by Pillow and
receive width/height/dHash metadata.

```bash
curl -X POST \
  -H "Authorization: Bearer $RP_KEY" \
  -F "file=@packing.jpg;type=image/jpeg" \
  -F "stage=dispatch" \
  -F "kind=photo" \
  -F "device_note=Bay 2 camera" \
  http://localhost:8000/api/v1/orders/12/evidence
```

Tamper-check response:

```json
{
  "order_id": 12,
  "evidence_count": 2,
  "valid": false,
  "head_hash": "…",
  "checked_at": "2026-11-16T10:00:00Z",
  "problems": [
    {"evidence_id": 5, "code": "FILE_HASH_MISMATCH",
     "message": "Stored file no longer matches its SHA-256"}
  ]
}
```

Problem codes: `FILE_MISSING`, `FILE_HASH_MISMATCH`, `FILE_SIZE_MISMATCH`,
`CHAIN_LINK_BROKEN`, `CHAIN_INDEX_GAP`, `CHAIN_HASH_MISMATCH`.

## Verification

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/returns/{return_id}/verification` | owner/ops | Verify typed identifiers + checklist |
| POST | `/verifications/import` | owner/ops | Verify many returns from CSV |
| GET | `/verification-statuses` | any key | Status vocabulary |

Typed body (all fields optional, but missing expected identifiers produce
`NEEDS_REVIEW`):

```json
{
  "serial_number": "SN-DISPATCH-001",
  "imei": "869000000000001",
  "barcode": "8901234567890",
  "batch_code": "BATCH-NOV-1",
  "seal_intact": true,
  "accessories_present": true,
  "tags_present": true,
  "weight_grams": 208
}
```

Result:

```json
{
  "id": 7,
  "return_request_id": 3,
  "status": "match",
  "score": 0.94,
  "reasons": ["All captured identifier, visual and checklist checks agree."],
  "checklist": {"weight_ok": true, "weight_variance_grams": 2},
  "identifier_checks": {"serial_number": {"status": "match", "...": "..."}},
  "returned_identifiers": {"serial_number": "SN-DISPATCH-001"},
  "image_distance": 0,
  "dispatch_evidence_id": 4,
  "return_evidence_id": 5,
  "created_at": "2026-11-16T10:00:00Z",
  "updated_at": "2026-11-16T10:00:00Z"
}
```

CSV verification headers (case-insensitive aliases in brackets):
`rma_number` [`rma`] or `order_number` [`order`], `serial_number` [`serial`],
`imei`, `barcode`, `batch_code` [`batch`], `weight_grams` [`weight`],
`seal_intact` [`seal`], `accessories_present` [`accessories`],
`tags_present` [`tags`]. Booleans accept `yes/no`, `true/false`, `1/0`,
`present/missing`. Response returns processed results and per-row errors.

## Claims

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/claim-templates` | any key | Data-driven template registry |
| POST | `/returns/{return_id}/claims` | owner/ops | Create claim + HTML/PDF pack |
| GET | `/claims` | any key | List claims; optional `status` |
| GET | `/claims/{claim_id}` | any key | One claim |
| POST | `/claims/{claim_id}/file` | owner/ops | `DRAFT → FILED`, set deadline |
| POST | `/claims/{claim_id}/resolve` | owner/ops | `FILED → APPROVED/REJECTED/PARTIAL` |
| GET | `/claims/{claim_id}/pack?format=html\|pdf` | any key | View/download pack |

Create body:

```json
{
  "template_code": "amazon-safe-t",
  "amount_claimed_paise": 249900,
  "notes": "Swapped unit, serial mismatch"
}
```

`template_code` defaults to the marketplace's configured template. Eligibility:
verification `mismatch`/`needs_review`, or a damage-related return reason.
Resolve body:

```json
{"status": "partial", "amount_approved_paise": 100000}
```

Partial must be `> 0` and `< claimed`. Approved without an amount approves the
full claimed amount. Rejected approves zero. Only filed claims can resolve.

## Recovery

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/settlements/import` | owner/ops | Multipart settlement CSV import + reconcile |
| GET | `/settlements` | any key | Imported settlements |
| GET | `/settlements/{id}` | any key | Settlement + lines |
| POST | `/settlements/{id}/reconcile` | owner/ops | Re-run leak detection |
| GET | `/recovery/entries` | any key | Ledger; filter `direction`, `status`, `category`, `limit` |
| POST | `/recovery/reconcile` | owner/ops | Reconcile approved claims vs payouts |
| POST | `/recovery/entries/{id}/recover` | owner/ops | Mark an open entry recovered |
| GET | `/recovery/summary` | any key | At-risk vs recovered by marketplace/month |

Import form fields: `marketplace_id`, `file`, optional `period_start` and
`period_end` (`YYYY-MM-DD`). Header aliases and detection rules are in
[SETTLEMENT_FORMATS.md](SETTLEMENT_FORMATS.md). Duplicate file hashes return
`409`; re-reconciliation is idempotent.

Summary response:

```json
{
  "rows": [
    {
      "marketplace_id": 1,
      "marketplace": "Amazon.in",
      "month": "2026-11",
      "at_risk_paise": 249900,
      "recovered_paise": 100000,
      "open_entries": 3,
      "recovered_entries": 1
    }
  ],
  "total_at_risk_paise": 249900,
  "total_recovered_paise": 100000
}
```

## Exports

| Method | Path | Description |
|---|---|---|
| GET | `/exports/{dataset}.csv` | `orders`, `returns`, `claims`, `recovery`, `audit` |

Money columns are integer paise; dates are ISO-8601 UTC. Text cells beginning
with `=`, `+`, `-` or `@` are prefixed with `'` to prevent spreadsheet formula
injection. The UI equivalent is `/app/exports/{dataset}.csv`.

## Background jobs

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/jobs/settlement-import` | owner/ops | Queue multipart settlement import (202) |
| POST | `/jobs/claim-pack/{claim_id}` | owner/ops | Regenerate pack (202) |
| GET | `/jobs/{job_id}` | any key | Status/result/error |
| GET | `/jobs` | any key | Recent jobs for the organisation |

Job statuses: `queued`, `running`, `succeeded`, `failed`. Poll
`GET /jobs/{id}` until terminal. The runner is in-process; its interface is
designed to be replaced by an external queue. Jobs create their own database
session and are tenant-scoped on read.

## Roles

| Role | Read | Evidence/verify/claim/import | API keys |
|---|---|---|---|
| `viewer` | yes | no | no |
| `ops` | yes | yes | no |
| `owner` | yes | yes | yes |

Web login uses session cookies and CSRF. Login attempts are rate-limited per IP
and return `429` with `Retry-After` when exceeded.
