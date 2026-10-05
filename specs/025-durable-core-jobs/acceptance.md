# Spec 025 acceptance

Status: ACCEPTED — revision 2 (with the pack, 2026-10-04; applies the
owner CHANGES_REQUESTED decisions; explicitly accepted for
implementation 2026-10-04).  The gates below define the IMPLEMENTATION
phase validation; each may be claimed only after the corresponding
validation is actually executed and recorded in §V.

## A. Domain / state machine (planned)

1. Every allowed transition applies and is event-logged.
2. Every forbidden transition raises a typed domain error and leaves
   no partial state.
3. Terminal immutability: late/duplicate callbacks, post-cancellation
   completion, and duplicate dispatch cannot move a terminal job;
   no state moves backward.
4. Cancellation-race matrix (spec.md §3.3) resolves each case to a
   deterministic outcome: cancel-before-claim → cancelled;
   claim-before-cancel → running/cancelling rules; cancel during
   dispatching/dispatch_unknown → cancelling with evidence-based
   resolution; worker success after a cancel request → succeeded
   (never falsely cancelled); late success after a completed
   cancellation → refused (job stays cancelled, bounded anomaly
   event); uncertain races → manual_review (no false terminal
   claims).
5. `manual_review` semantics: enters only from the specified
   evidence conditions; excluded from automatic resolution and
   age-based cleanup; exits only via operator resolution.
6. Retry limits enforced from the durable `max_attempts` snapshot;
   automatic retries never exceed it; operator override is durably
   recorded.
7. Progress bounded and monotonic within a stage; throttling
   verified.

## B. Persistence (planned)

1. Clean database: `core.jobs` applies after `core.platform`; all
   objects migrator-owned; grants/RLS/comments/trigger as specified.
2. Migration-order matrix (spec.md §3.13) — every path converges to
   the same final catalog state, each verified for: schema owner;
   table and sequence owners; runtime grants; default privileges;
   RLS policies; Core runtime DML access; denied CRM direct writes;
   idempotent rerun; legacy migration-ledger integrity:
   a. `core.platform → core.jobs → pro.crm V001..V009`
   b. `core.platform → pro.crm V001..V009 → core.jobs`
   c. `core.platform → pro.crm V001..V008 → core.jobs → pro.crm V009`
   d. existing Spec 024 database → `core.jobs` → `pro.crm V009`
3. Existing database (restored post-Spec-024 copy): ownership
   transition (CRM ceding migration + Core adoption) yields the
   identical final catalog state as a clean database; CRM V001
   checksum and ledger rows unchanged (no applied-CRM-migration
   edits).
4. Idempotent migration rerun (ledger no-op).
5. RLS: cross-tenant denial; fail-closed without tenant context;
   forced-RLS owner probe.
6. Runtime role (`retriva_core`) DML works on jobs/attempts; DDL
   denied; Pro roles denied any access to Core job tables (post-
   ceding state probed on every matrix path).
7. Idempotency unique index: repeat returns the existing job; key
   reuse with different input identity conflicts.
8. Guarded transitions survive concurrent actors (claim races).
9. Event immutability: UPDATE/DELETE denied to runtime roles;
   append-only trigger raises; bounded event_type CHECK; deletion
   only via the privileged retention path.

## C. Dispatch and workers (planned)

1. Successful submit → pending → dispatching (attempt prepared with
   preallocated Celery task id, dispatch token/generation, timestamps,
   publication state persisted BEFORE publishing) → queued (confirmed)
   → worker claim → running → succeeded, all durable.
2. Publication classification: confirmed / definitely-rejected-
   before-acceptance (mapped exception classes only) / ambiguous
   (timeout, reset, unmapped, crash window) / not-attempted; the
   definite class reverts to pending; the ambiguous class NEVER
   reverts blindly to pending (dispatch_unknown only).
3. Broker unavailable after commit: definite rejection path applies;
   job returns to pending; reconciliation re-dispatches (new
   attempt); no loss.
4. Ambiguous outcome: reconciliation resolves by evidence — delivery
   observed → queued; republication reuses the SAME attempt id,
   preallocated task id, and dispatch token (publication_tries
   bounded; no new attempt number); unresolved → manual_review.
5. Duplicate publication and duplicate deliveries: claim-based
   execution dedup; exactly one execution per attempt; duplicate/
   late callbacks are idempotent no-ops (no misleading events).
6. Worker claim protocol: single claimant; job/tenant consistency
   verified before execution; claim refusal under cancel intent →
   attempt cancelled + job cancelled (never-executed evidence);
   stale-generation deliveries classified without executing.
7. Worker started twice / success repeated / failure repeated:
   predicate guards keep the job consistent.
