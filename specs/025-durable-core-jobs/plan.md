# Spec 025 plan

Status: ACCEPTED — revision 2 (with the pack, 2026-10-04; applies the
owner CHANGES_REQUESTED decisions; explicitly accepted for
implementation).  Implementation proceeds through phases B–G below.

## 0. Revision record

Revision 1 (2026-10-04) was presented and returned CHANGES_REQUESTED
with owner decisions 1–14.  Revision 2 applies all of them: the
publication-state dispatch model (definite vs ambiguous outcomes,
preallocated task ids, dispatch tokens/generations); the tenant
trust model (fixed server-configured tenant; header only behind a
trusted gateway or a constrained dev override); the `jobs` schema
transfer with the migration-order matrix and the CRM destructive-
downgrade guard; retention defaults (30/90 days, purge_after
snapshot, no age-deletion of active jobs); operator-only retry
authorization; operator reconcile/cleanup/retry commands with
dry-run default and exit codes; cancellation race semantics with the
bounded `manual_review` classification; attempt numbering and
redelivery rules; append-only event enforcement; the BackgroundTasks
fallback on the same lifecycle; the verified legacy/durable
inventory and boundary.  Discovery findings were re-verified against
the repositories during revision; no contradictions were found.
Spec 025 and ADR-030 remain `proposed`; no new numbers allocated.

## 1. Current-state evidence index (verified 2026-10-04; re-verified at revision 2)

1. Celery app/config/queues/tasks/worker:
   `src/retriva/ingestion_api/celery_app.py`,
   `src/retriva/ingestion_api/tasks.py`,
   `src/retriva/ingestion_api/worker.py`;
   settings: `celery_broker_url`, `celery_result_backend`,
   `celery_task_max_retries` (3), `celery_task_soft_time_limit`,
   `celery_task_time_limit`, `celery_worker_concurrency` (1),
   `celery_worker_prefetch_multiplier` (1); compose `retriva-worker`
   runs `python -m retriva.ingestion_api.worker` on the `ingestion`
   queue; BackgroundTasks fallback when the broker is unset.
2. Volatile job state: `job_manager.py` (in-memory singleton; lost on
   restart), Redis `retriva:job:{id}` (7d TTL) / `retriva:retry:…`
   (OOM counter, 24h) / `retriva:cancel:…`; Celery result backend
   unusable (task id discarded at dispatch; `AsyncResult(job_id)`
   fallback mismatched).
3. Dispatch sites: `routers/v2_documents.py` (POST "" 1031,
   `/mediawiki` 1070, `/upload` 1122) → `create_job` → `.delay`
   with the discarded task id; dual-write exposure live.
4. Status APIs: `/api/v1/jobs` (list 24/get 32/cancel 73),
   `/api/v2/jobs` (list 32/get 63, merged with Redis); no
   tenancy/pagination.
5. Tenant context: absent from the Core ingestion path; present only
   as explicit graph-overlay parameters; CRM owns the
   `app.current_tenant` RLS helper pattern (fail-closed pools).
6. `jobs` schema: CRM V001 creates it empty (migrator-owned; USAGE +
   future-table default privileges for CRM roles — up.sql lines
   26/36-37/42/78-84; down.sql line 13 drops it CASCADE); zero code
   references in any repository; CRM job tracking is SQLite
   (`jobs.py`, `job_archive.py`).
7. Platform baseline (Spec 024): provider registry + `core.platform`
   stream + ledger + one-shots + `retriva_core`/`retriva_migrator`
   roles; deterministic topological stream ordering with the
   `core.*` namespace reserved to the `retriva-core` provider;
   extension providers load via `RETRIVA_PG_MIGRATION_PROVIDERS`;
   the core migrate one-shot currently applies `core.platform` only
   — Core-owned streams register through the Core CLI (Core→Core
   import), so `core.jobs` requires no Compose change.
8. Legacy flows (full inventory in spec.md §3.11): v1 ingest routers
   (`routers/ingest.py:54` chunks, `ingest_text.py:64`,
   `ingest_HTML.py:75`, `ingest_markdown.py:80`, `ingest_image.py:93`,
   `ingest_pdf.py:163` + `:190` upload, `ingest_mediawiki.py:86`),
   v2 artifacts (`v2_artifacts.py:127` + status/content/delete
   161/184/223) — all BackgroundTasks + in-memory JobManager; worker
   side touches (`tasks.py:246`, `mediawiki_v2_parser.py:292`).
9. Quality gates: repository-standard pytest suites (core, CRM,
   messaging, deployment), `docker compose config`, `bash -n`,
   compileall, ruff (messaging), governance registry test; no CI
   system exists (gates run locally; same as Spec 024).
10. `AGENTS.md` mission blocks: pre-existing Spec 014 block in
    retriva-core (unrelated; untouched); no changes made.

## 2. Implementation phases (after acceptance)

### Phase A — Governance completion (DONE)
Registry entries (done pre-presentation per §43), ADR-030 PROPOSED
(done; revised in revision 2), this pack (revised), registry-test
extension asserting the new artifacts are registered (done), owner
review CHANGES_REQUESTED (revision 1), revision 2 re-presentation,
**explicit owner acceptance recorded 2026-10-04** (Spec 025 and
ADR-030 revision 2 accepted; `manual_review` approved as bounded and
non-terminal only) → status ACCEPTED; registry statuses updated.

