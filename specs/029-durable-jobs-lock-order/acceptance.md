# Spec 029 — Acceptance

Status: PROPOSED. Implementation is barred until this pack and ADR-034 are
`ACCEPTED` (Constitution §42, §43). This file defines the acceptance matrix and
records the evidence available at proposal time.

## A. Acceptance matrix

| # | Criterion | Gate |
|---|---|---|
| A1 | Every two-relation path acquires J before A; no inverse path remains | static inventory + code review |
| A2 | Deterministic concurrency matrix (§4 of spec) passes on real PostgreSQL; final consistency asserted per race | test suite |
| A3 | Bounded stress: zero known-cycle deadlocks after fix; bounded retries; no stuck jobs/duplicate attempts/inconsistent terminal states | stress harness |
| A4 | Pristine-baseline reproduction fails pre-fix, passes post-fix | regression gate |
| A5 | Celery + local fallback + Spec 025/026/027/028 compatibility | suite |
| A6 | RLS/tenant isolation unchanged; no leakage; no new DDL/cross-schema privilege | security review |
| A7 | Migration disposition recorded | review (expected: no migration) |
| A8 | Exactly one local Core commit; no push/tag/PR/release/deploy in phase | repository audit |

## B. Evidence at proposal time (baseline, read-only)

- Live deadlocks: `2026-10-06 09:19:22 CEST` and `2026-10-06 17:48:53 CEST`,
  `40P01`; PostgreSQL auto-recovered; no stuck jobs. Cycle:
  `record_publication_confirmed` (A→J) vs `claim_for_delivery` (J→A).
- Pristine baseline commit `d19edc3a7e9b2d0ca8cf2e240e4b442312e7c562`:
  - deterministic isolated-PostgreSQL cycle reproduced (`40P01`);
  - 60 concurrent method-vs-method rounds → 48 deadlocks;
  - final state consistent after recovery.
- Lock inventory: 8 inverse paths, 6 consistent paths, single-relation paths
  enumerated (see `durable-jobs-lock-inventory.json`).

## C. Implementation results (2026-10-06, ACCEPTED revision)

### Final lock inventory
All durable-jobs SQL is confined to `src/retriva/jobs/repository.py`. The
eight inverse (`job_attempts -> jobs`) paths were normalized to acquire the
`jobs.jobs` row lock first: `record_publication_confirmed`,
`record_publication_rejected`, `record_publication_ambiguous`,
`complete_success`, `complete_failure`, `acknowledge_cooperative_cancel`,
`mark_execution_lost`, `cancel_unclaimed_attempt`. The six canonical
(`jobs -> job_attempts`) paths are preserved: `prepare_dispatch`,
`prepare_retry_dispatch`, `prepare_operator_retry`, `resolve_manual_review`
(re-dispatch), `claim_for_delivery` (refactored to shared primitives,
identical order), `purge_expired_jobs` (FK cascade). Single-relation paths and
reads are unchanged. An AST invariant check (`tests/test_jobs_lock_order.py::
test_repository_lock_order_invariant`) proves no two-relation method acquires
`job_attempts` before `jobs`.

### Baseline reproduction (isolated export at `d19edc3`)
- Deterministic cycle: `record_publication_confirmed` vs the claim path →
  `DeadlockDetected` SQLSTATE `40P01`, `reached_cycle=true`.
- Concurrent method-vs-method: 60 rounds → 39 deadlocks (pristine); the exact
  prior count 48/60 is not required to match.
- Final state remained consistent after the aborted transaction.

### Post-fix validation
- Deterministic cycle no longer reachable; 60 concurrent rounds → **0**
  deadlocks.
- `tests/test_jobs_lock_order.py`: 15 passed (static invariant + barrier race
  matrix + bounded-retry unit tests).
- Isolated PostgreSQL 16 multi-worker stress (20 workers, 50 jobs, mixed
  claim/confirm/success/failure/cancel/retry/stale-recovery): **0 deadlocks, 0
  stuck jobs (after the real reconciliation sweep), 0 duplicate terminal
  effects, 0 inconsistent states**; 112 guard refusals (`JobsError`) are
  expected typed no-ops under concurrent guarded transitions.
- Real Celery worker over an isolated Redis broker executing the real durable
  worker protocol: 12/12 terminal, then a Redis restart (no durable flush) and
  a second batch 8/8 terminal (20/20 succeeded).
- Local fallback and Celery dispatch paths validated by
  `test_artifact_jobs.py` / `test_jobs_dispatch.py` / `test_jobs_api_v2.py`.
- Focused suites: `test_jobs_persistence`, `test_jobs_domain`,
  `test_jobs_dispatch`, `test_jobs_lock_order`, `test_jobs_api_v2`,
  `test_artifact_jobs`, `test_spec027_decommissioning`,
  `test_governance_registry`, `test_constitution_integrity` = 190 passed;
  knowledge correlation (`test_knowledge_lifecycle_sim`,
  `test_knowledge_integration`, `test_knowledge_pipeline`) = 16 passed.

### Bounded retry (defense in depth)
`_retry_on_transient` replays whole guarded transactions on `40P01`/`40001`
only, at most `TRANSIENT_RETRY_ATTEMPTS = 3` with jittered backoff; typed
domain/validation/not-found errors are never retried; the repository
transactions have no external side effects, so replay cannot duplicate an
attempt or terminal effect. Low-cardinality counters only.

### Migration disposition
**No migration required.** No schema, RLS, grant, trigger, or index change.
No new runtime DDL or cross-schema privilege.

### Commit / deployment
Exactly one local Core commit implements Spec 029 / ADR-034. Live deployment
is a separate governed phase (`/mnt/devel/retriva/tmp/
durable-jobs-lock-order-deployment-prompt.txt`) and was not performed.