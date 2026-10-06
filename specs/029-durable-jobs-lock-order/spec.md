# Spec 029 — Durable-jobs canonical lock ordering

- **Status:** ACCEPTED (owner acceptance recorded 2026-10-06; Constitution
  §42, §43). Implementation phase authorized; live deployment remains a
  separate governed phase.
- **Revision:** 2 (2026-10-06) — revision 1 presented `PROPOSED`; owner
  accepted revision 1 unchanged on 2026-10-06 and implementation proceeded.
- **Repository:** retriva-core
- **Governing documents:** `.agent/rules/retriva-constitution.md`;
  ADR-034; Spec 025 (durable job lifecycle, ACCEPTED rev 2); ADR-030.
  Cross-reference only: Spec 028 / ADR-033 (knowledge authority) is NOT
  reopened and NOT in scope.
- **Owner:** Retriva Core owner (acceptance required).

## 1. Problem

The durable job lifecycle locks two relations, `jobs.jobs` (J) and
`jobs.job_attempts` (A). Spec 025 does not define an inter-relation lock
order and the implementation is inconsistent: the claim path acquires J→A,
while eight paths acquire A→J. Concurrent transactions on the same job can
therefore deadlock (SQLSTATE `40P01`).

### Evidence (pristine baseline `d19edc3`)

- Live `cust_0007` deadlocks: 2026-10-06 09:19:22 and 17:48:53 CEST, both
  `record_publication_confirmed` (A→J) versus `claim_for_delivery` (J→A).
  PostgreSQL aborted one side (`FATAL`/`ERROR: deadlock detected`), recovered
  automatically, and left no job stuck.
- Deterministic isolated-PostgreSQL reproduction (`tmp`, throwaway cluster):
  a real `record_publication_confirmed` call interleaved with the claim
  path's two statements deadlocked (`40P01`); 60 concurrent method-vs-method
  rounds produced 48 deadlocks.

### Inverse paths (A→J) at `d19edc3`

`record_publication_confirmed`, `record_publication_rejected`,
`record_publication_ambiguous`, `complete_success`, `complete_failure`,
`acknowledge_cooperative_cancel`, `mark_execution_lost`,
`cancel_unclaimed_attempt` (`src/retriva/jobs/repository.py`).

### Consistent paths (J→A) at `d19edc3`

`prepare_dispatch`, `prepare_retry_dispatch`, `prepare_operator_retry`,
`resolve_manual_review` (re-dispatch branch), `claim_for_delivery`,
`purge_expired_jobs` (FK cascade).

## 2. Normative requirement

**R1 — Lock-order invariant.** Any transaction that locks or mutates both
`jobs.jobs` and `jobs.job_attempts` MUST acquire the `jobs.jobs` row lock
first, then the `jobs.job_attempts` row lock. No transaction may hold a
`job_attempts` lock while acquiring or waiting for the corresponding `jobs`
lock.

**R2 — Enforcement in one place.** The invariant MUST be enforced through
shared internal repository helpers, not by scattered ad hoc edits, and MUST
NOT expose raw transaction/lock control to public APIs.

**R3 — No semantic change.** Guards, terminal-state monotonicity, idempotency,
tenant ownership, leases, attempts, progress, retry counters, cancellation
fields, and subject correlation MUST be preserved exactly.

**R4 — Bounded retry (defense in depth).** Safely-retryable PostgreSQL errors
(`40P01`, and `40001` if isolation ever changes) MUST be retried at most three
times with small jittered backoff; non-retryable errors (validation,
authorization, not-found, unique-violation handled explicitly) MUST NOT be
retried; no retry may create a duplicate attempt or duplicate terminal effect.

**R5 — Observability.** Retry/deadlock counts MUST be low-cardinality and MUST
NOT expose SQL, credentials, tenant identifiers, job/attempt identifiers, or
content (Constitution §33, §41).

**R6 — No migration.** The fix MUST be transactional code plus tests unless
discovery proves a schema change is strictly required, in which case it is
re-governed. Expected disposition: no migration.