8. Retry: retryable failure → retry_wait (scheduled_at) → reschedule
   executor creates a NEW attempt and publishes; Celery retry
   counters diagnostic only; a `self.retry` redelivery of a terminal
   attempt executes nothing; max-attempts terminal enforced from the
   durable snapshot.
9. OOM redelivery (acks_late + reject_on_worker_lost): proven-dead
   prior worker → prior execution evidence `lost`, same attempt
   re-claimed (execution_generation+1), executed once; possibly-
   alive prior worker → no execution, reconciliation decides
   (restart-safe → new attempt; otherwise manual_review); the Redis
   OOM counter is never authoritative.
10. Worker crash / lost callback: redelivery takeover or
    reconciliation conservative closure per R7.
11. Tenant and correlation ids propagate API → broker → worker →
    rows/logs.
12. Cancellation: requested → cooperative acknowledgement; pending/
    queued/retry_wait jobs cancel without execution; Celery revoke
    treated as a non-guaranteed aid only.

## D. API (planned)

1. Tenant trust model: fixed configured tenant mandatory when no
   authenticated resolver is enabled; startup validation; applied
   server-side; ordinary request input cannot select tenants;
   gateway_header model requires the trusted-gateway stripping
   contract; the dev-only override is constrained (loopback/trusted)
   and emits a prominent startup warning; auto-disabled when an
   authenticated resolver is active; repository/service fail closed
   without server-resolved tenant context; logs carry configuration-
   mode information only (no tenant values).
2. NO manual retry route on the unauthenticated public API
   (asserted absent); operator CLI/admin surface only.
3. Tenant-authorized status retrieval; cross-tenant denial;
   pagination and filter bounds enforced.
4. Cancellation authorization and idempotency enforced.
5. Sanitized failures only (no raw exceptions, no payloads).
6. Existing ingestion endpoints and `JobResponseV2` shape preserved
   (additive fields); 202 contract unchanged; v2 status routes
   resolve ids durable-first with documented legacy fallback; v1
   endpoints unchanged and never list durable rows.
7. Legacy-boundary pinning: integrated flow writes ONLY the durable
   store (no JobManager/Redis dual writes); legacy flows L1–L11
   behavior unchanged.

## E. Restore and reconciliation (planned)

1. Stale `pending`/`dispatching`/`queued` re-dispatched
   at-least-once (R1); stale `dispatching` publishes the same
   generation when safe, invalidates it under cancel intent.
2. `dispatch_unknown` resolved by evidence or bounded republication
   (R2); unresolved → manual_review.
3. Stale `running` → restart-safe types re-dispatch (converge to the
   true outcome); others conservative `lost` + `manual_review` (R7).
4. Restored pre-terminal states classified conservatively (R8); no
   automatic replay of unproven side effects.
5. `cancelling` with uncertain evidence → manual_review (R4/R5);
   never a false terminal claim.
6. Attempt/job divergence reconciled with event records (R9).
7. Reconciliation rerun is idempotent; every applied action event-
   logged; dry-run default; batch bounds enforced; explicit
   operational context required for cross-tenant runs; exit codes
   distinguish 0 clean / 3 applied / 4 manual-review items / 1
   operational failure; logs and reports bounded (no secrets, no
   tenant-sensitive values, aggregate counts only).

## E2. Retention and cleanup (planned)

1. Defaults: succeeded 30d, failed 90d, cancelled 90d; attempts and
   events removed with their owning job.
2. `purge_after` snapshotted at the terminal transition; later
   configuration changes do not rewrite existing values; explicit
   maintenance operation required to modify.
3. Active and non-terminal jobs (incl. manual_review) never removed
   by age-based cleanup.
4. Idempotency protection never expires earlier than the job record
   it protects.
5. Referenced external artifacts are not assumed deleted by job-row
   deletion (their lifecycle is owned by their store).
6. Cleanup: tenant-safe, batch-bounded, idempotent, observable
   (aggregate counts, no high-cardinality tenant labels), dry-run
   default, manual command only (no scheduler); event deletion only
   via the privileged path.

## E3. BackgroundTasks fallback (planned)

1. The fallback uses the SAME durable job/attempt lifecycle (same
   submit, claim, transition service, progress, sanitized failures,
   durable cancellation checks) — no second state machine, no
   in-memory authority.
2. `execution_transport='local'` recorded; publication states map
   coherently (in-process schedule recorded; `queued` used only with
   genuinely equivalent semantics).
3. Durable status survives API process restart; a process death
   leaves the attempt to reconciliation (R7 with local evidence);
   duplicate local execution prevented by the durable claim.

## F. Container lifecycle (planned)

1. Core one-shot applies `core.platform` then `core.jobs` (Core-only
   stack healthy with the jobs schema).
