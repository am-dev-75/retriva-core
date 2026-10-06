# ADR-034: Canonical durable-jobs lock ordering (`jobs` before `job_attempts`)

- **Status:** ACCEPTED (owner acceptance 2026-10-06; Constitution §42, §43).
  Supersedes its own PROPOSED state; no prior accepted ADR amended or
  superseded.
- **Date:** 2026-10-06
- **Repository:** retriva-core
- **Supersedes / amends:** none. Corrects an implementation/contract
  divergence within the durable-jobs lifecycle governed by Spec 025 and
  ADR-030; does not change Spec 025 transition semantics, stores of record,
  tenancy, or the Spec 028 knowledge-authority architecture.
- **Deciders:** Retriva Core owner (acceptance recorded 2026-10-06).

## Acceptance record (2026-10-06)

The owner accepted ADR-034 and Spec 029. The decision below is binding.
Implementation (shared job-first lock primitives, normalization of the eight
inverse paths, bounded idempotent retry as defense in depth, deterministic and
stress concurrency tests) is authorized and was performed under isolated
validation, followed by exactly one local Core commit. Live deployment remains
a separate governed phase and was not performed here. No schema migration was
required. Spec 028 / ADR-033 remain closed and untouched; a migration would
require additional owner acceptance before creation.

## Context

The durable job lifecycle (Spec 025; ADR-030) performs most transitions as
single, predicate-guarded atomic `UPDATE`s and one row-locked claim. Two
relations are involved: `jobs.jobs` (J) and `jobs.job_attempts` (A), with
`job_attempts.job_id → jobs.jobs(id) ON DELETE CASCADE`.

Spec 025 does not state an inter-relation lock-acquisition order. The
implementation is not uniform:

- The **claim** path (`claim_for_delivery`) locks **J then A**.
- Eight other paths lock/update **A then J**:
  `record_publication_confirmed`, `record_publication_rejected`,
  `record_publication_ambiguous`, `complete_success`, `complete_failure`,
  `acknowledge_cooperative_cancel`, `mark_execution_lost`,
  `cancel_unclaimed_attempt`.

When an A→J transaction interleaves with a J→A transaction on the same job,
PostgreSQL raises SQLSTATE `40P01` (deadlock) and aborts one side. This was
observed in the live `cust_0007` stack (two deadlocks, 2026-10-06 09:19:22 and
17:48:53 CEST) and reproduced deterministically on the pristine baseline
(commit `d19edc3`): `record_publication_confirmed` (A→J) versus the claim path
(J→A), plus 48/60 concurrent method-vs-method rounds. PostgreSQL recovers
automatically and no job was left stuck, but the defect is a latent
availability/latency hazard that worsens with concurrency and producer load.

The A→J ordering is not a documented contract; `claim_for_delivery`'s
docstring already asserts "all paths follow it", so the correction restores
intended consistency. Because it introduces a cross-cutting persistence
invariant plus bounded retry semantics, §43 requires this decision to be
recorded in an ADR **before** implementation.

## Decision

1. **Invariant.** Any transaction that locks or mutates both `jobs.jobs` and
   `jobs.job_attempts` MUST acquire the `jobs.jobs` row lock **first**, then
   the `jobs.job_attempts` row lock. No transaction holding a `job_attempts`
   lock may subsequently attempt to acquire the corresponding `jobs` lock.

2. **Scope.** The invariant applies to every durable-jobs path: claim, lease
   and heartbeat where both relations are touched, attempt creation,
   publication outcomes, success/failure completion, cooperative and
   reconciliation cancellation, lost-execution classification, retry
   scheduling, stale-lease recovery, duplicate delivery, and the privileged
   retention purge (whose FK cascade is already J→A).

3. **Shared primitives.** The repository exposes internal, non-public lock
   helpers (`_lock_job_row`, `_lock_attempt_row`) so the order is enforced in
   one place rather than by scattered edits. Raw transaction or lock control
   is never exposed to public APIs.

4. **Defense in depth.** A bounded, idempotent retry wraps repository
   transitions for safely-retryable errors only (`40P01` deadlock, and
   `40001` serialization if the isolation level ever changes): at most three
   attempts with small jittered backoff, never retrying validation,
   authorization, or not-found errors, never duplicating terminal effects.
   Retry is secondary; the ordering invariant is the fix.

5. **Observability.** Retries are counted with low-cardinality labels and
   logged without SQL, credentials, tenant ids, job ids, or content.

6. **No migration.** The fix is transactional code plus tests; the schema,
   RLS, grants, triggers, and indexes are unchanged. Disposition: **no
   migration required**.

## Consequences

- The known A↔J deadlock cycle is eliminated by construction; retry absorbs
  any residual or future transient cycle.
- Correctness is preserved: guards, terminal-state monotonicity, idempotency,
  ownership, leases, attempts, progress, and subject correlation are
  unchanged; only the order of lock acquisition within a transaction changes.
- A readily reproducible concurrency regression gate is added (real
  PostgreSQL), keeping the invariant protected (Constitution §40).
- No public contract, OpenAPI model, tenancy boundary, store of record,
  Qdrant payload, or Spec 028 authority behavior changes.

## Compliance

- Constitution §42 (spec before code) and §43 (ADR before implementation):
  this ADR and Spec 029 are presented `PROPOSED`; implementation MUST NOT
  begin until both are `ACCEPTED`.
- Constitution §30 (auditability), §32 (tenant isolation), §20 (store of
  record), §12 (additive compatibility), §40 (regression protection):
  preserved.

## Alternatives considered

- **Global serialization / process mutexes / Redis locks:** rejected — they
  reduce concurrency or move relational consistency to an unreliable layer
  (Spec 025 keeps Redis as transport only).
- **Retry-only (no reordering):** rejected — treats the symptom; the ordering
  invariant removes the cycle and is cheap.
- **Ad hoc per-method ordering edits without shared primitives:** weaker
  traceability; rejected in favor of shared helpers and an explicit invariant.