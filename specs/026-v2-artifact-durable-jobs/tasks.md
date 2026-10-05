# Spec 026 tasks

Status: **ACCEPTED** — revision 1 (2026-10-05; explicit owner
acceptance recorded in the registry, ADR-031, and plan.md §5).

## A. Governance (this pass)

- [x] Allocate Spec 026 + ADR-031 in the registry BEFORE first
      presentation (Constitution §43); statuses `proposed`.
- [x] ADR-031 written with status PROPOSED.
- [x] This pack written with status PROPOSED.
- [x] Narrow governance-test update asserting 026/031 registration.
- [x] Owner acceptance decision recorded (registry statuses →
      accepted; acceptance event in ADR-031 + plan.md §5).

## B. Job-type contract and registration

- [x] Register `v2_artifact` (payload v2-1; restart_safe=False;
      task `retriva.ingestion_api.tasks.process_artifact_task`;
      queue `ingestion`) in `retriva/jobs/registry.py`.
- [x] Bounded payload contract validation (artifact_type/format/
      parameters/user_metadata bounds; fingerprint).
- [x] Payload reconstruction from durable metadata (generic path;
      version-gated; fail-safe on unknown versions).
- [x] Tests: registration, restart-safe flag, reconstruction
      completeness/bounds, unknown-version fail-safe.

## C. Execution handler (Celery + local, one lifecycle)

- [x] `artifact_handler` in `ingestion_api/durable_jobs.py`
      (recorder phases fetching_data/rendering/finalizing;
      cooperative cancel via renderer `cancel_check`; sanitized
      failures with retryable=False for handler/renderer errors).
- [x] Atomic finalization: temp `.partial` write → `os.replace`;
      quarantine of partial output on failure; sha256 + bounded
      result metadata (artifact_id, storage_ref, media_type, size).
- [x] Celery task `process_artifact_task` (claim protocol, duplicate
      delivery, OOM rules — same base as document/mediawiki tasks).
- [x] Local BackgroundTasks fallback through the same handler.
- [x] Tests: fake-broker publication; claim/duplicate; payload
      reconstruction; finalization race with cancel; late success
      after cancel request; missing/partial/duplicate output.

## D. Route integration and legacy retirement

- [x] `v2_artifacts.py` submission via the durable service (tenant
      resolution, bounded contract, 202 contract preserved; job_id =
      durable id).
- [x] Durable-first status projection into `JobResponseV2`
      (status mapping, phases, sanitized errors).
- [x] Content endpoint: 200/202/404/410 semantics; sanitized 410
      detail (raw exception text removed).
- [x] DELETE → durable cancel (best-effort revoke aid) + file delete;
      idempotent.
- [x] REMOVE every JobManager touch from this router (create/start/
      complete/fail/request_cancel/list-scan); keep JobManager for v1.
- [x] Tests: 202 contract, projection, cross-tenant denial, no
      public retry route, no legacy co-authoritative write.

## E. Reconciliation and restore proofs

- [x] Stale running `v2_artifact` → `lost` + `manual_review` (R7/R9
      conservative; no automatic provider-cost replay).
- [x] R2 same-generation republication for unclaimed dispatch.
- [x] Succeeded-with-missing-output anomaly event; not auto-failed.
- [x] Idempotent reconciliation rerun; restore-time classification.

## F. Deployment and container validation

- [x] No deployment change required — prove it (same image/queue;
      migrate one-shot unaffected; Pro compatibility; no Pro
      dependency).
- [x] Real Celery artifact completion; local fallback completion;
      Redis flush preserves status; API restart preserves status;
      ambiguous-publication reconciliation.

## G. Closure

- [x] Documentation (ingestion-api.md artifact section + governed
      evidence updates).
- [x] Governed artifacts updated with executed validation record.
- [x] Truthful implementation report; no commit unless instructed.

Out of scope (binding): see spec.md §3 — gateway, v1 flows, other v2
flows, JobManager removal, schedulers, session artifacts, retrieval
scoping product change (owner decision), UI redesign.