2. Migration rerun no-op; restart preserves durable history.
3. Redis loss/flush does NOT delete PostgreSQL job history.
4. Pro path: core.jobs + pro.crm order independence with the ceding
   migration; CRM data and `pro.crm` history preserved.
5. CRM destructive-downgrade guard: refuses while Core-owned job
   objects/data exist (distinct guard error, NOT bypassed by the
   generic `--confirm-destructive`); clean-DB and pre-Core cases
   proceed; Core-owned state removal only via the explicit
   Core-owned `core.jobs` downgrade first.
6. No Core dependency on Pro migrations (Core-only path proven).

## G. Security (planned)

Per spec.md §3.12–§3.14 probes: tenancy fail-closed, DDL denial,
Pro write denial, event UPDATE/DELETE denial (privileges + trigger),
bounded fields, parameterized SQL, registry-only type resolution,
operator-only retry/reconciliation context, bounded sanitized
logging.

## V. Executed validation record

Recorded 2026-10-04 during implementation and the closure audit. All
commands executed successfully; pass counts are exact.

**Deterministic test suites (pytest):**

- `tests/test_jobs_domain.py` — 55 passed.
- `tests/test_jobs_persistence.py` — 26 passed (clean-DB migration +
  ledger; ownership/grant/default-privilege convergence; idempotent
  rerun; fail-closed DML; idempotency index; append-only enforcement;
  dispatch protocol T4–T8 incl. definite rejection and ambiguous
  never-reverting; claim protocol incl. duplicate delivery, takeover,
  cancel-intent refusal; cancel transitions T2/T11–T16 incl. cancel
  race (late success refused / cancel-lost-race success); durable
  retry T19/T20 + max-attempts bound; operator retry; R1/R2/R6/R7/R9
  reconciliation incl. same-generation republish; divergence
  adoption; dry-run no-op).
- `tests/test_jobs_dispatch.py` — 9 passed (fake-broker publication
  classification incl. definite/ambiguous classes and the
  unregistered-task ambiguous outcome; service flows; tenant resolver
  trust model; local-dispatch runner).
- `tests/test_jobs_api_v2.py` — 17 passed (no manual-retry route on
  the public surface; submit through the route durable + idempotent;
  fixed-resolver trust model; dev override loopback-constrained;
  tenant-scoped listing/filters/pagination bounds; cross-tenant
  404; legacy fallback boundary; cancel idempotent/terminal-409/
  manual-review-409/unknown-404; pending cancel → direct terminal).
- `tests/test_jobs_api.py` — 9 passed (v1 surface unchanged).
- `tests/test_v2_ingestion.py` + `tests/test_v2_acceptance.py` +
  `tests/test_mediawiki_v2_endpoint.py` — 18 passed (v2 endpoints run
  the durable lifecycle through the real app: 202 contract,
  JobResponseV2 shape, metadata propagation, stage data, upload
  path, mediawiki path).
- `tests/test_job_manager.py` — 5 passed (legacy boundary).
- Governance: `test_governance_registry.py`,
  `test_constitution_integrity.py`, `test_pg_platform_config.py`,
  `test_pg_migration_contract.py` — 43 passed (registry 025/030
  `accepted`; constitution integrity).
- CRM: `tests/test_pg_jobs_transfer.py` — 33 passed (the FULL
  migration-order matrix A/B/C/D with per-path owner/grant/RLS/DML/
  ledger probes; downgrade-guard refusal incl. WITH
  `--confirm-destructive` and the migrator-only credential posture;
  clean-DB and pre-Core reservation cases proceed; Core-owned
  downgrade drops only Core objects); `test_pg_migration_provider.py`
  + `test_pg_migrations.py` — 17 passed (8→9 revision assertions).
- Full final battery: core 188 passed, CRM 50 passed.

**Live deployment validation (real containers, real broker):**

- Clean-DB path: live `retriva` database recreated empty; core
  one-shot applied `core.platform` then `core.jobs` (v1); Core-only
  stack healthy; then Pro stack: CRM V001–V009, messaging bootstrap +
  Alembic + service healthy (Messaging compatibility on the
  centralized DB).
- Restored-copy path: faithful restore of the pre-upgrade dump
  (roles first; ownership/grants preserved) → bootstrap → core
  one-shot (old `audit.schema_migrations` ledger adopted;
  `core.jobs` applied) → CRM upgrade (pro.crm → 9) → verify: 31
  checks, 0 failures including `jobs_schema_ceded_to_core`.
- Real Celery durable job completion: submitted →
  `dispatch_prepared → dispatch_confirmed → attempt_claimed →
  attempt_succeeded → completed` through the real worker.
