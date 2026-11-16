# ReturnProof roadmap

## Shipped (R-001 … R-010)

- FastAPI scaffold, settings, health, CI on Ubuntu/Windows, MIT license.
- Domain model for sellers, users, marketplaces, products, orders, shipments,
  returns, evidence, verification, claims, settlements and recovery ledger.
- Argon2 login, signed sessions, owner/ops/viewer roles, API keys, tenant
  isolation tests.
- Evidence vault with SHA-256, per-order append-only hash chain, local storage
  behind an S3-ready interface, tamper-check endpoint and UI.
- Product identity verification: identifiers, CSV import, dHash image distance,
  seal/accessories/tags/weight checklist, deterministic generated-image tests.
- Data-driven claim templates, standalone HTML + ReportLab PDF packs, claim
  lifecycle with deadlines, UI preview/download.
- Settlement CSV import with Amazon/Flipkart-style aliases, seven leakage
  detections, idempotent reconciliation, recovery ledger and monthly summary.
- Seller UI: dashboard, orders, returns, return detail, evidence gallery,
  verification form, claim creation/lifecycle, settlement import, recovery
  report, audit log, responsive layout and IST display.
- Ops: audit events, formula-safe CSV exports, in-process jobs, login rate
  limiting, structured JSON logs, request IDs, styled HTML/JSON errors, tagged
  OpenAPI.
- Docs, synthetic demo workspace, coverage gate at 85%.

## Next (0.2)

1. **Production storage** — S3-compatible `Storage` implementation, presigned
   downloads, lifecycle rules, immutable object lock.
2. **Postgres + migrations** — run the suite against Postgres in CI, add
   connection pooling guidance and backup/restore playbook.
3. **External job queue** — Celery/RQ/Arq adapter behind the existing job
   contract; per-job retries and dead-letter handling.
4. **Shared rate limiting** — Redis-backed limiter for multi-worker auth and API
   key abuse protection.
5. **Capture UX** — mobile-friendly camera capture flow, barcode scanning,
   automatic image quality checks, batch evidence upload.
6. **Notification loop** — email/webhook alerts for claims nearing deadline,
   newly detected leakage and settlement import completion.

## Later (0.3+)

- **OCR-assisted identifiers** — read serial/IMEI/barcode from photos with an
  optional self-hosted model; keep manual/typed paths authoritative.
- **Marketplace adapters (read-only exports)** — import order/return exports in
  addition to settlements; no scraping, official credentials only.
- **Courier events** — webhook/CSV ingestion for AWB scans and RTO outcomes.
- **External chain anchoring** — periodically publish evidence head hashes to a
  WORM store or timestamping authority for stronger tamper evidence.
- **Advanced analytics** — buyer/return-risk cohorts, SKU-level mismatch rates,
  warehouse/camera performance, recovery aging.
- **Finance controls** — write-off approvals, period close, journal export,
  GST-aware reports, multi-currency groundwork.
- **Enterprise identity** — SAML/OIDC SSO, SCIM, granular custom roles,
  organisation-level policy settings.
- **Field-level encryption** — encrypt identifier columns and PII at rest with
  KMS-backed keys.
- **Public API hardening** — idempotency keys for mutations, cursor pagination,
  per-key scopes and audit of API-key usage.

## Non-goals

- Calling real marketplace APIs or scraping seller portals in this build.
- Paid APIs, local LLMs or any AI dependency for core verification.
- Storing card/payment credentials.
- Replacing a marketplace's dispute system; ReturnProof prepares and tracks the
  seller side of the claim.