### Phase B — Domain and persistence
`retriva.jobs` domain (state machine incl. dispatching/
dispatch_unknown/manual_review, publication states, records,
sanitized errors), repository protocol + psycopg2 adapter (tenant
context, guarded transitions, bounded columns), `core.jobs` stream
V001 (adoption + tables + RLS + grants + append-only event trigger +
CHECK + indexes + comments) + down migration (drops only Core-
created objects), CRM `pro.crm` V009 ceding migration (+ down) and
the CRM destructive-downgrade guard (Pro repo), platform CLI Core-
stream registration, platform tenant-context helper, focused tests
including the full migration-order matrix and guard cases.

### Phase C — Service, dispatch, Celery
Application service (submit/idempotency, publication-state dispatch
with preallocated task ids/tokens/generations, definite-vs-ambiguous
classification, status/cancel), reschedule executor (durable retry,
not `self.retry`), Celery task base with the claim/takeover/duplicate-
delivery protocol, throttled progress, sanitized terminal
transitions, tenant + correlation propagation, local (BackgroundTasks)
executor on the same lifecycle, fake-broker and eager-mode tests.

### Phase D — Ingestion integration
v2 upload/mediawiki/ingest submission through the durable service
(BackgroundTasks fallback keeps the same protocol), jobs routers
(tenant-scoped list with pagination/filters, get, cancel; NO public
retry), compat response shapes (additive fields), tenant resolution
module (fixed tenant mandatory/validated; gateway_header; constrained
dev override with startup warning; fail-closed), sanitized errors,
idempotency via content-hash default key, removal of the Redis
job-state/cancel keys and JobManager touches for the integrated flow,
legacy-boundary pinning tests, API tests.

### Phase E — Reconciliation, cleanup, operator CLI, observability
`retriva.jobs.cli` (reconcile / cleanup / retry; dry-run default,
bounded batches, explicit operational context, event-logged applied
actions, exit codes 0/3/4/1), R1–R9 classification, restart-safe
gate, retention purge via the privileged event-cleanup path,
structured counters/logs (bounded labels, aggregate counts),
deployment manage.sh helpers `jobs-reconcile` / `jobs-cleanup`.

### Phase F — Deployment and container validation
Deployment tests (core.jobs in the core one-shot, Core-only stack,
schema ownership assertions, order matrix on containerized scratch
databases, CRM downgrade-guard behavior); clean-DB and restored-copy
upgrade validation; real container lifecycle; docs.

### Phase G — Final report
Truthful §25-format report; no commit unless instructed.

## 3. Data-safety procedure

The preserved pre-upgrade backup (`/mnt/devel/retriva-backups/
retriva-pg-2026-10-04-preupgrade.dump`, sha256 937bb162…) remains the
rollback anchor.  Migration validation runs against scratch
databases and a restored post-Spec-024 copy FIRST; the live
development database is upgraded only after the copy validates; the
live volume is never recreated.  The CRM ceding migration and the
downgrade guard are validated on CRM-path databases (pro.crm
applied; with and without core.jobs) before any live run.

## 4. Rollback

Code revert across the repos; the `core.jobs` stream's down migration
drops only Core-created job objects (never the schema itself — the
schema pre-exists as the CRM reservation; documented); CRM V009's
down restores the CRM-side grants symmetrically.  The CRM downgrade
guard prevents any CRM downgrade from silently removing Core job
history while Core job objects/data exist: the explicit Core-owned
`core.jobs` downgrade runs first (operator-confirmed), then the CRM
downgrade is unblocked.  The job history tables are additive;
reverting code leaves them in place and inert.

## 5. Follow-up inventory (documented, not in scope)

Legacy flows L1–L11 migration (spec.md §3.11) and v1 job endpoint
retirement; Pro workflows (CRM/Messaging) adopting the Core jobs
service; tenant-facing retry API after an authenticated principal
and authorization model exist; production scheduling of
reconciliation/cleanup (cron/beat); production retention policy and
the explicit maintenance operation for existing purge_after values;
cross-schema reference contracts (Pro rows referencing Core job
ids); narrowly scoped transactional outbox if publication outcomes
ever prove unclassifiable (spec.md §3.4 escalation option).

## 6. Constitution checkpoints

§42 (this pack; acceptance before implementation; CHANGES_REQUESTED
re-presentation), §43 (registry first; deterministic check extended;
statuses remain proposed), §20 (store-of-record declared in
ADR-030), §26/§27 (idempotent, resumable movement; attempt and
publication semantics), §30 (audit trail via append-only job_events),
§32 (tenant_id from the first migration; trimming in RLS; fail-closed
tenancy), §33/§34 (no content/secret leakage; sanitized errors),
§36 (explicit deployment posture: fixed dev tenant, constrained
override, no hidden fallbacks), §39 (deterministic tests; real
PostgreSQL/Redis where required), §40 (baseline failure discipline —
the proven core/CRM baseline failure sets remain the reference), §17
(no Pro imports in Core; Pro adopts via stable contracts), §44
(scope binding: this revision changes only governed artifacts), §45
(licensing boundary preserved).

## 7. Owner-authorization note

The tasking brief ordered a governed PROPOSAL for the first pass;
revision 1 was reviewed with CHANGES_REQUESTED; revision 2 was
re-presented and the owner **explicitly accepted** Spec 025 revision 2
and ADR-030 revision 2 for implementation (2026-10-04), approving
`manual_review` as a bounded, non-terminal state only.  Implementation
is now authorized within the accepted scope and constraints (twenty
explicit implementation constraints recorded in the acceptance
message); a material architectural change during implementation stops
work and returns the pack to CHANGES_REQUESTED for owner review; no
commit/push/merge/tag/release without explicit instruction.