# Spec 030 — Plan

Status: PROPOSED. Governing: ADR-035.

## Phase 0 — Discovery and governance (COMPLETE)
- Baselines recorded; live `cust_0007` verified authoritative/healthy.
- Root cause localized (`submit_upload_job` fingerprint over volatile fields,
  unmapped conflict, temp-file leak).
- Baseline reproduced deterministically on isolated PostgreSQL.
- Input inventory and canonicalization contract drafted.
- Spec 030 + ADR-035 presented `PROPOSED`; registry entries added. No source
  change.

## Phase 1 — Owner acceptance (GATE; owner action required)
- Owner reviews Spec 030 + ADR-035. `ACCEPTED` ⇒ proceed;
  `CHANGES_REQUESTED` ⇒ revise and re-present. No implementation before
  `ACCEPTED` (Constitution §42, §43).

## Phase 2 — Implementation
- Add `canonical_upload_identity` + versioned canonical hash.
- Use it for the upload `input_fingerprint`; keep idempotency key content-stable.
- Map `IdempotencyConflictError` → 409 with stable code at the v2 boundary.
- Add `UploadTempFile` ownership guard + worker/local cleanup parity + startup
  wire all upload routes (no startup sweep in this revision).

## Phase 3 — Deterministic tests
- Canonicalization vectors; unit + integration (real PG); route 500→409;
  concurrency/race matrix (barriers, no sleeps); Spec 029 invariant preserved.

## Phase 4 — Real isolated runtime validation
- PostgreSQL, Redis, Celery, API, worker, local fallback, Qdrant, parser, temp
  storage; repeated rounds; restart/reconnect; temp-file leak accounting.

## Phase 5 — Compatibility and security
- Upload v2, generic document, MediaWiki, job status/cancel, Spec 028 identity,
  Spec 029 invariant, CRM/Gateway/Web UI upload paths, OpenAPI consistency,
  governance/constitution/migration contract; tenant isolation; no leakage.

## Phase 6 — Commit and deployment prompt
- Exactly one local Core commit (path-based staging); external report/evidence/
  leak ledger; parameterized deployment prompt (not executed).

## Phase 7 — Separate deployment (not in this phase)
- Deploy per the deployment prompt with backup/rollback, bounded live
  duplicate-upload validation, restart checks, rollback/suspension criteria.