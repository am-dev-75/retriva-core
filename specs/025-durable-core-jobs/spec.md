# Spec 025: Durable Core job lifecycle persistence (PostgreSQL system of record)

- **Status:** ACCEPTED — revision 2 (2026-10-04).  Revision 1 was
  presented and returned **CHANGES_REQUESTED**; revision 2 applied
  every owner decision and was re-presented; the owner then
  **explicitly accepted** Spec 025 revision 2 and ADR-030 revision 2
  for implementation (acceptance event recorded in ADR-030 §Status
  and plan.md §7).  The approved `manual_review` state is bounded and
  non-terminal: it is used ONLY where the system cannot safely
  determine whether execution or external side effects occurred —
  never as a generic error state and never as a substitute for
  deterministic reconciliation.  Per Constitution §42, implementation
  now proceeds under the accepted scope; a material architectural
  change during implementation returns the pack to CHANGES_REQUESTED
  for owner review.
- **Order of authority:** Retriva constitution v1.2 (canonical) →
  ADR-030 (PROPOSED) → this spec → architecture.md → plan.md /
  tasks.md / acceptance.md → code.
- **Registry:** allocated as specification 025 and ADR-030 in
  `retriva-core/docs/governance/spec-adr-registry.yaml` before first
  presentation (Constitution §43); statuses remain `proposed`; no new
  numbers were allocated for this revision.
- **Implements:** the durable Core jobs follow-up deferred by
  Spec 024 (plan.md §9.1) and ADR-029 (§Consequences).
- **Baseline:** Spec 024 commits — retriva-core `e67dd44`,
  retriva-crm-assistant `c563aa6`, retriva-messaging-extension
  `d12b38e`, retriva-local-containerized-deployment `1b84ff4`
  (branch `centralized_relational_db`).
- **Revision 2 record:** owner review decisions 1–14 of 2026-10-04
  (dispatch publication model; tenant trust model; `jobs` schema
  transfer + downgrade guard; retention defaults; retry
  authorization; operator commands; cancellation races; attempt
  numbering; event immutability; BackgroundTasks fallback; legacy
  inventory).  Discovery findings were re-verified against the
  repositories during this revision; no contradictions were found.

---

## 1. Objective

Make PostgreSQL the authoritative system of record for the logical,
user-visible lifecycle of Retriva Core's asynchronous work, while
Celery and Redis remain the task-delivery and worker-coordination
mechanism.  The subsystem is Core-owned, tenant-isolated, integrated
first with the representative v2 ingestion workflow, and operated
through explicit manual operator commands (no scheduler in this
phase).

Out of scope (binding): replacing Celery with a PostgreSQL queue;
replacing Redis; workflow DAG orchestration; general-purpose event
sourcing; a platform-wide event bus; a transactional outbox (kept as
a documented escalation option only, §3.4); cron/scheduler/beat
product features or any automatic scheduling of reconciliation or
cleanup; connector SQLite, GraphRAG, and file/attachment state
migration; Messaging provider-contract integration and delivery
workflows; CRM campaign integration; tenant-facing retry APIs before
an authenticated principal exists (§3.12); production HA/backup/DR
and secret-manager integration; UI redesign; unrelated refactoring.

## 2. Current-state facts (verified — see plan.md §1 for sources)

1. Async execution today: Celery app `retriva_ingestion`
   (`src/retriva/ingestion_api/celery_app.py`), Redis broker and
   Redis result backend (`celery_result_backend or broker`), JSON
   serialization, `acks_late=True`,
   `task_reject_on_worker_lost=True`, `task_track_started=True`,
   prefetch multiplier and concurrency from settings,
   `task_default_max_retries` (default 3), `result_expires` 7 days,
   single `ingestion` queue, two registered tasks
   (`process_document_task`, `process_mediawiki_task`), worker
   entrypoint `python -m retriva.ingestion_api.worker`.  When
   `celery_broker_url` is empty the API falls back to FastAPI
   BackgroundTasks (optional-dependency pattern; Celery/Redis stay
   optional for unit tests and minimal deployments).
2. Job state today is non-authoritative and volatile:
   - in-process `JobManager` singleton (`job_manager.py`): dict +
     lock in the API process; lost on restart; holds the 202-level
     job records the API returns;
   - Redis keys written by the worker: `retriva:job:{id}` (state
     JSON, `setex` 7d), `retriva:retry:{content_hash}` (OOM-kill
     attempt counter, 24h), `retriva:cancel:{id}` (cooperative
     cancellation flag, 24h);
   - Celery result backend: effectively unusable — the dispatch
     call site discards the Celery task id and `get_task_status`
     falls back to `AsyncResult(job_id)` with the *job* id.
3. Dispatch today: the upload routers create the in-memory job
   (uuid hex id) and then `dispatch_document_task(payload)` →
   `.delay(**payload)`; the returned Celery task id is DISCARDED.
   The database-to-broker dual-write problem exists live: a crash
   between in-memory creation and dispatch (or a broker error after
   creation) leaves a permanently-pending phantom; an API restart
   erases every job.
4. Job model today (`Job`): id, status
   (pending/running/completed/failed/cancelling/cancelled), source,
   job_type, created_at, updated_at, error (RAW `str(exc)` —
   unsanitized), cancel_requested, current_stage, stages_completed,
   stage_detail, progress (0-100 within stage).  Response shape
   `JobResponseV2` exposes exactly these fields.
5. Status APIs today: GET `/api/v1/jobs` (list/get, in-memory only,
   cancel at `/api/v1/jobs/{id}/cancel`), GET `/api/v2/jobs` (list
   ALL in-memory v2 jobs merged with Redis state) and
   GET `/api/v2/jobs/{id}` — **no tenant scoping, no pagination**.
   The Core ingestion API carries no tenant concept (kb-scoped
   only).  Core has tenant context only as explicit graph-overlay
   parameters (CRM-driven).
6. Retry today: Celery `self.retry(exc, countdown=2**retries)` on
   exceptions; OOM-kill re-deliveries bypass Celery's retry counter
   (re-delivery via `task_reject_on_worker_lost`) and are counted in
   Redis with a 24h TTL.  No durable attempt history.
7. Cancellation today: `app.control.revoke(id, terminate=False)` +
   Redis flag polled by cooperative `cancel_check()` checkpoints;
   `cancelling` exists in the in-memory enum only.
8. The `jobs` PostgreSQL schema: created EMPTY by CRM V001
   (`CREATE SCHEMA IF NOT EXISTS jobs AUTHORIZATION retriva_migrator`,
   comment "Durable job tracking (later phases)"), with USAGE granted
   to the four CRM roles and future-table default privileges
   (SELECT/INSERT/UPDATE to `retriva_application`; SELECT to
   readonly/pgadmin_operator); CRM V001's down migration drops it
   `CASCADE`.  **No code in any repository reads or writes it**
   (CRM's own job tracking — `jobs.py`, `job_archive.py` — is
   SQLite).  It is a pure reservation owned (in PostgreSQL catalog
   terms) by `retriva_migrator`.
