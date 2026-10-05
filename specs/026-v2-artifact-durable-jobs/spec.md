# Spec 026 — Durable v2 artifact generation jobs (Spec 025 extension)

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

Status: **ACCEPTED** — revision 1 (2026-10-05). Allocated per
Constitution §43 before first presentation as PROPOSED; presented the
same day; the owner **explicitly accepted** Spec 026 and ADR-031 for
implementation with the four owner decisions and the binding
clarifications recorded in §8 and the ADR status section. The
companion ADR-031 is Core-owned
(`retriva-core/docs/adr/adr-031-v2-artifact-durable-jobs.md`).

Registry: `docs/governance/spec-adr-registry.yaml` → specification
026 (`accepted`), ADR-031 (`accepted`).

## 1. Summary and problem statement

The v2 artifact generation workflow (`/api/v2/artifacts`: render a
`document_list` or `basic_report` artifact to pdf / markdown / docx /
xlsx / odt / ods / odp) is the last remaining v2 async workflow whose
job state is ephemeral. Verified current facts:

- job state lives ONLY in the in-process `JobManager` singleton
  (lost on API restart; `GET /api/v2/artifacts/{id}` returns 404
  after a restart even when the rendered file exists);
- execution runs exclusively through FastAPI BackgroundTasks inside
  the API process (no Celery task is registered for artifacts, so the
  deployment cannot spread artifact rendering to workers);
- the renderer writes DIRECTLY to the final output path (no temp
  file, no atomic rename): a crash mid-render leaves a partial file
  at the final location;
- the raw exception string is stored via `JobManager.fail_job` and
  surfaced in the download 410 detail (violates the bounded
  sanitized-error contract applied to every other v2 workflow by
  Spec 025);
- there is no attempt model, no retry, no reconciliation, no event
  log, no retention, and no tenant scoping on the artifact job
  records.

