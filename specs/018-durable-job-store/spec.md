# Spec 018 — Durable Job Store for Qualification Jobs

Status: proposed (created 2026-09-17 after the Unicode-URL reliability fix,
per archival clarification of the retry run of `job_09c46605d8804ff5`).
Priority: prerequisite infrastructure follow-up; no qualification or
URL-normalization logic changes belong in this milestone.

## Problem

The CRM qualification job registry (`retriva_crm_assistant.jobs.JobManager`)
is process-local and in-memory. Consequences observed in production:

- `job_249b350154a74d09` (failed with the pre-fix Unicode URL crash) became
  retrievable only until the process restarted; afterwards
  `GET /api/v2/crm/jobs/{id}` returned 404 forever.
- Retry lineage (`retry_of_job_id`) points to a job that may no longer
  exist, weakening the audit chain.
- The WebUI had to be taught to treat a 404 as a terminal `LOST` state as a
  mitigation; a durable store removes the root cause instead.

## Goals

1. Job records survive process/container restart (crash-consistent write on
   every state transition).
2. Job status API (`GET /jobs/{id}`, `GET /sessions/{sid}/jobs`) serves the
   same schema, now from durable storage.
3. Retry lineage remains resolvable for the retention window.
4. No change to qualification logic, thresholds, ACP/CCO, SearXNG circuit
   behavior, or URL normalization (ADR-017 boundaries preserved).

## Non-goals

- Cross-tenant multi-writer contention handling beyond current semantics.
- Migration of jobs lost before adoption (the failed original stays lost;
  its history is documented in the Unicode-URL report).

## Proposed design (to be validated in this spec's solution phase)

- Store: Redis hash per job (`retriva:crm:job:{job_id}`) with the existing
  `QualificationJob.to_dict()` payload, written on every transition via the
  existing `_lock`-protected write path; TTL = retention window (default
  30 days, configurable `CRM_JOB_TTL_SECONDS`).
- `JobManager` becomes a thin facade: in-memory fast path + durable
  write-through + read-through on 404-miss (registry and durable store
  reconcile by `updated_at`).
- Active-job scanning (`find_active_job`, `list_jobs`) moves to a Redis set
  index (`retriva:crm:session:{sid}:jobs`, `retriva:crm:jobs:all`) with
  best-effort eventual consistency; a background sweeper promotes
  stale-running jobs (no heartbeat > N seconds) to FAILED with a
  `job_store_recovered` note.
- Queue ownership conflict: Redis is already a runtime dependency
  (`retriva-redis` in docker-compose), so no new services.
- Result payloads (`/jobs/{id}/results`) stay artifact-backed as today;
  only job records move to durable storage.

## Acceptance criteria

1. Restart the ingestion container mid-qualification; after restart,
   `GET /jobs/{id}` returns the job with correct terminal/last-known state.
2. A job killed by container restart is reconciled to FAILED (not left
   RUNNING) after the heartbeat window.
3. A retry job's `retry_of_job_id` resolves to a readable record for the
   full retention window.
4. All existing tests pass; new tests cover restart-persistence,
   heartbeat reconciliation, and lineage resolution after restart.
5. Zero changes to files owned by the qualification pipeline.

## Explicitly out of scope of the deferred decision

THIS milestone must not bundle any URL-normalization or qualification
change. If implementation reveals a defect in those areas, it is filed as
a separate issue referencing this spec.