9. Spec 024 platform baseline (reused): provider contract and
   registry, deterministic stream ordering (topological, ties by
   provider then stream; `core.*` namespace reserved to the
   `retriva-core` provider), `platform.schema_migrations` ledger,
   advisory-locked migration one-shots in Compose, idempotent
   bootstrap, `retriva_core` runtime role (currently USAGE on
   `platform` + SELECT on the ledger only), `retriva_migrator` as the
   development-phase shared owner, forced-RLS conventions from the
   CRM store (`app.current_tenant`, fail-closed).
10. Legacy async flows inventory (verified call sites; full table
    with migration disposition in §3.11): v1 ingest routers
    (`chunks`, `text`, `html`, `markdown`, `image`, `pdf`,
    `pdf_upload`, `mediawiki`) and the v2 artifacts router all run
    FastAPI BackgroundTasks against the in-memory `JobManager`; the
    v1 and v2 job status routers read that same in-memory state
    (v1: in-memory only; v2: merged with Redis when Celery is
    enabled).

## 3. Requirements

### 3.1 Durable model (PostgreSQL, schema `jobs`, stream `core.jobs`)

- **jobs** — one row per logical job: `id` (uuid hex TEXT, repository
  standard), `tenant_id` (NOT NULL), `job_type` (NOT NULL),
  `payload_version` (NOT NULL, contract version of the job's input
  contract), `status` (state machine, §3.2), `subject_type`,
  `subject_id` (nullable typed subject reference), `input_metadata`
  (JSONB, repository-bounded), `result_metadata` (JSONB,
  repository-bounded, written only on success), `progress` (0-100,
  nullable), `progress_stage`, `progress_message` (bounded),
  `idempotency_key` (nullable), `requested_by` (nullable bounded
  actor reference), `queue` (nullable execution class),
  `execution_transport` (NOT NULL, `celery` | `local`; set at submit;
  §3.10), `scheduled_at` (nullable; set when entering `retry_wait`),
  `submitted_at`, `started_at`, `finished_at`
  (nullable; set on any terminal transition), `cancelled_at`
  (nullable), `cancel_requested_at` (nullable), `attempt_count`
  (NOT NULL default 0; authoritative durable count of created
  attempts), `max_attempts` (NOT NULL, snapshot of the retry policy
  at submit), `last_error_code`, `last_error_summary` (bounded),
  `celery_task_id` (diagnostic, nullable; the latest preallocated
  task id), `created_at`, `updated_at`, `purge_after` (nullable
  retention; snapshotted at the terminal transition, §3.9).  Primary
  key `id`; unique partial index on (`tenant_id`, `job_type`,
  `idempotency_key`) where the key is not null.
- **job_attempts** — one row per durable execution attempt: `id`
  (uuid hex), `job_id` (FK → jobs, ON DELETE CASCADE), `tenant_id`
  (NOT NULL, direct RLS), `attempt_no` (NOT NULL; allocated
  transactionally; unique with `job_id`), `dispatch_generation`
  (NOT NULL, monotonic per attempt, starts at 1),
  `dispatch_token` (NOT NULL, uuid, generated with the attempt),
  `celery_task_id` (nullable; PREALLOCATED at attempt creation and
  reused for every publication try of the same dispatch generation),
  `publication_state` (NOT NULL: `prepared` | `publishing` |
  `published` | `unknown` | `rejected`; §3.4), `published_at`
  (nullable), `publication_tries` (NOT NULL default 0; bounded),
  `execution_generation` (NOT NULL default 1; incremented on a
  proven-dead takeover of the same attempt), `worker_id` (bounded,
  nullable), `status` (queued/running/succeeded/failed/lost/
  cancelled/dispatch_failed), `dispatched_at` (first publish
  attempt), `started_at`, `finished_at`, `retry_class`
  (retryable/non_retryable/oom_requeue/operator_override/none),
  `error_code`, `error_summary` (bounded), `detail` (JSONB,
  bounded), created/updated.  Unique (`job_id`, `attempt_no`).
- **job_events** — append-only transition log: `id` (uuid hex PK),
  `seq` (BIGINT GENERATED ALWAYS AS IDENTITY; stable ordering
  anchor), `job_id` (FK, CASCADE), `tenant_id`, `event_type`
  (bounded set enforced by CHECK + registry, §3.14), `from_status`,
  `to_status`, `actor` (bounded: system/api/worker/operator),
  `attempt_id` (nullable), `detail` (JSONB, bounded, sanitized),
  `created_at`.  Ordering semantics: `ORDER BY seq` (monotonic,
  gap-tolerant); `created_at` is informational.  Justification:
  reconciliation evidence after crashes/restores, audit of
  business-critical async work (Constitution §30), and debugging of
  lost callbacks.  Retention-bounded; inserts only (§3.14).
- **Idempotency** is enforced by the jobs unique partial index; no
  separate record.  Semantics: repeating a submission with the same
  (tenant, job_type, idempotency_key) returns the EXISTING job
  (200 with its reference; the terminal job after completion);
  reusing the key with a different input identity is a clear
  conflict (409); keys are not reused for new executions while the
  job row exists, and the retention window of a job (§3.9) bounds
  how long its idempotency protection outlives it — protection can
  never expire earlier than the job record it protects.

### 3.2 Job state machine and transition table

States (only these, with observable semantics):

```
pending          submitted; no publication attempted
dispatching      attempt prepared (preallocated task id); publication in flight or outcome unwritten
queued           publication confirmed; awaiting/ready for worker claim
dispatch_unknown publication outcome ambiguous; must be resolved by evidence, never blindly reverted
retry_wait       durable retry scheduled (scheduled_at); no execution in flight
running          a worker claimed the durable attempt and is executing
cancelling       durable cancellation requested; execution fate being resolved
manual_review    bounded non-terminal review classification: outcome could not be determined from durable evidence
succeeded        TERMINAL
failed           TERMINAL
cancelled        TERMINAL
```

Every transition is a single guarded, predicate-based atomic UPDATE
(`WHERE status = <expected>` / rowcount checked) or row-locked
select-for-update; invalid transitions raise a typed domain error
and change nothing; every applied transition writes a `job_events`
row in the same transaction; duplicates are idempotent no-ops.
Terminal states are immutable: late or duplicate worker callbacks,
duplicate dispatch, or post-cancellation completions MUST NOT move a
terminal job or rewind any state (an attempt may record its own
terminal state; the job does not change).

Transition table (actor; guard; atomic effect; event; meaning;
duplicate behavior; reconciliation behavior — the latter cross-
references §3.8 classifications):

