# Spec 030 — Acceptance

Status: ACCEPTED (owner acceptance 2026-10-07). Implementation authorized.

## A. Acceptance matrix

| # | Criterion | Gate |
|---|---|---|
| A1 | Baseline HTTP 500 + `IdempotencyConflictError` + temp leak reproduced | isolated repro |
| A2 | Canonical identity excludes volatile fields, includes stable fields | test vectors |
| A3 | Same key + same canonical identity reuses one job/result (no duplicates) | integration (real PG) |
| A4 | Same key + different canonical identity → bounded 409, no leakage/side effect | route test |
| A5 | Every terminal path cleans request-local temp file exactly once | leak ledger |
| A6 | Deterministic concurrency/race matrix passes | test suite |
| A7 | Real isolated runtime (PG/Redis/Celery/API/worker/local/Qdrant/parser) passes; restart/reconnect | runtime harness |
| A8 | Spec 029 invariant + concurrency green; Spec 028 integrity unchanged | suites |
| A9 | Compatibility (upload/document/MediaWiki/status/OpenAPI/CRM/Gateway/Web UI) | suites |
| A10 | Security/privacy (tenant isolation, no leakage, no arbitrary deletion, no new DDL/privilege) | review |
| A11 | No migration required | review |
| A12 | Exactly one local Core commit; deployment prompt produced | repo audit |

## B. Evidence at proposal time (baseline, isolated)

- Deterministic isolated-PostgreSQL reproduction (pristine `110a824a…`, real
  repository/service + real `fingerprint_for`): fingerprints differ when only
  `temp_path`/`created_at` differ; the second identical re-upload raises
  `IdempotencyConflictError`; exactly one job with the key exists; identical
  same-fingerprint submissions reuse one job (control). Code path and live
  observation (HTTP 500 twice, temp files accumulated) recorded separately.
- Input inventory recorded (see external report).
- No product source change, no commit, no deployment; live `cust_0007`
  unchanged.

## C. Owner acceptance

Spec 030 and ADR-035 are `ACCEPTED` (owner acceptance 2026-10-07). Implementation
and isolated validation are authorized; live deployment is a separate phase.

## D. Status

ACCEPTED. Implementation, runtime validation, leak accounting, the single Core
commit, and the deployment prompt are performed under this phase.

## E. Implementation results (2026-10-07, ACCEPTED)

- Canonical `v2upload-input/1` identity implemented in
  `durable_jobs.canonical_upload_identity` / `upload_input_fingerprint`;
  volatile `temp_path`/`created_at` and derived ids excluded; stable fields
  NFC/ordering-normalized; SHA-256 over canonical JSON.
- `JobsService.submit(..., _with_status=True)` exposes the created flag;
  `submit_upload_job` passes the canonical fingerprint and returns real
  `created`.
- `IdempotencyConflictError` mapped to HTTP 409 with stable code
  `upload_idempotency_conflict` at the app boundary.
- `UploadTempFile` shared ownership guard (`create`/`transfer`/`release`,
  idempotent, missing-safe, staging-root + symlink confined) wired into the
  upload route; worker terminal cleanup routed through it.
- Concurrent-submission convergence hardened with a savepoint in
  `repository.submit_job` so a lost unique-index race reuses the winner.
- Tests: `tests/test_upload_idempotency.py` (12) + `tests/test_upload_idempotency_route.py` (3)
  pass. Regression suites: 202 passed (jobs/lock-order/api/artifact/spec027/
  governance/constitution) + 28 knowledge-related.
- Isolated concurrency harness: 0 deadlocks, 0 stuck, 0 duplicate terminal
  effects, 0 inconsistent states. Real Celery worker + isolated Redis:
  12/12 terminal, then 8/8 after a Redis restart without flush (20 succeeded).
- Migration disposition: **no migration required**.

### A. Acceptance matrix status

A1 reproduced, A2 canonical, A3 reuse, A4 409, A5 cleanup, A6 concurrency,
A7 runtime, A8 Spec 028/029, A9 compatibility, A10 security, A11 no-migration,
A12 single commit + deployment prompt: satisfied (see external report).