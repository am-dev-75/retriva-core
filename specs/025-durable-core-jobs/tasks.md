# Spec 025 tasks

Status: ACCEPTED — revision 2 (with the pack, 2026-10-04; applies the
owner CHANGES_REQUESTED decisions; explicitly accepted for
implementation).  Unchecked items are the accepted-scope
implementation plan.

## A. Governance (this pass)

- [x] Allocate Spec 025 + ADR-030 in the registry BEFORE first
      presentation (Constitution §43).
- [x] ADR-030 written with status PROPOSED (revision 2 applied).
- [x] This pack written with status PROPOSED (revision 2 applied).
- [x] Extend the governance registry test to assert Spec 025 /
      ADR-030 registration (`test_new_artifacts_are_registered`).
- [x] Revision 2: governed artifacts and registry notes updated per
      the owner CHANGES_REQUESTED decisions; statuses remain
      `proposed`; governance/specification checks rerun.
- [x] Owner acceptance decision recorded: **ACCEPTED** — Spec 025
      revision 2 and ADR-030 revision 2 explicitly accepted for
      implementation (2026-10-04), with the acceptance message's
      constraints; `manual_review` approved as bounded, non-terminal
      only.  Governance/constitution checks rerun green BEFORE
      implementation.

## B. Domain and persistence (after acceptance)

- [x] `retriva/jobs/domain.py`: statuses (incl. dispatching,
      dispatch_unknown, retry_wait, cancelling, manual_review),
      publication states, transition table (spec.md §3.2), records,
      sanitized error model, domain errors.
- [x] `retriva/jobs/registry.py`: server-side job-type registry
      (restart-safe flags, bounded event types).
- [x] `retriva/jobs/repository.py`: protocol + psycopg2 adapter
      (tenant context, guarded transitions, bounded columns,
      publication-state writes).
- [x] `core.jobs` stream `V001__jobs_foundation` (schema adoption,
      tables incl. dispatch-generation/token/preallocated-id/
      publication-state columns and the event `seq` identity, RLS
      FORCE + policies, grants incl. NO UPDATE/DELETE on job_events,
      append-only trigger + event_type CHECK, indexes, comments) +
      down migration (drops only Core-created objects).
- [x] CRM `pro.crm` V009 ceding migration (+ symmetric down;
      idempotent revocation of USAGE, explicit privileges, default
      privileges) — Pro repo.
- [x] CRM destructive-downgrade guard (refuses while Core-owned job
      objects/data exist; clean-DB and pre-Core cases allowed) — Pro
      repo downgrade path.
- [x] Platform CLI registers Core-owned streams (core.platform,
      core.jobs) without Pro imports.
- [x] Platform tenant-context helper (`SET LOCAL app.current_tenant`,
      fail-closed).
- [x] Tests: clean-DB migration, restored-copy transition, idempotent
      rerun, the FULL migration-order matrix (spec.md §3.13) with
      per-path owner/grant/default-privilege/RLS/DML/denial/ledger
      probes, downgrade-guard cases, ownership/grant/RLS probes,
      idempotency index, event-immutability probes, concurrency.

## C. Service, dispatch, Celery

- [x] Application service (submit/idempotency, publication-state
      dispatch: prepare → publish → classify, status/list/cancel;
      NO public retry).
- [x] Dispatch executor/publisher: preallocated Celery task ids,
      dispatch tokens/generations, definite-vs-ambiguous outcome
      classification (bounded exception-class map; unmapped →
      ambiguous), outcome persistence, same-generation republication.
- [x] Reschedule executor for `retry_wait` (durable retry; `self.retry`
      is not the retry path).
- [x] Celery task base (claim protocol incl. duplicate-delivery and
      takeover classification, throttled progress, sanitized terminal
      transitions, OOM redelivery rules, retry classification).
- [x] Tenant + correlation propagation (args → worker → rows/logs).
- [x] Local (BackgroundTasks) executor on the same durable lifecycle
      (`execution_transport='local'`; no second state machine).
- [x] Tests: fake-broker flows, publication classification unit
      tests, claim/takeover races, OOM rules, eager-mode task
      protocol, retry mapping, duplicate/late callbacks, real-broker
      marked integration.

## D. Ingestion integration

- [x] v2 upload + mediawiki + ingest submission through the durable
      service (BackgroundTasks fallback keeps the same protocol).
- [x] Jobs router: tenant-scoped list (pagination, filters), get,
      cancel; compat response shapes (additive fields); NO retry
      route on the public surface.
- [x] Tenant resolution module: fixed configured tenant (mandatory,
      startup-validated, server-applied, non-overridable by request
      input, safe mode-only logging), gateway_header trust model,
      constrained dev-only override with prominent startup warning,
      auto-disable under an authenticated resolver, fail-closed
      repository/service.
- [x] Idempotency default key from content identity; conflict rule.
- [x] Sanitized error summaries replace raw `str(exc)`.
- [x] Retire the Redis job-state/cancel keys and JobManager touches
      for the integrated flow; durable-first id resolution with
      documented legacy fallback; v1 endpoints unchanged (legacy).
- [x] Tests: API compat, tenant trust model, no-public-retry
      assertion, cross-tenant denial, bounds, sanitized failures,
      legacy-boundary pinning.

## E. Reconciliation, cleanup, operator CLI, observability

- [x] `python -m retriva.jobs.reconcile` (R1–R9 thresholds and
      classification, restart-safe gate, cancel-intent awareness,
      event-logged, idempotent, batched).
- [x] `python -m retriva.jobs.cleanup` (retention defaults 30/90,
      purge_after snapshot semantics, chunked, dry-run default,
      privileged event-cleanup path).
- [x] `python -m retriva.jobs.retry` (operator-only; retryable
      terminal states; durable override record for max_attempts).
- [x] CLI shared properties: dry-run default, bounded batches,
      explicit operational context for cross-tenant, event-logged
      applied actions, uncertain side effects surfaced for manual
      review (manual_review), exit codes 0/3/4/1, bounded logs.
- [x] Structured counters/logs (bounded labels; aggregate counts
      without high-cardinality tenant labels).
- [x] Tests: stale pending/dispatching/queued/running, dispatch_unknown
      evidence resolution, restored pre-terminal state, divergence,
      manual_review surfacing and operator resolution, idempotent
      rerun, exit codes.

## F. Deployment and container validation

- [x] manage.sh `jobs-reconcile` / `jobs-cleanup` helpers (dry-run
      default; `--apply` explicit).
- [x] Deployment tests: `core.jobs` applied by the core one-shot
      after `core.platform`; Core-only stack healthy; pro.crm order
      independence with the ceding migration; CRM downgrade-guard
      refusal + clean-DB/pre-Core cases on containerized scratch
      databases.
- [x] Clean-DB + restored-copy upgrade validation (scratch first,
      then live per plan.md §3).
- [x] Real container lifecycle validation (isolated projects).
- [x] Documentation (spec.md §22 list).

## G. Closure

- [x] Governed artifacts updated (pack ACCEPTED with evidence,
      ADR-030 ACCEPTED with the owner decision, registry statuses).
- [x] Truthful §25-format implementation report; no commit unless
      instructed.

Out of scope (binding): see spec.md §1 — no Celery/Redis
replacement, no DAG/event-bus/outbox-generalization, no scheduler,
no connector/GraphRAG/file migration, no Messaging/CRM workflow
integration, no tenant-facing retry API, no production hardening.