| #  | Transition | Actor | Guard | Atomic effect | Event | Meaning | Duplicate / reconciliation |
|----|------------|-------|-------|---------------|-------|---------|----------------------------|
| T1 | create → `pending` | api/system | idempotency miss | INSERT jobs(pending) + event | `job_created` | non-terminal | idempotency hit returns the existing job |
| T2 | `pending` → `cancelled` | api | `status='pending' AND cancel_requested_at IS NULL` | status=cancelled, cancelled_at | `cancel_requested`, `cancelled` | TERMINAL (nothing published, no external work) | duplicate cancel → idempotent 200; recon: none |
| T3 | `pending` → `dispatching` | dispatch executor (system) | `status='pending' AND cancel_requested_at IS NULL`, rowcount=1 | status=dispatching; INSERT attempt(`prepared`, attempt_no=attempt_count+1, dispatch_token, PREALLOCATED celery task id, generation=1) | `dispatch_prepared` | non-terminal | losing concurrent executor exits (rowcount=0); recon R1 |
| T4 | `dispatching` → `queued` | publisher (system) | attempt `publication_state='publishing'` | attempt: publication_state=`published`, published_at; job status=queued | `dispatch_confirmed` | non-terminal | duplicate confirm is a no-op (guard on publication_state); recon R1 |
| T5 | `dispatching` → `pending` | publisher (system) | DEFINITE pre-acceptance rejection evidence (§3.4 classes) | attempt: publication_state=`rejected`, status=`dispatch_failed`; job status=pending | `dispatch_rejected` | non-terminal, retryable; next dispatch creates a NEW attempt (new generation) | recon R1 re-dispatch |
| T6 | `dispatching` → `dispatch_unknown` | publisher (system) | AMBIGUOUS outcome evidence (§3.4 classes) or crash before the outcome write | attempt: publication_state=`unknown`; job status=dispatch_unknown | `dispatch_ambiguous` | non-terminal | MUST NOT revert to `pending`; recon R2 |
| T7 | `dispatch_unknown` → `queued` | worker claim evidence | delivery observed whose attempt id + task id + dispatch token match the current generation | attempt publication_state=`published` (confirmed by delivery), then claim path (T10) | `dispatch_confirmed` (delivery evidence) | resolution by evidence | recon R2 resolution |
| T8 | `dispatch_unknown` → `queued` | reconciliation republication | same dispatch generation; no cancel intent; stale threshold; publication_tries bound not exhausted | RE-PUBLISH with the SAME attempt id + SAME preallocated task id + SAME dispatch token; publication_tries+1; on confirm → `published`, job queued | `dispatch_republished` | same attempt reused; NOT a new execution attempt | recon R2; bound exceeded → R9 |
| T9 | `dispatch_unknown` → `manual_review` | reconciliation/operator | republication bound exhausted or contradictory evidence | status=manual_review | `reconciled` (classification) | non-terminal bounded review | operator resolution (T24) |
| T10 | `queued` → `running` | worker | claim predicate (§3.5): attempt `queued` AND job executable AND no cancel intent | attempt: status=running, worker_id, started_at, execution stamps; job status=running | `attempt_claimed` | non-terminal | exactly one claimant (atomic UPDATE rowcount=1); losers classify and exit |
| T11 | `queued` → `cancelling` | api | `status='queued' AND cancel_requested_at IS NULL` | cancel_requested_at=now; status=cancelling | `cancel_requested` | non-terminal | duplicate cancel idempotent; recon R3 |
| T12 | `running` → `cancelling` | api | `status='running' AND cancel_requested_at IS NULL` | same | `cancel_requested` | non-terminal | duplicate idempotent; recon R4 |
| T13 | `dispatching`/`dispatch_unknown`/`retry_wait` → `cancelling` | api | status IN (those) AND cancel_requested_at IS NULL | same | `cancel_requested` | non-terminal | duplicate idempotent; recon R3/R5 |
| T14 | `cancelling` → `cancelled` | worker / claim refusal / reconciliation | attempt evidence: never executed (claim refusal, §3.5) or cooperative ack at a checkpoint | attempt: status=cancelled, finished_at; job: cancelled, cancelled_at | `cancelled` | TERMINAL | a later success claim for the same attempt is refused by the attempt-terminal guard and logged as a bounded anomaly event; job stays |
| T15 | `cancelling` → `succeeded` | worker | attempt `succeeded` evidence — external work is KNOWN to have completed (cancel lost the race) | attempt succeeded; job succeeded, finished_at | `attempt_succeeded` (detail `cancel_lost_race`) | TERMINAL | the system MUST NOT report `cancelled` when side effects are known complete; duplicate no-op |
| T16 | `cancelling` → `failed` | worker | attempt `failed` evidence (work failed before/while stopping) | attempt failed; job failed | `attempt_failed` | TERMINAL | duplicate no-op |
| T17 | `running` → `succeeded` | worker | attempt running; execution success | attempt succeeded; job succeeded, finished_at, result_metadata | `attempt_succeeded` | TERMINAL | duplicate/late success → no-op (guard) |
| T18 | `running` → `failed` | worker | non-retryable failure, or retryable with attempt_count >= max_attempts | attempt failed (retry_class); job failed, last_error_code/summary, finished_at | `attempt_failed` | TERMINAL | duplicate/late failure → no-op |
| T19 | `running` → `retry_wait` | worker | retryable failure AND attempt_count < max_attempts | attempt failed (`retry_class=retryable`); job retry_wait, scheduled_at=now+backoff | `retry_scheduled` | non-terminal | duplicate no-op |
| T20 | `retry_wait` → `queued` | reschedule executor (system) | `status='retry_wait' AND scheduled_at <= now` | NEW attempt (prepared + preallocated id); publish; job queued | `retry_dispatched` | non-terminal | predicate prevents double reschedule; recon R6 |
| T21 | `failed` → `queued` | OPERATOR CLI ONLY (§3.12) | `status='failed'` AND (attempt_count < max_attempts OR durably recorded override) | NEW attempt; job queued | `operator_retry` (actor=operator) | non-terminal | duplicate invocation idempotent where practical (status guard + event) |
| T22 | `manual_review` → `succeeded`/`failed`/`cancelled`/`queued` | operator CLI | explicit operator resolution recorded durably | status per resolution (+cancelled_at/finished_at as applicable) | `operator_resolution` (actor=operator, bounded reason) | TERMINAL, except `queued` (operator-approved re-dispatch for restart-safe types) | duplicate resolution guarded by status predicate |

Rules:
- Terminal states: `succeeded`, `failed`, `cancelled`.  No rule
  leaves a terminal state.  `manual_review` is non-terminal but
  excluded from automatic resolution and age-based cleanup; only the
  operator exits it.
- Every event write is idempotent-by-transition: an event row is
  written only when its guarded transition applied (rowcount=1);
  duplicate callbacks emit nothing.  Evidence contradictions emit a
  bounded anomaly event at most once per attempt (§3.14).
- Cancellation is cooperative and durable: the API sets
  `cancel_requested_at` (+ status per the table); handlers check the
  durable flag at checkpoints; Celery revoke (`terminate=False`) is
  a best-effort transport aid only and NEVER guarantees
  interruption of running work (§3.3).
