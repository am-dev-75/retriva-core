# Spec 025 architecture — durable Core jobs

Status: ACCEPTED — revision 2 (with the pack, 2026-10-04; applies the
owner CHANGES_REQUESTED decisions; explicitly accepted for
implementation).

## 1. Component layout (retriva-core)

New Core package `src/retriva/jobs/` (no Pro imports):

- `domain.py` — `JobStatus` (pending, dispatching, queued,
  dispatch_unknown, retry_wait, running, cancelling, manual_review,
  succeeded, failed, cancelled), `AttemptStatus`, `PublicationState`
  (prepared/publishing/published/unknown/rejected), the transition
  table (§3.2 of spec.md), `JobRecord`/`AttemptRecord` frozen
  dataclasses, sanitized error model (`error_code` + bounded
  `summary`), typed domain errors.
- `registry.py` — server-side job-type registry: type name → handler
  descriptor (Celery task name, restart-safe flag, max attempts
  default, queue class, subject semantics).  Persisted rows never
  invoke code by string lookup outside this registry.
- `repository.py` — protocol (`JobsRepository`) + psycopg2 adapter
  `PostgresJobsRepository` over the platform settings
  (`connection_kwargs("core")`); parameterized SQL; transactional
  transitions with guarded updates; tenant context set per
  transaction (`SET LOCAL app.current_tenant`), fail-closed.
- `tenant.py` — tenant resolution per the trust model (spec.md
  §3.12): startup validation of `RETRIVA_JOBS_DEFAULT_TENANT`
  (mandatory when no authenticated resolver is enabled), resolver
  modes (`fixed` | `gateway_header`), the clearly named
  development-only header override (loopback/trusted-constrained,
  prominent startup warning, auto-disabled when an authenticated
  resolver is active), and safe configuration-mode logging.  The
  service boundary always receives an explicit, server-resolved
  tenant; ordinary request input never silently selects one; the
  repository fails closed when context is absent.
- `service.py` — application service: submit (idempotency),
  publication-state dispatch (prepare → publish → classify),
  status/list/cancel, worker claim/progress/complete/fail,
  retry-schedule, operator retry (CLI-authorized), reconciliation
  entry points.  Tenant context flows in explicitly from callers.
- `dispatch.py` — the dispatch executor and publisher: attempt
  preparation (preallocated Celery task id, dispatch token/generation,
  `prepared` → `publishing`), `apply_async(task_id=<preallocated>)`,
  outcome classification (confirmed / definitely-rejected-before-
  acceptance / ambiguous / not-attempted per the bounded exception-
  class map; unmapped classes default to ambiguous), outcome
  persistence (`published` | `rejected` | `unknown`), and
  republication of the same generation for reconciliation.
- `celery_integration.py` — the Celery task base that wraps handler
  execution with the claim/progress/terminal protocol, redelivery/
  takeover classification (§3.5–§3.6 of spec.md), retry
  classification, and context propagation; the existing
  `ingestion_api` tasks delegate to it.
- `local.py` — the BackgroundTasks fallback executor on the SAME
  lifecycle (durable job + attempt claim via the same service;
  `execution_transport='local'`; in-process schedule recorded as
  publication `published`; no second state machine).
- `cli.py` — operator commands: `python -m retriva.jobs.reconcile`,
  `python -m retriva.jobs.cleanup`, `python -m retriva.jobs.retry`
  (dry-run default, bounded batches, explicit operational context,
  event-logged applied actions, documented exit codes 0/3/4/1).
- `reconcile.py` / `cleanup.py` — implementations behind the CLI
  (thresholds, conservative classification per R1–R9, restart-safe
  registry gate, retention purge).
- `api.py` — the `/api/v2/jobs` router extensions (compat-shaped;
  list/get/cancel; NO retry route on the public surface).
- `sql/` — the `core.jobs` stream: `V001__jobs_foundation.up.sql`
  (+ down): schema adoption (create-if-absent AUTHORIZATION
  retriva_migrator), the three tables, RLS (FORCE + policies),
  grants to `retriva_core` (USAGE, table DML, sequences; NO
  UPDATE/DELETE on `job_events`), the append-only trigger on
  `job_events` (BEFORE UPDATE OR DELETE → RAISE) plus the
  event_type CHECK constraint, Core-side default privileges,
  idempotency unique partial index, indexes (status+type, tenant,
  purge_after, scheduled_at, celery_task_id, publication_state),
  comments.  Down migration: drops ONLY Core-created objects (tables,
  trigger, indexes) — never the schema itself (it pre-exists as the
  CRM reservation).