- Real ambiguous-publication recovery: a publication failure
  classified ambiguous (`task_not_registered`, incident remediated —
  see below) left `dispatch_unknown`; `manage.sh jobs-reconcile
  --tenant default --apply` performed R2 same-generation
  republication (exit 3) → job completed.
- Clean submissions require no reconciliation (dry-run exit 0,
  empty counts).
- Idempotent resubmission returned the SAME job id.
- Redis `FLUSHALL`: durable PostgreSQL history preserved.
- `manage.sh jobs-cleanup` dry-run exit 0 (nothing purgeable).
- Migration rerun no-op on the live stack (core and CRM one-shots:
  `applied: []`, exit 0).
- CRM downgrade guard refusal on the live DB (migrator-only
  container posture, WITH `--confirm-destructive`): exit 1,
  actionable error, pro.crm unchanged at 9.
- Container recreation (up -d recreate of ingestion/worker):
  durable history preserved.

**Closure-audit probes (2026-10-04):**

- Live catalog evidence: jobs schema owned by `retriva_migrator`;
  all jobs tables/sequences/indexes/function owned by
  `retriva_migrator`; RLS enabled AND forced on `jobs`,
  `job_attempts`, `job_events` with per-tenant policies; schema
  USAGE only to `retriva_migrator` (+CREATE) and `retriva_core`
  (USAGE, no CREATE); default privileges in schema `jobs` grant
  only to `retriva_core` (S: USAGE+SELECT; r: SELECT/INSERT/UPDATE —
  no DDL); CRM roles hold ZERO grants on jobs objects; PUBLIC holds
  no privileges on the schema.
- Live role probes: `retriva_core` DDL denied; no-context read
  fails closed (0 rows); wrong-tenant context denied; own-tenant
  rows visible (2); CRM role read denied (no schema USAGE);
  `retriva_core` UPDATE/DELETE on `job_events` denied by ACL;
  migrator UPDATE/DELETE denied by the append-only trigger;
  privileged cleanup path (GUC + migrator + tenant context)
  permitted.
- Isolated downgrade-guard probe (throwaway container, 5 cases,
  all passed): guard refusal with empty objects; with real job+event
  data (state snapshot byte-identical before/after: ledger, table
  grants, schema grants, default privileges, objects, row counts);
  ledger-row-alone drift state refused; migrator-only posture
  refused; reservation-only pre-Core case proceeds with the
  documented behavior (V009 down runs, empty reservation dropped,
  ledger 9→8, CRM usage re-granted by design).
- Closure-audit defect found and fixed in scope: R9 divergence
  resolution adopted `pending` for a lost attempt even after R7 had
  parked a non-restart-safe job in `manual_review`, which would have
  let a later R1 re-dispatch unproven work. Fixed: R9 now honors the
  conservative job-side state (manual_review is not un-parked;
  non-restart-safe divergence parks manual_review; only
  restart-safe types return to pending) per spec.md §3.9 R9.
  Affected suites rerun green (107 + 90 passed).

**Incident remediation (live, 2026-10-04):** a CRM downgrade with
`--confirm-destructive` ran V009 down BEFORE the guard fix (the
guard's admin-only credential probe silently no-opped in the
migrator-only migration-container posture) and re-granted CRM
privileges onto Core jobs objects. Remediation: the guard probe now
falls back to the migrator connection (catalog `to_regclass` probe
requires no object privileges); the CRM image was rebuilt,
`pro.crm` re-upgraded to 9 (revoking the leaked grants), and the
final live state was re-verified: `pro.crm` 9 applied; zero CRM
grants on jobs objects; zero CRM default privileges in schema
`jobs`; CRM USAGE on `jobs` revoked. Regression-protected by
`test_core_jobs_state_probe_fires_migrator_only`.

**Gateway disposition:** the accepted scope requires no gateway
route changes; the v2 durable submission/status routes are served by
the ingestion API (the owning Core service) and validation ran
directly against it (`http://localhost:8200`, the standard
development deployment entry point for the ingestion service). The
gateway's current 405/404 behavior for those paths predates Spec 025
(its documents router never had POST "" or /jobs routes) and Spec
025 changed zero gateway files.

**Known baseline failures (pre-existing, unrelated to Spec 025):**

- `tests/test_mediawiki_v2_parser.py` fails at collection:
  `ImportError: cannot import name 'COLLECTION_NAME' from
  'retriva.indexing.qdrant_store'`. Proven baseline: the test file
  is untouched since commit `854c3ec` and the symbol was removed by
  `9f8ffc5` ("Add support for multiple collections", 2026-07-12);
  both files are unmodified in the working tree
  (`git diff HEAD --stat` empty for both). Focused Spec 025
  MediaWiki endpoint integration tests (`test_mediawiki_v2_endpoint.py`)
  pass (2).