- Reconciliation transitions (§3.8) are the only system-driven
  non-callback transitions and are separately authorized.

### 3.3 Cancellation race semantics

Cancellation is durable intent (`cancel_requested_at`) evaluated at
deterministic durable-ordering points; Celery revoke is never treated
as an interruption guarantee.  Outcomes by state at cancellation
time:

- **pending** — nothing published: `cancelled` directly (T2).
- **dispatching** — publication in flight: `cancelling` (T13).  The
  publication outcome is then resolved WITH the cancel intent:
  confirmed publication → the attempt exists but is not claimed →
  the next claim is refused (§3.5) → `cancelled` (never-executed
  evidence); ambiguous → reconciliation invalidates the dispatch
  generation (no republication when cancel intent exists) →
  `cancelled` on never-executed evidence, or `manual_review` if
  execution cannot be excluded.
- **dispatch_unknown** — same as dispatching: `cancelling` (T13);
  republication is forbidden; resolution per the durable execution
  evidence (claim refusal → `cancelled`; execution happened →
  worker rules below).
- **queued** — no worker claim yet: `cancelling` (T11); a claim
  arriving after the cancel request is refused (durable ordering:
  `cancel_requested_at` predates the claim) → `cancelled` with
  never-executed evidence.  If the worker claim wins the race
  (claim commit precedes the cancel request), the job is `running`
  → `cancelling` (T12) and the execution rules apply.
- **retry_wait** — no execution in flight: `cancelling` (T13);
  the reschedule executor honors cancel intent → `cancelled`
  (never-executed evidence).
- **running** — `cancelling` (T12); the worker reaches cooperative
  checkpoints (cancel_check) and: stops before completing →
  attempt `cancelled` → job `cancelled` (T14); completes
  successfully before the next checkpoint → job `succeeded` (T15 —
  side effects are known complete; it MUST NOT be reported
  `cancelled`); fails → job `failed` (T16).  External work MAY
  complete even though local cancellation was requested: the durable
  attempt outcome is the evidence, and the job follows it.
- **Celery revoke requested but not guaranteed**: revoke
  (`terminate=False`) may prevent a not-yet-started delivery and
  never interrupts a running process; it is recorded as bounded
  diagnostic detail only.  Correctness never depends on it.
- **Duplicate cancellation requests** — idempotent (guard
  `cancel_requested_at IS NULL`); repeated cancels of an already
  `cancelling`/`cancelled` job return the current state (no error,
  no duplicate events).  Cancellation of a terminal job is a typed
  no-op conflict (existing semantics preserved).
- **Late success after a truly completed cancellation** (worker
  acknowledged cooperative stop, attempt `cancelled`, job
  `cancelled`) — the late success claim for the SAME attempt is
  refused by the attempt-terminal guard and logged as a bounded
  anomaly event; the job does NOT move.  It is not silently
  overwritten, and the job is not falsely claimed failed either.