- `migrations.py` — the `core.jobs` provider module
  (`MIGRATION_PROVIDERS`), registered by the Core migration CLI next
  to `core.platform` (Core→Core import only; the platform CLI gains
  a Core-stream registration list so the existing core one-shot
  applies `core.jobs` without any Compose change).

Pro-side (retriva-crm-assistant): `pro.crm` V009 ceding migration
(+ symmetric down): idempotent revocation of CRM USAGE on `jobs`,
explicit CRM privileges on Core job objects, and CRM default
privileges in `jobs` (ALTER DEFAULT PRIVILEGES ... REVOKE); plus the
destructive-downgrade guard in the CRM downgrade path (refuses while
Core-owned job objects/data exist; §3.13 of spec.md).

## 2. State machines

### 2.1 Job

```
pending ──dispatch-claim──> dispatching ──confirmed────────> queued ──worker-claim──> running
pending ──cancel──────────> cancelled (terminal)                  │                     │
dispatching ──rejected────> pending (retryable; new attempt next) │                     ├─> succeeded (terminal)
dispatching ──ambiguous───> dispatch_unknown ──evidence/repub──> queued                 ├─> failed    (terminal)
dispatch_unknown ──unresolved/contradiction──> manual_review                           └─> retry_wait ──reschedule──> queued
queued|running|dispatching|dispatch_unknown|retry_wait ──cancel──> cancelling
cancelling ──ack/never-executed──> cancelled (terminal)
cancelling ──success-evidence────> succeeded (terminal; side effects known complete)
cancelling ──failure-evidence────> failed    (terminal)
cancelling ──lost/contradiction──> manual_review (bounded; operator resolves)
failed ──operator retry (CLI)────> queued   (new attempt; never from the public API)
manual_review ──operator────────> succeeded | failed | cancelled | queued
```

Terminal: `succeeded`, `failed`, `cancelled`.  Immutability: every
terminal transition is predicate-guarded on the exact prior status;
late/duplicate callbacks raise `InvalidTransition` (attempt-side
state may still be recorded; the job never regresses).  Every
transition's actor, guard, atomic effect, event, meaning, duplicate
behavior, and reconciliation behavior are fixed in the spec.md §3.2
table.

### 2.2 Attempt

`queued → running → succeeded | failed | lost | cancelled`;
`dispatch_failed` (definite pre-acceptance rejection; generation
abandoned).  Publication states on the attempt: `prepared →
publishing → published | unknown | rejected`; republication of the
same generation reuses attempt identity + preallocated task id +
dispatch token and increments `publication_tries` (bounded), never
attempt_no.  The worker claim stamps `celery_task_id` (verified
against the preallocated id), `worker_id`, `started_at`, and
`execution_generation`; a proven-dead takeover re-claims the SAME
attempt with execution_generation+1; redelivery of a possibly-live
execution does NOT execute.

## 3. Dispatch sequence (publication-state boundary)

```
BEGIN;                                                   -- submit
  INSERT jobs (pending, attempt_count=0)                 -- or idempotency hit
  INSERT job_events (job_created)
COMMIT;
-- post-commit dispatch (idempotent; reconciliation covers crash windows):
BEGIN;                                                   -- T3 prepare
  UPDATE jobs SET status='dispatching' WHERE id=? AND status='pending'  -- rowcount gate
  INSERT job_attempts (attempt_no, dispatch_generation=1, dispatch_token,
                       celery_task_id=<PREALLOCATED>, publication_state='prepared')
  INSERT job_events (dispatch_prepared)
COMMIT;
publish: apply_async(task_id=<preallocated>, ...)        -- broker call
  confirmed (returned normally)      -> BEGIN; attempt.publication_state='published';
                                        published_at; job.status='queued'; event; COMMIT   -- T4
  definite pre-acceptance rejection  -> BEGIN; attempt.publication_state='rejected',
                                        status='dispatch_failed'; job.status='pending';
                                        event; COMMIT                                      -- T5
  ambiguous (timeout/reset/unmapped/crash window)
                                     -> BEGIN; attempt.publication_state='unknown';
                                        job.status='dispatch_unknown'; event; COMMIT       -- T6
```

Worker claim, republication (same attempt + task id + token),
progress (throttled), terminal transitions, retry/cancellation, and
the takeover rules follow spec.md §3.3–§3.6.  Reconciliation sweeps
cover R1–R9 (stale pending/dispatching, dispatch_unknown evidence
resolution, cancel-intent cases, retry_wait reschedule, stale
running with restart-safe gate, restored pre-terminal states,
divergence).  All reconciliation actions are event-logged,
batched, idempotent, and dry-run is the default.

A transactional outbox remains a documented escalation option only
if validation proves publication outcomes cannot be safely
classified and reconciled (spec.md §3.4).

## 4. Roles, grants, and tenancy

