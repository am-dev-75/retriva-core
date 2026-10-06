# Spec 029 — Architecture

Status: PROPOSED. Governing: ADR-034. Repository: retriva-core.

## 1. Current structure

All durable-jobs SQL is confined to `src/retriva/jobs/repository.py`
(`PostgresJobsRepository`). Every public method opens exactly one transaction
via the `_transaction(tenant_id, privileged)` context manager (one connection,
`SET LOCAL app.current_tenant`, commit on success, rollback on error). No
public method nests another method's transaction, so lock ordering is decided
entirely by the statement order inside each method.

## 2. Target design

### 2.1 Shared lock primitives (internal)

```
def _lock_job_row(cur, *, job_id, tenant_id=None):
    # SELECT * FROM jobs.jobs WHERE id=%s [AND tenant_id=%s] FOR UPDATE

def _lock_attempt_row(cur, *, attempt_id, tenant_id=None, job_id=None):
    # SELECT * FROM jobs.job_attempts WHERE id=%s [AND job_id=%s]
    # [AND tenant_id=%s] FOR UPDATE
```

Both return the row (or `None`). Every two-relation method calls
`_lock_job_row` first, then `_lock_attempt_row` (or lets its first attempt
`UPDATE` acquire A), then performs mutations. Because both rows are already
held in J→A order, the order of subsequent `UPDATE`s is irrelevant to lock
acquisition.

### 2.2 Paths to normalize (A→J → J→A)

| Method | Change |
|---|---|
| `record_publication_confirmed` | lock J (`FOR UPDATE`) before the attempt update |
| `record_publication_rejected` | lock J before the attempt update |
| `record_publication_ambiguous` | lock J before the attempt update |
| `complete_success` | lock J first; capture `was_cancelling` from the locked row; then attempt update; then job update |
| `complete_failure` | lock J first, then `_lock_attempt_row`; guards unchanged |
| `acknowledge_cooperative_cancel` | lock J before attempt update |
| `mark_execution_lost` | lock J before attempt update |
| `cancel_unclaimed_attempt` | lock J before attempt update |

Already consistent (unchanged): `prepare_dispatch`, `prepare_retry_dispatch`,
`prepare_operator_retry`, `resolve_manual_review` (queued branch),
`claim_for_delivery`, `purge_expired_jobs` (FK cascade J→A). Single-relation
paths (`record_publication_inflight`, `record_progress`, `request_cancel`,
terminal `resolve_manual_review`, reads) are untouched.

### 2.3 Bounded retry (defense in depth)

An internal decorator `_retry_on_deadlock` wraps transactional public methods.
It retries only `psycopg2.errors.DeadlockDetected` (`40P01`) and
`SerializationFailure` (`40001`): at most `DEADLOCK_RETRY_ATTEMPTS = 3` with
jittered backoff (`base * attempt + uniform(0, jitter)`). It never retries
`JobsError`, `IdempotencyConflictError`, `JobNotFoundError`, unique-violation
handling, or tenant-context failures. Retries re-invoke the whole (idempotent,
predicate-guarded) repository method; there are no external side effects inside
repository transactions.

Observability: a module-level counter (`deadlock retries`, `retries
exhausted`) exposed via a bounded accessor, plus a warning log without
identifiers/content.

### 2.4 Transparency

No public signature changes; decorators preserve signatures via
`functools.wraps`. No raw lock/transaction control is exported.

## 3. Indices, FKs, cascades

- `job_attempts.job_id → jobs.jobs(id) ON DELETE CASCADE`: an `INSERT` of an
  attempt takes `FOR KEY SHARE` on the parent job; the consistent paths already
  update J first, so the FK lock is subsumed. `DELETE jobs` cascades J→A.
- `job_events.job_id → jobs.jobs(id)`: event inserts take `FOR KEY SHARE` on
  the job; taken after J is held on all two-relation paths.
- No trigger reorders locks; `job_events` immutability trigger does not lock
  the two relations.

## 4. Testing architecture

- Deterministic tests use the existing real-PostgreSQL fixtures
  (`pg_platform_stack`, `durable_jobs_database`) and drive the real repository
  methods with controlled interleaving (barrier threads / explicit helper
  transactions) to force each race, then assert final consistency.
- The pristine-baseline reproduction harness (throwaway cluster; not committed
  as product code) is retained as the regression proof: pre-fix reproduces
  `40P01`; post-fix reports zero.
- A bounded stress test exercises mixed operations across workers/tenants and
  worker/Redis restarts, measuring deadlocks, retries, stuck jobs, duplicate
  attempts, and inconsistent terminal states.

## 5. Migration disposition

None. Schema, RLS, grants, triggers, and indexes are unchanged.