Spec 025 §4 explicitly deferred this flow ("all other workflows
(legacy L1–L11; Pro submissions) are follow-ups"). This specification
migrates exactly this one flow onto the accepted Spec 025 durable
Core jobs subsystem — no other workflow is touched.

## 2. Goals (binding)

1. PostgreSQL (the `core.jobs` stream created by Spec 025) is the
   ONLY authoritative logical job store for the v2 artifact
   workflow: submission, status, progress, cancellation, attempts,
   events, and reconciliation all flow through the existing durable
   jobs service/repository/state machine.
2. The durable job id becomes the primary public job identifier and
   remains the `job_id` returned by `POST /api/v2/artifacts`.
3. Celery and the BackgroundTasks fallback execute the SAME durable
   artifact lifecycle (one handler, one claim protocol, no second
   state machine).
4. Output finalization becomes atomic (temp write + `os.replace`)
   so a partial render can never masquerade as a complete artifact.
5. Errors surfaced to clients are bounded and sanitized; raw
   exception text is never stored in user-visible fields.
6. Legacy `JobManager` writes are removed for this workflow only;
   `JobManager` remains for the out-of-scope v1 flows.
7. No new job state machine states; the accepted Spec 025
   transitions (T1–T24) are reused as-is.
8. No new database migration is required (the Spec 025 V001 schema
   already stores everything this flow needs: bounded
   `input_metadata` JSONB, `result_metadata`-style bounded payload
   keys inside `input_metadata`/progress detail, events, attempts).

## 3. Non-goals / out of scope (binding)

- Gateway proxying of the v2 artifact routes (outside scope; the
  ingestion API remains the direct entry point).
- The pre-existing `tests/test_mediawiki_v2_parser.py` baseline
  failure (unrelated; untouched).
- Migration of the legacy v1 ingestion jobs (L1–L11) or any other
  v2 workflow.
- Removal of the shared legacy `JobManager` (still used by v1).
- Session artifacts (`retriva/session/artifacts.py`, `SessionStore`,
  TTL sweep) — a separate, already-persistent feature; untouched.
- Authenticated tenant/principal redesign, public retry API,
  automatic schedulers, transactional outbox, workflow DAGs.
- Product redesign of artifact types or renderer capabilities.
- Retrieval scoping changes for `fetch_artifact_data` (currently
  unscoped, collection-wide; preserved as-is — flagged in §8).

## 4. Integrated workflow

Exactly one: v2 artifact generation (`POST /api/v2/artifacts` with
`artifact_type ∈ {document_list, basic_report}`, `format ∈ {pdf,
markdown, docx, xlsx, odt, ods, odp}`, bounded `parameters` dict).
Every artifact job executes the full chain: durable submit →
dispatch → claim → fetch data (`fetch_artifact_data`) → render →
atomic finalize → terminal success with a durable result reference.
Cancellation, retry, events, reconciliation, retention: reused from
Spec 025 unchanged.

## 5. Job-type contract

Registered server-side in `retriva.jobs.registry`:

| field | value |
|---|---|
| job_type | `v2_artifact` |
| payload version | `v2-1` |
| subject | `artifact:<artifact_id>` (the artifact id is generated server-side at submission and stored bounded in `input_metadata.artifact_id`) |
| celery task | `retriva.ingestion_api.tasks.process_artifact_task` |
| queue | `ingestion` (same queue; no new queue) |
| execution_transport | `celery` when the broker is configured, else `local` (identical lifecycle; Spec 025 §3.4) |
| restart_safe | **False** — a provider-cost operation (`basic_report` invokes the LLM answerer) whose uncertain replay the system must never perform automatically; stale running → attempt `lost` + job `manual_review` (R7/R9 conservative) |
| max_attempts | `RETRIVA_JOBS_MAX_ATTEMPTS` default (3), retryable classification below |
| retryable classes | transport/dispatch classification per Spec 025 §3.4; handler failures (renderer missing, invalid format/type, provider failure) are NON-retryable (`retryable=False` → terminal `failed`) because rendering is deterministic given the same input — a retry would deterministically fail again or repeat provider cost |
| cancellation | cooperative; checkpoint after data fetch (renderer `cancel_check`) and before finalization |
| progress phases | registered bounded phases: `fetching_data` → `rendering` → `finalizing` (recorder-throttled) |
| result metadata | bounded keys in `input_metadata.result`: `artifact_id`, `storage_ref` (relative path), `media_type`, `size`, `sha256` |
| idempotency key | none by default (artifact generation is inherently non-idempotent: each submission creates a NEW artifact — current product behavior preserved); the Spec 025 idempotency column stays NULL; no content-derived key exists |
| retention | standard Spec 025 defaults (succeeded 30d / failed 90d / cancelled 90d); `result.storage_ref` rows are cleaned with the job row (the referenced file's lifecycle remains owned by its store — same rule as Spec 025 E2.5) |

Payload contract `v2-1` (bounded; the full reconstruction source for
both fresh dispatch and R2 same-generation republication):

```
input_metadata = {
  artifact_id: <hex>,            # server-generated subject id
  artifact_type: <str ≤ 64>,     # registered type name
  format: <str ≤ 16>,            # registered format key
  parameters: {… ≤ 64 keys, bounded string/number/bool values},
  user_metadata: {… ≤ 32 keys, bounded strings},   # passthrough, sanitized
  input_fingerprint: <sha256>,   # bounded fingerprint of the above
}
```

`input_fingerprint` = sha256 over the canonical JSON of
`(artifact_type, format, parameters)` — recorded for evidence; NOT
used as an idempotency key (see above).

Task payload reconstruction (service-level, same mechanism as
`task_payload_from_metadata`): `{artifact_id, artifact_type, format,
parameters, user_metadata}` — complete, bounded, secret-free,
reconstructable for R2 republication of the SAME generation. The
registry rejects unknown payload versions (fail safe, no arbitrary
code execution from persisted strings).

## 6. Input, output, and storage lifecycle

| object | location | lifetime | ownership |
|---|---|---|---|
| logical job metadata | `core.jobs` PostgreSQL row | retention defaults | Core durable jobs |
| input identity | `input_metadata` (bounded JSONB) | job lifetime | durable |
| temporary render file | `<base>/<artifact_id><ext>.partial` in the LocalStorageProvider directory | attempt lifetime; removed or renamed at finalization | handler |
| generated output artifact | `<base>/<artifact_id><ext>` (final name; atomic `os.replace`) | until operator DELETE (or, if the owner accepts, job purge — open decision §8) | artifact store |
| result reference | `input_metadata.result` (bounded: storage_ref/media_type/size/sha256) | job lifetime | durable |

Base directory: `LocalStorageProvider` (same path derivation as
today: `Path(settings.artifacts_path).parent / "collections" /
<collection> / "artifacts"`). The collection dependency is preserved
(read at execution time; recorded in `result.storage_ref`).

Deterministic behaviors (binding):

- **crash after output creation, before success callback:** the
  output exists (complete, atomically renamed), the job is non-
  terminal → R7 classifies stale running as `manual_review` (non-
  restart-safe). The operator may adopt success via the operator
  CLI after evidence review. No automatic terminal adoption.
- **database succeeded, output missing:** download returns 404 (as
  today for a missing file); reconciliation records a bounded
  `anomaly` event; the job row is NOT auto-failed (conservative).
- **output exists, terminal transition missing:** `manual_review`
  (uncertain evidence; no automatic replay or adoption).
- **duplicate delivery / worker retry of the same attempt:** claim
  protocol makes it a no-op (T10 duplicate classification).
- **partial render:** the temp `.partial` file never occupies the
  final name; on failure it is quarantined (unlinked) by the
  handler's bounded cleanup.
- **DELETE:** cancels the durable job (best-effort Celery revoke as
  a non-guaranteed aid only) and deletes the output file; idempotent;
  the durable job record transitions per the accepted cancel
  semantics (pending → cancelled directly; running → cancelling;
  uncertain → manual_review).

## 7. API compatibility (binding)

- `POST /api/v2/artifacts` → 202 `ArtifactResponseV2`
  `{status: "accepted", message, job_id, artifact_id}` — unchanged
  shape; `job_id` is now the DURABLE job id; `artifact_id` remains
  the artifact/download identifier (server-generated subject id,
  persisted in `input_metadata.artifact_id`).
- `GET /api/v2/artifacts/{artifact_id}` → `JobResponseV2` resolved
  durable-first: the jobs service lists/reads the tenant's
  `v2_artifact` jobs and matches `input_metadata.artifact_id`;
  404 when unknown. Status projection maps durable states into the
  existing JobResponseV2 `status` strings (succeeded→`completed`,
  running→`running`, pending/dispatching/queued/dispatch_unknown/
  retry_wait→`pending`-family as today's queued projection,
  cancelling→`running` per the existing mapping, failed/cancelled→
  `failed`). `current_stage`/`stages_completed`/`stage_detail`/
  `progress` carry the artifact phases (bounded, additive).
- `GET /api/v2/artifacts/{artifact_id}/content` → 200 (FileResponse,
  media_type now derived from the recorded extension map instead of
  blanket `application/octet-stream` — additive refinement),
  202 in-progress, 404 unknown/missing-file, 410 failed with a
  SANITIZED summary (`e.__class__.__name__`-style bounded code +
  short summary; no raw exception text — a bounded error-text
  change inside the 410 detail).
- `DELETE /api/v2/artifacts/{artifact_id}` → 204, idempotent,
  cancels the durable job and deletes the output file.
- `GET /api/v2/artifacts/capabilities` → unchanged.
- No list endpoint (today: none), no pagination, **no retry
  endpoint** (operator retry only, unchanged posture).
- Legacy jobs created before deployment: they were in-memory only
  and already 404 after any restart; after migration they remain
  404 (no durable record exists). No transition window is required;
  orphaned output files behave as today (download 404 until the
  file is cleaned by an operator). The O(n) `JobManager.list_jobs`
  scan disappears from this router.

## 8. Owner decisions (recorded 2026-10-05 with the explicit
## acceptance; binding)

1. **Retrieval scoping:** preserved this phase (no KB-authorization
   redesign); BUT the effective collection/knowledge-base context is
   resolved server-side, a bounded normalized collection-context
   reference is persisted in durable input metadata, unauthenticated
   input cannot select tenant or collection context, worker
   execution validates tenant+collection against the durable
   submission record (missing/malformed/inconsistent/no-longer-
   permitted → reject), and execution context is never re-inferred
   from mutable process-global state. Parameters that can
   indirectly influence collection selection are normalized and
   validated. Finer-grained retrieval authorization is deferred.
2. **Artifact files vs retention:** job cleanup (rows, attempts,
   events, job-owned idempotency state) NEVER deletes generated
   artifact files; deletion remains controlled by the artifact API
   (or a future artifact-retention policy); an artifact may outlive
   its job-history record (documented).
3. **Client idempotency:** NO client-supplied idempotency key; each
   accepted POST creates a new artifact and a new durable job; a
   bounded canonical input fingerprint is still computed and
   persisted (diagnostics, reconciliation, duplicate-delivery
   protection, future idempotency); no automatic dedup by
   fingerprint.
4. **ADR ownership:** ADR-031 is Core-owned, stays in Retriva Core
   (`docs/adr/adr-031-v2-artifact-durable-jobs.md`); never moved to
   CRM Assistant.

Additional binding clarifications from the acceptance: Spec 025
`jobs.result_metadata` (bounded JSONB, written only through the
terminal-guarded success operation) is the dedicated result-metadata
facility — input metadata is immutable execution-request evidence
and must never be mutated to store output; storage references are
server-generated, tenant-consistent, path-safe (traversal rejected),
stable across restarts, resolve only through the configured
provider, never expose private host paths, and identify only the
finalized artifact; provenance evidence for the
finalized→crash→missing-success window is a bounded sidecar
(tenant, artifact id, job id, format, checksum, size) written at
finalization; reconciliation may adopt a completed artifact ONLY
with verified provenance, otherwise `manual_review`; provider calls
are never replayed merely because the success callback is absent;
no automatic handler retries (publication tries and operator retry
excepted); media types are deterministic per format with a safe
binary fallback.

## 9. Security and privacy

- No credentials, auth headers, raw uploads, unbounded prompts, raw
  exceptions, stack traces, or provider responses are persisted.
  `parameters`/`user_metadata` are size- and key-bounded, stored as
  sanitized JSONB; the user prompt/query appears only inside the
  bounded parameters the CLIENT chose to send (same exposure as
  today's in-memory manager, now with bounded size).
- Errors sanitized (`error_code` + short summary; exception class
  only), consistent with Spec 025 §3.7.
- Tenant context is server-resolved and fail-closed; artifact jobs,
  statuses, content, and deletion are tenant-scoped through the
  Spec 025 RLS model. Ordinary request input cannot select tenants.
- Logs carry safe durable job/attempt ids and phases only.

## 10. Tests (summary; full list in acceptance.md)

Domain/registration, persistence/tenancy, dispatch/worker (fake
broker + real claim protocol), side effects (atomic finalization,
crash windows, missing/partial/duplicate output), progress/cancel
(incl. finalization race, late success), reconciliation/restore
(non-restart-safe → manual_review; missing-output anomaly; idempotent
rerun), API compatibility (202 contract, projection, sanitized 410,
idempotent DELETE, no retry route), and container validation (real
Celery completion, ambiguous recovery, Redis flush, restart, Pro
compatibility, no new Pro dependency). See acceptance.md.

## 11. Rollback plan

The integration is additive at the code level and touches NO schema.
Rollback = revert the Core commit; pre-existing behavior (in-memory
jobs) returns; durable artifact rows (if any) remain inert in
`core.jobs` and are handled by reconciliation R7/R9 as
non-restart-safe work. No migration to reverse. The output file
format and location are unchanged.