- **Uncertain cancellation races** (execution lost without any
  terminal evidence while `cancelling`; contradictions between
  attempt evidence and job state): the job enters `manual_review`
  (T9/T22-adjacent; bounded, precise semantics: "outcome could not
  be determined from durable evidence; no terminal claim is made;
  operator resolution required").  `manual_review` exists precisely
  to avoid false terminal claims (`cancelled` when work may have
  completed, or `failed` when work may have succeeded).
- For every transition the actor, guard predicate, atomic update,
  event, terminal/non-terminal meaning, duplicate behavior, and
  reconciliation behavior are fixed in the §3.2 table.

### 3.4 Submission and the publication-state dispatch model

The two-phase dispatch design is retained; a transactional outbox is
NOT introduced in this phase (escalation option only, below).  A
publication exception or timeout is NEVER equated with proof that
the broker rejected the message.  Exactly four publication outcomes
are distinguished:

1. **definitely not attempted** — attempt `prepared`, no publish
   call (crash before the publish step, dispatch executor never
   invoked);
2. **definitely rejected before broker acceptance** — bounded
   transport exception classes that occur before the broker could
   accept (connect refusal, authentication failure at connect,
   client-side pre-acceptance rejection).  The mapping is an
   explicit, documented list in the transport adapter; every
   unmapped exception class defaults to AMBIGUOUS (fail-safe);
3. **confirmed** — the publish call returned normally (broker
   accepted);
4. **unknown / ambiguous** — timeout, connection reset mid-call,
   any unmapped exception, or a process crash between the attempt
   claim and the outcome write.

Durably persisted BEFORE publishing, in one database transaction:
the logical job identity; the durable attempt identity (attempt_no,
attempt id); the PREALLOCATED Celery task ID (uuid, generated
Core-side); the dispatch token and generation; the dispatch
timestamp; the publication state (`prepared` → `publishing`);
bounded correlation metadata (job type, tenant id, queue class,
payload version).  The preallocated task id is passed to
`apply_async(task_id=...)`; Celery is never waited on to allocate an
opaque id afterwards.  Celery task ids are unique per dispatch
generation.

Publication-state model (attempt-level, mirrored by job status):

```
prepared    publication definitely not attempted
publishing  publish call in flight (job status: dispatching)
published   publication confirmed (job status: queued)
unknown     publication outcome ambiguous (job status: dispatch_unknown)
rejected    definite pre-acceptance rejection (job returns to pending;
            attempt dispatch_failed; the generation is abandoned)
```

Required behavior:
- confirmed publication: `dispatching -> queued` (T4);
- definite failure before broker acceptance: `dispatching ->
  pending` (T5) — an explicitly retryable state; the abandoned
  attempt is terminal `dispatch_failed` and the next dispatch
  creates a new attempt;
- ambiguous outcome: `dispatching -> dispatch_unknown` (T6); an
  ambiguous outcome is NEVER blindly reverted to `pending`;
- reconciliation identifies and processes stale `dispatching`
  (crash before the outcome write) and `dispatch_unknown` records
  (R1/R2, §3.8);
- duplicate publication is safe (claim-based execution dedup);
- re-publication of the SAME dispatch generation reuses the durable
  attempt identity, preallocated Celery task id, and dispatch token
  (T8) — it does not create a new attempt or increment
  attempt_no; a genuinely new execution attempt always receives a
  new attempt identity and attempt number;
- terminal job and attempt state never move backward because of a
  late delivery or callback (§3.2 immutability);
- **no broker-level deduplication is claimed from task-id reuse**:
  reusing a task id provides correlation and traceability only.
  Brokers MAY deliver duplicates freely; the actual
  duplicate-execution protection is the durable, atomic worker-side
  claim of the attempt (§3.5) plus idempotent handler behavior
  (Constitution §26).

Submission flow: one transaction inserts the `jobs` row (`pending`)
and the creation event; NO broker call inside the transaction.
Post-commit dispatch (T3–T6) runs in the submitting process;
reconciliation covers crash windows.

**Outbox escalation option:** if implementation or validation later
proves this design cannot safely classify and reconcile publication
outcomes, a NARROWLY scoped transactional outbox for the jobs
submission transaction remains the documented escalation path; it
requires a dedicated relay and is out of scope otherwise.

### 3.5 Worker claim and duplicate-delivery protocol

The Celery task receives `(job_id, attempt_id, tenant_id,
dispatch_token, celery_task_id)` and loads extensions (existing
behavior).  Before ANY execution the worker runs the claim
protocol; execution without a granted claim is impossible by
construction (no Celery retry can create an execution outside the
durable attempt model).

Claim algorithm (single transaction; predicate-guarded UPDATEs;
rowcount decides):
1. Load the attempt tenant-filtered; verify consistency:
   `attempt.tenant_id = job.tenant_id = delivery tenant_id` and the
   delivery's dispatch token/generation match the attempt's current
   generation.  Mismatch → NO execution; deterministic
   classification (stale delivery) + bounded anomaly event.
2. Verify the job is executable: job status `queued` or
   `dispatch_unknown` (the claim itself is delivery evidence for
   T7) and `cancel_requested_at IS NULL`.
3. Claim by attempt state:
   - attempt `queued` (+ publication `published` or `unknown`) →
     claim granted: attempt → `running` (worker_id, started_at,
     execution stamps); job → `running` (T10);
   - attempt `queued` but cancel intent present / job `cancelling` →
     claim REFUSED: attempt → `cancelled` (never-executed
     evidence); job → `cancelled` (T14); delivery exits;
   - attempt `running`, same task id, same worker_id, live local
     execution record → duplicate delivery in-process → idempotent
     no-op exit;
   - attempt `running`, same task id, REDelivery evidence (§3.6
     OOM rules) → takeover protocol: if the prior execution is
     provably dead, mark the prior execution evidence `lost`, re-
     claim the SAME attempt (execution_generation+1, fresh
     started_at/worker_id) and proceed; if the prior worker is
     possibly still alive → NO execution; leave for reconciliation;
   - attempt `running`, different task id, same dispatch generation
     (republication delivery) → same takeover protocol; a stale
     generation (token mismatch) → no execution, bounded anomaly
     event;
   - attempt terminal (`succeeded`/`failed`/`lost`/`cancelled`/
     `dispatch_failed`) → idempotent no-op exit (no execution);
   - job terminal → no execution; attempt classified per evidence
     (usually `cancelled`), bounded anomaly event on contradiction.
4. Only one worker may claim an unclaimed durable attempt (atomic
   rowcount=1 UPDATE); a duplicate or late callback is idempotent;
   claim and state transition are concurrency-safe by predicate
   guards; the worker verifies job and tenant consistency BEFORE
   execution.

### 3.6 Attempt numbering, redelivery, and retry rules

- Attempt numbers are allocated transactionally; (`job_id`,
  `attempt_no`) is unique; the durable attempt count
  (`jobs.attempt_count` + rows) is authoritative.
- Celery retry counters (`self.request.retries`, task metadata) are
  DIAGNOSTIC ONLY and never a durable source of truth.
- A redelivery of the same durable attempt is NOT a new attempt.
  A new execution retry receives a new attempt row and number.
- Transport publication retry for the same attempt (republication of
  `dispatch_unknown`, in-generation publish retries) does NOT
  increment the execution attempt number; it is represented
  explicitly by `publication_tries` on the same attempt (bounded;
  bound exhaustion → R9).
- One attempt may have multiple publication tries; each is recorded
  without pretending it is an execution attempt.
- Celery task IDs are preallocated per dispatch generation and
  unique; late callbacks are matched to the durable job, attempt,
  task id, AND dispatch generation before any state change; non-
  matching deliveries are stale and never mutate state.
- **Durable retry scheduling:** the Celery `self.retry` mechanism is
  NOT the retry path.  A retryable failure durably sets
  `retry_wait` (T19) with `scheduled_at`; the reschedule executor
  (worker post-commit, fallback: reconciliation sweep R6) creates
  the next attempt and publishes (T20).  A `self.retry` redelivery
  of a terminal attempt is a claim no-op (no execution outside the
  model).  `max_attempts` is enforced from the durable snapshot.
- **OOM redelivery under `acks_late=True` +
  `task_reject_on_worker_lost=True`** — the message for an
  uncompleted `running` attempt is redelivered:
  - prior worker KNOWN lost (worker restart observed; worker_id
    differs; or `started_at` exceeds the stale threshold with no
    heartbeat): the prior execution evidence is marked `lost`
    (bounded detail), the SAME attempt is re-claimed
    (execution_generation+1) and executed once.  Duplicate
    execution is prevented by the atomic claim;
  - prior worker POSSIBLY still running (no dead evidence): the
    redelivering worker does NOT execute; the attempt stays
    `running`; reconciliation resolves by evidence (R7):
    restart-safe types → new attempt after the stale threshold;
    others → `manual_review`;
  - transitions to `lost`, retry, failure, or `manual_review` are
    evidence-based per the rules above.
- The Redis OOM counter (`retriva:retry:{hash}`) is NOT
  authoritative state; it is diagnostic-only and is retired with the
  integrated flow.

### 3.7 Progress

Durable columns (`progress`, `progress_stage`, `progress_message`)
mirror the existing user-visible semantics (stage name, completed
stage list stays in the stage metadata, bounded message, 0-100
within stage).  Writes: on stage change always; intra-stage updates
throttled (default: at most one durable progress write per 5 seconds
per job, configurable) to avoid write amplification.  Progress is
bounded and monotonic within a stage.

### 3.8 Reconciliation and operator commands

Operator-invoked, administrative-authorization commands (never
ordinary application credentials; no scheduler in this phase):

```
python -m retriva.jobs.reconcile [--dry-run (default)] [--apply]
                                 [--batch N] [--tenant T]
python -m retriva.jobs.cleanup  [--dry-run (default)] [--apply]
                                 [--batch N] [--tenant T]
python -m retriva.jobs.retry <job_id> [--reason S]
                              [--override-max-attempts]
```

Shared command properties:
- dry-run is the DEFAULT unless `--apply` is explicitly supplied;
- every run is bounded by a configurable batch size;
- repeated runs are safe and idempotent;
- global or cross-tenant operation requires explicit operational
  context (`--tenant` or an explicit operator scope flag); the
  operator identity is recorded in event `actor` classification as a
  bounded safe label (no secrets, no raw tenant data);
- every APPLIED reconciliation action is event-logged;
- uncertain external side effects are NOT automatically replayed —
  they are surfaced for manual review (`manual_review` + the run
  report);
- exit codes distinguish (documented): 0 clean completion / no
  changes; 3 changes applied; 4 unresolved manual-review items; 1
  operational failure;
- logs and metrics remain bounded and expose no secrets or tenant-
  sensitive values (aggregate counts only).

Reconciliation classifications (R-numbers referenced by §3.2):

| R | Condition (beyond threshold) | Default resolution |
|---|------------------------------|--------------------|
| R1 | `pending` / stale `dispatching` (`prepared`/`publishing`) / attempt `dispatch_failed` | re-dispatch; stale `dispatching` publishes the SAME generation if no cancel intent, else invalidates it |
| R2 | `dispatch_unknown` | resolve by evidence: delivery observed → `queued` (T7); republication with the SAME attempt + task id + token (T8), bounded tries; cancel intent → cancellation rules (§3.3); unresolved → `manual_review` |
| R3 | `cancelling` with attempt never claimed | `cancelled` (never-executed evidence) |
| R4 | `cancelling` with `running` attempt and no terminal evidence | `manual_review` (no false terminal claim) unless the type is restart-safe AND cancel intent allows only operator resolution — cancel intent is authoritative, so NO automatic replay |
| R5 | `cancelling` with lost/contradictory attempt evidence | `manual_review` (§3.3) |
| R6 | `retry_wait` past `scheduled_at` | reschedule (T20) — new attempt |
| R7 | `running` attempt stale, no callback | restart-safe types: mark prior execution `lost`, NEW attempt (re-dispatch converges to the true outcome); others: attempt `lost`, job `manual_review` |
| R8 | restored pre-terminal states (a PostgreSQL restore reintroduces `running`/`queued`/`retry_wait` for work whose side effects may postdate the backup) | conservative default: restart-safe types re-dispatch; others `manual_review`; automatic replay of externally visible side effects is forbidden without the restart-safe registry |
| R9 | attempt/job divergence (attempt terminal while job is not, or vice versa) or publication-try bound exhausted | reconciled to the conservative job-side state with an event record; unresolvable contradictions → `manual_review` |

Reconciliation is idempotent, event-logged, batched, and every
applied action is reported with aggregate counts.

### 3.9 Retention and cleanup

Development defaults, configurable: succeeded jobs 30 days, failed
jobs 90 days, cancelled jobs 90 days; attempts and events are
removed with their owning job (CASCADE).  Rules:
- retention configuration is applied when a job becomes terminal;
  `purge_after` is SNAPSHOTTED at the terminal transition;
- later configuration changes do not silently rewrite existing
  `purge_after` values; modifying existing retention requires an
  explicit maintenance operation (documented operator procedure via
  the privileged path);
- active and non-terminal jobs (including `manual_review`) are
  NEVER removed by age-based cleanup;
- idempotency protection cannot expire earlier than the job record
  it protects (the key lives with the row; §3.1);
- large external result artifacts have their own referenced
  lifecycle in their owning store; deleting a job row does not
  delete referenced artifacts, and they are not assumed deleted
  because the row is gone;
- cleanup is tenant-safe, bounded (batch deletes), observable
  (aggregate counts, no high-cardinality tenant labels), idempotent,
  and supports dry-run (default);
- cleanup of `job_events` executes only through the controlled
  privileged retention path (§3.14), never through runtime roles;
- cleanup remains a MANUAL development/operator command in this
  phase (`python -m retriva.jobs.cleanup`, §3.8); deployment
  manage.sh exposes `jobs-cleanup` (and `jobs-reconcile`) helpers.
  No scheduler is added.

### 3.10 BackgroundTasks fallback (local transport)

The existing development fallback is preserved ONLY as the same
PostgreSQL-authoritative lifecycle; it MUST NOT bypass durable
persistence, create a second state machine, or restore the in-memory
`JobManager` as an authoritative store.  The local/background
executor:
- creates the same durable job record (T1);
- creates and atomically claims a durable attempt through the same
  dispatch/claim protocol (T3, T10);
- uses the same transition service, progress path, sanitized failure
  handling, and cancellation checks (durable flag; Celery revoke
  does not exist for local transport);
- records `execution_transport='local'` on the job (and the
  API-process worker identity on the attempt) so provenance is
  explicit; the publication states map as: prepared → published
  (in-process schedule accepted) with `queued` remaining genuinely
  equivalent ("accepted for execution, awaiting the executor
  claim" — the local executor claims promptly; if the process dies
  first, reconciliation R7 classifies the stale attempt);
- preserves status across API process restart because state is in
  PostgreSQL (the BackgroundTasks function itself does NOT survive
  the crash — nothing pretends process-local execution survives);
- allows reconciliation to classify an attempt left running when
  the process dies (R7 with local-transport evidence);
- avoids duplicate local execution through the durable attempt
  claim.

One implementation, one state machine; `execution_transport` is the
only difference.

### 3.11 API, legacy inventory, and the durable/legacy boundary

**Durable API additions (v2, integrated flow):** GET `/api/v2/jobs`
(tenant-scoped, paginated, bounded filters by status/type), GET
`/api/v2/jobs/{id}` (tenant-authorized), POST `/api/v2/jobs/{id}/cancel`
(durable cancellation, replacing the Redis flag for the integrated
flow).  **NO manual retry on the unauthenticated public API** —
retry is operator CLI only (§3.12); acceptance asserts the public
surface exposes no retry route.  Responses keep the existing
`JobResponseV2` shape with additive fields only (attempt_count,
celery_task_id as diagnostic, safe error code).  Durable Core job
ids remain the public identifier; Celery task ids are diagnostics.

**Legacy inventory (verified call sites) and disposition:**

| # | Route / call site | Task / operation | State store today | ID format | Client-visible status endpoint | Cancellation today | Tenant today | Disposition |
|---|-------------------|------------------|-------------------|-----------|-------------------------------|--------------------|--------------|-------------|
| L1 | POST `/api/v1/ingest/chunks` (`routers/ingest.py:54`) | BackgroundTasks `process_chunks_in_background` | in-memory JobManager | uuid4().hex | GET `/api/v1/jobs[/{id}]` | POST `/api/v1/jobs/{id}/cancel` | none | DEFERRED (legacy) |
| L2 | POST `/api/v1/ingest/text` (`routers/ingest_text.py:64`) | BackgroundTasks | in-memory | uuid4().hex | `/api/v1/jobs` | `/api/v1/jobs/{id}/cancel` | none | DEFERRED |
| L3 | POST `/api/v1/ingest/html` (`routers/ingest_HTML.py:75`) | BackgroundTasks | in-memory | uuid4().hex | `/api/v1/jobs` | `/api/v1/jobs/{id}/cancel` | none | DEFERRED |
| L4 | POST `/api/v1/ingest/markdown` (`routers/ingest_markdown.py:80`) | BackgroundTasks | in-memory | uuid4().hex | `/api/v1/jobs` | `/api/v1/jobs/{id}/cancel` | none | DEFERRED |
| L5 | POST `/api/v1/ingest/image` (`routers/ingest_image.py:93`) | BackgroundTasks | in-memory | uuid4().hex | `/api/v1/jobs` | `/api/v1/jobs/{id}/cancel` | none | DEFERRED |
| L6 | POST `/api/v1/ingest/pdf` (`routers/ingest_pdf.py:163`) | BackgroundTasks (page) | in-memory | uuid4().hex | `/api/v1/jobs` | `/api/v1/jobs/{id}/cancel` | none | DEFERRED |
| L7 | POST `/api/v1/ingest/upload/pdf` (`routers/ingest_pdf.py:190`) | BackgroundTasks | in-memory | uuid4().hex | `/api/v1/jobs` | `/api/v1/jobs/{id}/cancel` | none | DEFERRED |
| L8 | POST `/api/v1/ingest/mediawiki` (`routers/ingest_mediawiki.py:86`) | BackgroundTasks | in-memory | uuid4().hex | `/api/v1/jobs` | `/api/v1/jobs/{id}/cancel` | none | DEFERRED |
| L9 | POST `/api/v2/artifacts` (`routers/v2_artifacts.py:127`) | BackgroundTasks `process_artifact_v2` | in-memory | uuid4().hex (artifact_id; job id separate) | GET `/api/v2/artifacts/{id}` (+ `/content`) | DELETE `/api/v2/artifacts/{id}` cancels | none | DEFERRED |
| L10 | GET `/api/v1/jobs`, GET `/api/v1/jobs/{id}` (`routers/jobs.py:24/32`) | status surface | in-memory + Redis merge | — | itself | — | none | DEFERRED with L1–L9 |
| L11 | POST `/api/v1/jobs/{id}/cancel` (`routers/jobs.py:73`) | cancel surface | in-memory + Redis flag + revoke | — | — | itself | none | DEFERRED |
| M1 | POST `/api/v2/documents` (`routers/v2_documents.py:1031`) | `process_document_task` (Celery) / BackgroundTasks | in-memory + Redis | uuid4().hex | GET `/api/v2/jobs[/{id}]` | v1-style | none | **INTEGRATED this phase** |
| M2 | POST `/api/v2/documents/upload` (`routers/v2_documents.py:1122`) | same | same | uuid4().hex | `/api/v2/jobs` | — | none | **INTEGRATED** |
| M3 | POST `/api/v2/documents/mediawiki` (`routers/v2_documents.py:1070`) | `process_mediawiki_task` (+ worker-side `mediawiki_v2_parser.py:292`) | same | uuid4().hex | `/api/v2/jobs` | — | none | **INTEGRATED** |
| M4 | GET `/api/v2/jobs`, GET `/api/v2/jobs/{id}` (`routers/v2_jobs.py:32/63`) | status surface | in-memory + Redis merge | — | itself | — | none | **MIGRATED to durable store** (compat projection) |
| M5 | worker tasks `process_document_task` / `process_mediawiki_task` (`tasks.py:146/265`) | execution protocol | Redis + in-memory touches | — | — | — | — | **MIGRATED** to the durable worker protocol |

**Exact durable/legacy boundary after this phase:**
- The integrated representative ingestion workflow (M1–M5) uses
  PostgreSQL as its ONLY authoritative logical job store; it does
  NOT write to legacy `JobManager` and PostgreSQL as co-authoritative
  stores (dual writes for the integrated flow are removed, including
  the worker-side JobManager touches and the Redis
  job-state/cancel keys).
- Compatibility adapters MAY project durable state into the old
  response shapes (M4 keeps `JobResponseV2` semantics additively)
  but MUST NOT create a second source of truth.
- Identifier handling: durable and legacy ids share the uuid4().hex
  FORMAT, so resolution is RULE-based, not format-based: the v2
  status routes consult the durable store FIRST and fall back to
  the legacy in-memory manager only for ids unknown to the durable
  store — deterministic precedence, documented for clients.  Because
  the integrated flow stops creating ids in the legacy manager, a
  v2-submitted id can only ever resolve durably; a collision between
  an unknown legacy id and a durable id remains cryptographically
  improbable AND is resolved deterministically by the precedence
  rule.
- v1 vs v2 status behavior is explicitly documented: `/api/v1/jobs`
  remains the legacy surface (in-memory only; it MUST NOT silently
  list cross-tenant durable jobs — no durable rows are exposed
  there); `/api/v2/jobs` is the durable, tenant-scoped, paginated
  surface.
- Removal plan (testable, bounded): L1–L11 migrate in a named
  follow-up phase through the same service contract; integration
  tests in THIS phase pin the boundary (durable-only for M-flows;
  legacy behavior unchanged for L-flows), making later removal a
  verifiable, bounded change.

### 3.12 Tenant resolution and trust model

No unauthenticated caller may select an arbitrary tenant.  Trust
model (explicit deployment posture, Constitution §36):

- **Fixed configured tenant (normal development posture):**
  `RETRIVA_JOBS_DEFAULT_TENANT` — a fixed, explicit, server-
  configured tenant identity that is MANDATORY when no authenticated
  tenant resolver is enabled; validated at startup (non-empty,
  bounded charset); applied SERVER-SIDE at the service boundary;
  unavailable for silent override by ordinary request input; and
  represented in logs only through safe configuration-mode
  information (mode + that a fixed tenant is configured), never the
  tenant value or tenant data.  This preserves compatibility for
  current local clients (they send no tenant input) without granting
  arbitrary tenant selection.
- **Authenticated trusted gateway model:** a tenant request header
  MAY be honored only when the deployment enables
  `RETRIVA_JOBS_TENANT_RESOLVER=gateway_header` AND a trusted
  authenticated gateway strips any untrusted external copy of the
  header and sets it server-side.  Trusted service identity MUST NOT
  rely solely on an externally forgeable header (Constitution §36).
- **Development-only override:** a clearly named development-only
  override (`RETRIVA_JOBS_TENANT_HEADER_OVERRIDE`) MAY be enabled
  ONLY with resolver `fixed`, ONLY in a constrained trusted/loopback
  environment, and it emits a PROMINENT startup warning; ordinary
  request input still cannot silently select tenants for production
  postures, and the override is disabled/ignored automatically when
  an authenticated trusted resolver is active.
- **Fail-closed:** the repository and service layers still require an
  established tenant context and FAIL CLOSED when trusted server-
  side resolution has not set it (Constitution §32; RLS enforced
  regardless of resolver).
- Operator surfaces (reconcile/cleanup/retry CLI) use an OPERATIONAL
  identity (explicit configuration or `--tenant`), never arbitrary
  caller-selected tenant context.

### 3.13 Roles, grants, schema transfer, and the downgrade guard

- `tenant_id` NOT NULL on all three tables from the first migration;
  forced RLS with policies on `app.current_tenant`; the repository
  sets the tenant context per transaction and fails closed when
  absent.
- Grants: `retriva_core` receives USAGE on schema `jobs` + SELECT/
  INSERT/UPDATE on the tables + sequence privileges; `retriva_core`
  has NO UPDATE/DELETE on `job_events` (§3.14); no Pro role receives
  any privilege on Core job objects; PUBLIC receives nothing; no new
  roles (development-phase shared migrator model from ADR-029
  stands).
- **Schema transfer (approved):** the existing empty `jobs` schema
  transfers to Core via the `core.jobs` stream with explicit Core-
  side adoption (create-if-absent, migrator-owned tables, RLS,
  grants), plus a NEW CRM-owned `pro.crm` V009 migration that cedes
  CRM claims idempotently: revoke CRM role USAGE on `jobs` (not
  required by CRM), revoke any explicit CRM privileges on Core job
  objects, and remove the CRM default privileges that would leak
  CRM access onto future tables/sequences/functions in `jobs`
  (ALTER DEFAULT PRIVILEGES ... REVOKE, idempotent).  NO applied CRM
  migration (V001 included) is edited.  The final catalog state
  converges regardless of valid migration order.
- **Migration-order matrix (tested, acceptance.md §B):**

| Path | Expected convergence |
|------|----------------------|
| `core.platform → core.jobs → pro.crm V001..V009` | core.jobs adopts; V009 cedes; final state F |
| `core.platform → pro.crm V001..V009 → core.jobs` | V009 cedes; adoption grants Core; final state F |
| `core.platform → pro.crm V001..V008 → core.jobs → pro.crm V009` | adoption between CRM migrations; V009 cedes; final state F |
| existing Spec 024 database → `core.jobs` → `pro.crm V009` | live-upgrade path; final state F |

  For EACH path verify: schema owner; table and sequence owners;
  runtime grants; default privileges; RLS policies; Core runtime DML
  access; DENIED CRM direct writes; idempotent rerun; and legacy
  migration-ledger integrity (pro.crm V001..V009 rows plus
  core.platform/core.jobs rows consistent, checksums unchanged).
- **CRM V001 down-migration hazard (documented explicitly):** CRM
  V001's down migration currently drops `jobs` CASCADE
  (`V001__foundation_schemas.down.sql`), which would silently remove
  Core-owned job objects and populated Core job history.
- **Destructive-downgrade guard (required):** a CRM destructive
  downgrade MUST REFUSE — with a distinct guard error that the
  generic `--confirm-destructive` flag does NOT satisfy — when any
  Core-owned job object or job data exists (predicate: any
  Core-owned relation/constraint in `jobs`, any rows therein, or
  ledger records showing `core.jobs` applied).  Removal of Core-
  owned job state requires a separate, explicit Core-owned operation
  FIRST: the `core.jobs` downgrade via the Core migration CLI
  (itself destructive and explicitly confirmed), which drops Core
  job objects and data while leaving the (CRM-reserved) schema in
  place; only then may the CRM downgrade proceed.
  - **Clean-database case:** a fresh database where `core.jobs` was
    never applied → no Core-owned job objects exist → the CRM
    downgrade proceeds as today (drops the empty reservation).
  - **Pre-Core compatibility case:** a database where CRM V001
    created the `jobs` reservation but `core.jobs` adoption never
    ran (or was itself downgraded) → no Core-owned objects → the
    CRM downgrade proceeds (drops the empty schema).
- No secrets, raw credentials, full payloads, raw exceptions, or
  unbounded stack traces in rows, logs, or errors; bounded columns
  and sanitized error codes; parameterized SQL only; job types
  resolve exclusively through a server-side registry (persisted
  strings never invoke code).

### 3.14 Event immutability

The `job_events` table is approved and its immutability is enforced
in the database:
- **append-only enforcement:** runtime roles cannot UPDATE or DELETE
  events (REVOKE from `retriva_core` and every non-owner role) AND a
  BEFORE UPDATE OR DELETE trigger raises an exception (defense in
  depth against future grants);
- **bounded event types:** a fixed set enforced by a CHECK
  constraint and the server-side registry (job_created,
  dispatch_prepared, dispatch_confirmed, dispatch_rejected,
  dispatch_ambiguous, dispatch_republished, cancel_requested,
  cancelled, attempt_claimed, attempt_succeeded, attempt_failed,
  attempt_lost, retry_scheduled, retry_dispatched, operator_retry,
  operator_resolution, reconciled, anomaly) — bounded metadata only:
  NO raw request payloads, credentials, stack traces, or arbitrary
  exception serialization;
- tenant RLS and fail-closed tenant context apply to events;
- deterministic linkage to the job and, where relevant, the attempt;
- ordering defined through `created_at` plus the stable `seq`
  identity (§3.1);
- duplicate callback handling does not create misleading duplicate
  events (events are written only when the guarded transition
  applies; contradictions emit one bounded `anomaly` event per
  attempt);
- deletion ONLY through the controlled privileged retention-cleanup
  path when the owning job is purged (§3.9);
- auditability of reconciliation, operator retry, cancellation,
  dispatch ambiguity, and terminal outcomes (Constitution §30).

### 3.15 Observability

Structured, bounded logging and counters for: jobs by status/type,
publication outcomes (confirmed/rejected/ambiguous counts),
dispatch latency, execution duration, retry counts, stale running
jobs, failed dispatches, terminal failures by safe error code,
reconciliation actions and classifications, cleanup actions,
manual_review backlog.  No tenant/customer/document/email values as
labels; correlation by durable ids only; cleanup/reconciliation
report bounded aggregate counts WITHOUT high-cardinality tenant
metric labels.

## 4. Integration scope (first workflow)

The v2 ingestion workflow (document + MediaWiki export tasks) is the
representative integration (largest existing async surface, already
Celery-backed, restart-safe pipeline).  It must prove: durable
submission; publication-state dispatch with preallocated task ids;
tenant/correlation context in workers; recorded attempts and
publication tries; durable progress; idempotent terminal success;
sanitized retry-aware failure; restart and Redis-expiry survival;
duplicate-submission/dispatch/redelivery safety; explicit durable
cancellation including race semantics; compatible endpoints; and
the BackgroundTasks fallback on the same lifecycle.  All other
workflows (legacy L1–L11; Pro submissions) are follow-ups via the
same service contract (§3.11).

## 5. Acceptance summary

Full criteria in acceptance.md; headline gates: domain/state-machine
tests (allowed/forbidden transitions, terminal immutability,
cancellation races), persistence tests (migration order matrix with
per-path convergence, ownership transition on a restored post-
Spec-024 database, RLS, grants, idempotency, event-immutability
probes), dispatch/worker tests (publication-state classification
including ambiguous outcomes, claim and duplicate-delivery races,
OOM redelivery rules, Celery-eager and a real-broker marked
integration), API tests (tenant trust model, no public retry,
pagination, sanitized failures), restore/reconciliation tests
(including manual_review semantics and exit codes), BackgroundTasks
fallback tests, container lifecycle tests (core.jobs applied after
core.platform in the core one-shot; Core-only stack healthy; Redis
loss preserves history; CRM destructive-downgrade guard), and
security probes (fail-closed tenancy, denied DDL, denied Pro
writes, denied event mutation).