- `retriva_migrator`: owns schema/tables (development-phase shared
  migrator, unchanged from ADR-029); runs the stream.
- `retriva_core`: USAGE on `jobs`; SELECT/INSERT/UPDATE on `jobs`
  and `job_attempts`; SELECT/INSERT only on `job_events` (no
  UPDATE/DELETE); USAGE/SELECT on sequences.  This is the runtime
  identity of API and worker processes for jobs.
- Pro roles: NO privileges on Core job tables (CRM's ceding
  migration V009 revokes CRM V001's USAGE + explicit privileges +
  default privileges on `jobs`; idempotent; every valid order
  converges — spec.md §3.13 matrix).
- RLS: FORCE ROW LEVEL SECURITY on all three tables; policy
  `tenant_isolation` on `app.current_tenant = tenant_id`; the
  migrator/administrative paths bypass only via explicit superuser
  diagnostics, never via application roles.
- Event immutability: privilege revocation + BEFORE UPDATE OR DELETE
  trigger + bounded event_type CHECK (spec.md §3.14).
- Downgrade guard: the CRM destructive-downgrade path refuses while
  Core-owned job objects/data exist; Core job state is removed only
  by the explicit Core-owned `core.jobs` downgrade first; clean-DB
  and pre-Core cases defined in spec.md §3.13.
- Operator inspection: pgadmin/readonly diagnostics use the
  established read-only probe pattern (documented), not new grants.

## 5. Compatibility

- `/api/v2/jobs` response: existing fields unchanged in meaning;
  additive fields only; `error` becomes the sanitized summary;
  tenant-scoped + paginated; NO retry route on the public surface
  (operator CLI only).
- `/api/v1/jobs` (legacy, in-memory) unchanged this phase; documented
  as the legacy surface for L1–L11; it never lists durable rows.
- Ingestion endpoints: paths and 202 contract unchanged; the job id
  remains the durable Core UUID; status/cancel flows move to the
  durable store for the integrated flow (the Redis
  job-state/cancel keys are retired there); v2 status routes resolve
  ids durable-FIRST with documented legacy fallback (spec.md §3.11).
- `JobManager` remains for L1–L11 (v1 ingest routers, artifacts, v1
  job endpoints) until the named follow-up migration; the boundary
  is pinned by integration tests.

## 6. Observability

Structured logs (job_id, attempt_id, job_type, publication outcome
class, status transitions, durations; no payload/tenant-label
content) + in-process counters exposed through the existing
diagnostics surfaces: jobs by status/type, publication outcomes
(confirmed/rejected/ambiguous), dispatch latency, execution
duration, retries, stale running, failed dispatches, terminal
failures by safe error code, reconciliation and cleanup actions,
manual_review backlog — bounded aggregate counts without
high-cardinality tenant labels.

## 7. Testing strategy (maps to acceptance.md)

- Domain: pure unit tests over the transition table (allowed,
  forbidden, terminal immutability, retry limits, cancellation-race
  matrix, manual_review semantics) — no DB.
- Persistence: real PostgreSQL (scratch cluster, platform-test
  pattern): migration clean-DB, restored-copy ownership transition,
  idempotent rerun, the FULL order matrix (spec.md §3.13) with
  per-path owner/grant/default-privilege/RLS/DML/denial/ledger
  probes, RLS probes (cross-tenant denial, fail-closed), grants
  probes (runtime DML, denied DDL, denied Pro writes, denied event
  UPDATE/DELETE), idempotency unique index, guarded transitions
  under concurrency, event-append-only trigger.
- Dispatch/worker: fake broker for service-level flows; publication
  classification unit tests (definite vs ambiguous exception maps);
  Celery eager mode for task-protocol flows; claim/takeover race
  tests; OOM redelivery rules; one real-broker marked integration
  test (Redis) for the delivery path.
- API: FastAPI TestClient with a real scratch database; tenant trust
  model (fixed tenant mandatory/validated; gateway_header stripping
  contract; dev override warning + constraints; ordinary input
  cannot select tenants; fail-closed), no-public-retry assertion,
  cross-tenant denial, pagination bounds, sanitized failures, compat
  shapes.
- Restore/reconciliation: scripted stale states (including a
  restored pre-terminal database) reconciled idempotently; manual_review
  surfaced; CLI exit codes.
- Fallback: BackgroundTasks path on the durable lifecycle
  (transport field, restart survival of durable state, stale
  local-attempt classification).
- Container lifecycle: core one-shot applies `core.jobs` after
  `core.platform`; Core-only stack healthy; Redis flush preserves
  job history; pro.crm + core.jobs order independence with the
  ceding migration; CRM destructive-downgrade guard refusal and the
  clean-DB/pre-Core allowances.