**R7 — Compatibility.** No change to the public API, OpenAPI models, tenancy
boundaries, stores of record, Qdrant payloads, knowledge authority, or
Spec 025 transition semantics.

## 3. Acceptance criteria

- **A1** Every two-relation path acquires J before A; the eight inverse paths
  are corrected; no inverse path remains (static inventory + code review).
- **A2** Deterministic concurrency tests pass on real isolated PostgreSQL for
  the full race matrix (§4), each asserting final consistency of job state,
  active attempt, attempt state, ownership/lease, progress, terminal result,
  retry counters, cancellation fields, and absence of duplicate terminal
  effects.
- **A3** Real-PostgreSQL bounded stress (multiple workers/tenants, mixed
  completion/cancellation/retry, restarts, Redis reconnect without flush,
  stale-lease recovery) reports **zero** known-cycle deadlocks after the fix;
  retry metrics bounded; no stuck jobs, duplicate attempts, or inconsistent
  terminal states.
- **A4** The pristine-baseline reproduction is retained as the regression
  gate and fails on the pre-fix code, passes on the fixed code.
- **A5** Celery and local fallback both pass; Spec 025/026/027 durable-job
  clients and Spec 028 ingestion-job correlation remain compatible.
- **A6** Security: tenant isolation/RLS unchanged; no secret/content leakage;
  no new runtime DDL or cross-schema privilege.
- **A7** Migration disposition recorded (`no migration required` expected).
- **A8** Exactly one local Core implementation commit after all gates pass;
  no push/tag/PR/release/deploy in the implementation phase; deployment is a
  separate authorized phase.

## 4. Required concurrency matrix

claim vs success; claim vs failure; claim vs cancellation; heartbeat vs
terminal completion; progress update vs cancellation; retry scheduling vs
late success; stale-lease recovery vs original worker completion; duplicate
success callbacks; duplicate failure callbacks; success vs failure race;
success vs cancellation race; two workers claiming the same attempt; local
fallback vs Celery completion; cleanup/reconciliation vs active transition;
injected deadlock/serialization retry; retry exhaustion; process interruption
between job lock and attempt mutation.

## 5. Out of scope

Spec 028 knowledge schema/authority; Qdrant; CRM/Messaging/gateway/deployment/
connector source; GraphIndexer/GraphRAG; Redis replacement; schema/migration
changes (unless proven strictly required); unrelated test cleanup; live
deployment; push/merge/rebase/tag/PR/release.

## 6. Governance and phase gates

1. Present this pack + ADR-034 (`PROPOSED`) with registry entries.
2. **Owner acceptance required** before any implementation (Constitution §42,
   §43). "Accept with changes" ⇒ `CHANGES_REQUESTED`, re-present.
3. On `ACCEPTED`: implement, run the concurrency and stress gates, create one
   local Core commit, produce the deployment prompt.
4. Deployment to `cust_0007` is a separate governed phase.

## 7. Current status

`ACCEPTED` (2026-10-06). Discovery, lock inventory, and pristine-baseline
reproduction were completed under revision 1. Owner acceptance is recorded;
implementation, deterministic concurrency tests, isolated stress validation,
and exactly one local Core commit followed. The live authoritative stack is
deployed separately and was unchanged by this phase.

### 7.1 Owner acceptance record (2026-10-06)

The owner accepted Spec 029 and ADR-034. Authorized: governance status change
to `ACCEPTED`; shared job-first lock primitives; normalization of all eight
inverse paths; bounded retry as defense in depth for safely retryable
PostgreSQL concurrency failures only; deterministic and stress concurrency
tests on isolated PostgreSQL/Redis/Celery/API/workers/local fallback; exactly
one local Core commit after all gates; finalization of the separate deployment
prompt. Not authorized: live deployment or live stress; any change to Spec 028,
knowledge authority, Qdrant, CRM, Messaging, Gateway, deployment, connectors,
GraphIndexer, GraphRAG, or lifecycle-simulation tests; push/merge/rebase/tag/PR/
release. Expected disposition: no migration; a migration would require
additional owner acceptance before creation.