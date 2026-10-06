# Spec 029 — Plan

Status: PROPOSED. Governing: ADR-034.

## Phase 0 — Discovery and governance (COMPLETE)

- Repository baselines recorded; all SQL confined to `jobs/repository.py`.
- Full two-relation inventory; eight inverse (A→J) paths identified.
- Pristine-baseline reproduction on isolated PostgreSQL (deterministic cycle +
  48/60 concurrent rounds).
- Spec 029 + ADR-034 presented `PROPOSED`; registry entries added.
- No source change.

## Phase 1 — Owner acceptance (GATE; owner action required)

- Owner reviews Spec 029 + ADR-034.
- `ACCEPTED` ⇒ proceed. `CHANGES_REQUESTED` ⇒ revise and re-present.
- No implementation before `ACCEPTED` (Constitution §42, §43).

## Phase 2 — Implementation

- Add `_lock_job_row` / `_lock_attempt_row` helpers.
- Normalize the eight inverse paths to J→A.
- Add `_retry_on_deadlock` with bounded attempts and low-cardinality metrics.
- Keep all public signatures and semantics unchanged.

## Phase 3 — Deterministic concurrency tests

- Implement the §4 race matrix on the real-PostgreSQL fixtures.
- Each test asserts final consistency and absence of duplicate terminal
  effects.

## Phase 4 — Isolated stress validation

- Bounded concurrency across workers/tenants; mixed
  completion/cancellation/retry; worker/Redis/API restarts; stale-lease
  recovery.
- Acceptance: zero known-cycle deadlocks after the fix; bounded retries; no
  stuck jobs / duplicate attempts / inconsistent terminal states.

## Phase 5 — Regression and gate validation

- Durable-jobs suite, API job status/cancel, Celery + local fallback,
  Spec 025/026/027 clients, Spec 028 ingestion correlation, governance
  registry test, OpenAPI consistency if models change (none expected).

## Phase 6 — Commit and deployment prompt

- Exactly one local Core commit (path-based staging only).
- Produce `/mnt/devel/retriva/tmp/durable-jobs-lock-order-deployment-prompt.txt`.
- No push/tag/PR/release; deployment is a separate phase.

## Phase 7 — Separate deployment (not in the implementation phase)

- Deploy per the deployment prompt with backups, rollback images, bounded
  live concurrency observation, and rollback/suspension criteria.