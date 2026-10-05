# Spec 026 — acceptance criteria

# Copyright (C) 2026 Retriva.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.  See the License for the specific language governing
# permissions and limitations under the License.

Status: **ACCEPTED** (2026-10-05). Sections marked "(planned)" are
gates; the executed record lands in §V only after implementation.

## A. Domain and registration (planned)

1. `v2_artifact` is registered with payload version `v2-1`,
   `restart_safe=False`, task name `process_artifact_task`, queue
   `ingestion`.
2. Unknown payload version fails safely (no arbitrary code execution
   from persisted strings; registry-only type resolution).
3. Payload reconstruction is complete (executes end-to-end from
   durable metadata alone), bounded (size/key limits), and
   secret-free.
4. Input fingerprint recorded; NOT used as an idempotency key (no
   content dedup — each submission is a new artifact).
5. Bounded input metadata: parameter/user-metadata size and key
   limits enforced; oversized → 400/422 class failure, nothing
   persisted raw.

## B. Persistence and tenancy (planned)

1. Artifact jobs are tenant-scoped rows in `core.jobs`; missing
   tenant context fails closed.
2. Cross-tenant status/content/cancellation denied (404 for another
   tenant's artifact id).
3. Idempotency column NULL; repeated identical submissions create
   DISTINCT jobs (current product behavior) — asserted.
4. Input/result metadata bounded and sanitized; no raw uploaded
   content, no raw exceptions, no provider responses persisted.
5. `job_events` remain append-only (existing enforcement; events
   written for the artifact lifecycle: job_created, dispatch_*,
   attempt_claimed, attempt_succeeded/failed, cancel events).

## C. Dispatch and workers (planned)

1. Celery: confirmed publication with preallocated task id;
   ambiguous publication → `dispatch_unknown`; R2 same-generation
   republication works (payload reconstructed from durable
   metadata); duplicate delivery is a claim no-op; atomic claim
   (single claimant); publisher-side task registration present.
2. Local: BackgroundTasks fallback runs the SAME handler and
   protocol; `execution_transport='local'` recorded.
3. No legacy co-authoritative write: after integration, NO
   JobManager/Redis record is created for artifact submissions
   (proven by test: JobManager stays empty post-submission).

## D. Artifact side effects (planned)

1. Successful run creates exactly one final output file via atomic
   finalization (no `.partial` residue on success).
2. Failed run leaves no final-path file; partial temp removed.
3. Crash after output creation but before success callback → job
   parked `manual_review` (non-restart-safe), output file intact and
   downloadable ONLY after operator resolution (download before
   resolution → not-ready semantics preserved).
4. Succeeded job with missing output → download 404; reconciliation
   records a bounded anomaly event; job not auto-failed.
5. Output present with uncertain state → `manual_review`; no
   automatic adoption or overwrite; never overwrite an artifact
   whose origin cannot be proven (final names are artifact-scoped;
   DELETE is the only remover).
6. Checksum recorded at finalization; mismatch (manual corruption)
   surfaces via download path behavior, not silent success.

## E. Progress and cancellation (planned)

1. Phases `fetching_data`/`rendering`/`finalizing` appear in the
   durable record with throttled updates (bounded write frequency).
2. Pending/queued cancel → cancelled without execution; running
   cancel → cooperative at the renderer checkpoint / before
   finalization; cancel racing finalization: either the cancel is
   honored (finalization skipped, partial quarantined) or the
   completed artifact wins the race (T15 semantics — never a false
   cancelled claim when output is proven complete); duplicate cancel
   idempotent; terminal no-op.
3. Celery revoke is a non-guaranteed aid only.

## F. Reconciliation and restore (planned)

1. Stale dispatch states, stale running, dispatch_unknown, R6 retry
   reschedule, R7 lost classification, R9 divergence — all reuse the
   accepted rules; artifact classification proven (non-restart-safe
   → manual_review; NO automatic provider-cost replay).
2. Idempotent reconciliation rerun (no duplicate actions).
3. Restore-time classification: restored pre-terminal artifact jobs
   → conservative classification; restored post-terminal + file
   present → download works; file missing → 404 + anomaly.

## G. API compatibility (planned)

1. `POST /api/v2/artifacts` → 202 with `{status, message, job_id,
   artifact_id}`; job_id is the durable id; artifact_id resolvable
   for status/content/delete.
2. Status projection preserves the existing status strings
   (`completed`/`running`/`failed` family) plus additive
   stage/progress fields.
3. Content: 200 (correct media type), 202 in-progress, 404 unknown/
   missing, 410 failed with sanitized detail.
4. DELETE idempotent 204; cancels the durable job.
5. Capabilities endpoint unchanged; /v2/discovery surfaces unchanged.
6. No public retry route; cross-tenant 404; sanitized errors only.
7. Pre-existing artifact jobs (in-memory era) remain 404 — identical
   to pre-change restart behavior (documented boundary; no hidden
   migration).

## H. Container lifecycle (planned)

1. Core-only stack: migration one-shot no-op; real Celery artifact
   completion; local fallback completion; Redis flush preserves
   durable status; API restart preserves status and download.
2. Pro stack compatibility (shared postgres/redis); Messaging
   compatibility; no new Core dependency on Pro.
3. Deployment repository requires NO change (proven, not asserted).

## V. Executed validation record

Recorded 2026-10-05. All commands executed successfully.

**Governance:** `tests/test_governance_registry.py` +
`test_constitution_integrity.py` + `test_pg_platform_config.py` +
`test_pg_migration_contract.py` — **43 passed** (registry 026/031
`accepted`; acceptance event recorded in ADR-031 §Status and
plan.md §5; four owner decisions recorded in spec.md §8).

**Focused deterministic suites (pytest, scratch PostgreSQL):**
- `tests/test_artifact_jobs.py` — **22 passed**: registration +
  restart-safe=False + task name + queue; idempotent task
  registration; bounded fingerprint-free payload reconstruction;
  unknown payload version fails safe (payload_version gate);
  media-type map; path traversal rejected; every submission = new
  artifact + new job (no idempotency key, fingerprint persisted,
  subject columns set, collection context server-resolved);
  cross-tenant subject lookup denied; result metadata SEPARATE from
  immutable input metadata; local-transport generation success with
  atomic finalization + checksum + provenance sidecar + bounded
  progress phases; invalid collection context fails safe
  (non-retryable, first-attempt terminal); render failure and
  missing renderer non-retryable; cooperative cancel during render
  → cancelled with partial quarantined; finalize-wins race (T15
  cancel_lost_race) then artifact deletion with job history
  intact; R7 crash-window ADOPTION with proven provenance
  (idempotent rerun); unproven/mismatched/wrong-job provenance →
  manual_review; R2 same-generation republication (same
  attempt/token/task id; fingerprint-free payload); duplicate
  delivery no-op (same worker); retention purge keeps the artifact
  file.
- `tests/test_v2_artifacts.py` — **13 passed**: API compatibility on
  the durable lifecycle (202 contract with durable job id; polling
  to `completed`; progress phases in the projection; markdown/pdf/
  docx/xlsx generation with deterministic media types and content
  verification; capabilities unchanged; unknown artifact 404s;
  every POST = new artifact+job; NO legacy JobManager write;
  unsupported format 400; no public retry route; cross-tenant
  status/content/DELETE denial; idempotent DELETE (finalized
  artifact removed, content 404/410/202 family); sanitized 410
  detail (raw exception text and private paths never exposed)).
- Full Core battery (jobs domain/persistence/dispatch/durable API/
  v1 API/artifacts/v2 ingestion/acceptance/mediawiki-endpoint/job
  manager/governance/constitution/platform-config/migration-
  contract) — **223 passed** (zero Spec 025 regressions).

**Real container validation (isolated Compose project
`spec026-audit`; dedicated network `spec026-audit-net`;
project-scoped volumes; container names and host ports overridden;
the live development stack untouched and verified intact after
teardown):**
- Core-only image build (`retriva-core-base:local`,
  `retriva-ingestion:local`); Core-only stack startup and health
  (postgres/redis/qdrant/tika/ingestion/worker/core/gateway/webui).
- Migration: fresh isolated DB → `core.platform` + `core.jobs`
  applied by the one-shot; migration rerun → `applied: []` (no-op).
- Real Celery artifact completion: submit → `pending` →
  `completed`; download 200 `text/markdown`; durable row
  `succeeded` with bounded result metadata (media_type + sha256).
- Redis `FLUSHALL`: durable status and download preserved.
- API restart (`docker compose restart`): status and download
  preserved.
- Cooperative cancellation: worker stopped → submit (queued) →
  DELETE 204 → durable intent (`cancelling`) → worker restart →
  claim refused → `cancelled` (T14). Finalize-wins race on the
  real stack → `completed` (T15; completion proven beats a racing
  cancel) with idempotent DELETE.
- Local BackgroundTasks fallback (CELERY_BROKER_URL empty,
  ingestion recreated): `execution_transport=local`, `succeeded`,
  download 200 — same handler, same lifecycle.
- Operator CLI: `python -m retriva.jobs.reconcile --tenant default`
  dry-run on the isolated stack → clean report, exit 0.
- Pro compatibility on the isolated DB: CRM bootstrap + migrate →
  `pro.crm` v9 alongside `core.jobs` v1; Messaging bootstrap +
  Alembic → `messaging` schema created; no Core dependency on Pro
  (Core-only stack ran without any Pro service).
- No new database migration (Spec 025 V001 reused as-is).

**Incidents during validation (documented, remediated):**
1. First isolated bring-up attached the project to the SHARED live
   network (`retriva-local-net` is a fixed-name network): one-shots
   resolved DNS across BOTH stacks and one test artifact row was
   written to the LIVE database. Remediated: teardown; network
   override corrected to a dedicated `spec026-audit-net`; the test
   row deleted from live (cascade attempts/events); live ledger and
   health re-verified intact (core.platform=1, core.jobs=1,
   pro.crm=9, zero v2_artifact rows); a second bring-up/teardown
   cycle re-verified complete isolation (0 spec026 containers on
   the live network). The corrected override is preserved in the
   validation evidence.
