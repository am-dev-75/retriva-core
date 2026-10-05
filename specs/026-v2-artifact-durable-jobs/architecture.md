# Spec 026 — architecture

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

Status: PROPOSED (revision 1).

## 1. Current architecture (verified)

```
POST /api/v2/artifacts (v2_artifacts.py)
  ├─ uuid4 artifact_id
  ├─ JobManager().create_job(source=f"artifact:{artifact_id}",
  │                          job_type="v2_artifact")        [in-memory]
  └─ BackgroundTasks.add_task(process_artifact_v2, …)      [API process]
        ├─ manager.start_job(job_id)
        ├─ renderer = get_renderer(format)                 [CapabilityRegistry]
        ├─ data = fetch_artifact_data(artifact_type, parameters)
        │     ├─ document_list → _retrieve_and_select(20)  [Qdrant read]
        │     └─ basic_report  → ask_question(query)       [LLM provider call]
        ├─ renderer.render(parameters, output_path=FINAL PATH,
        │                 cancel_check=manager.is_cancel_requested)
        └─ complete_job / fail_job(str(e))                 [in-memory]
GET /{artifact_id}          → O(n) scan of JobManager by source  [404 after restart]
GET /{artifact_id}/content  → FileResponse when COMPLETED; 410 with RAW error text
DELETE /{artifact_id}       → manager.request_cancel + storage.delete(file)
Output: LocalStorageProvider → <artifacts_path>.parent/collections/<col>/artifacts/<artifact_id><ext>
```

State stores today: in-memory `JobManager` (job state) + filesystem
(output file; the only durable mapping `artifact_id → file` is the
file name itself). No Redis keys, no Celery result-backend reads, no
database rows. Restart ⇒ job records vanish; output files orphan.

## 2. Target architecture (Spec 025 reuse, no parallel framework)

```
POST /api/v2/artifacts (v2_artifacts.py)
  ├─ resolve_request_tenant(request)                 [server-resolved, fail-closed]
  ├─ artifact_id = uuid4;  fingerprint = sha256(artifact_type, format, parameters)
  ├─ jobs_service.submit(tenant_id, job_type="v2_artifact",
  │                      execution_transport=…, input_metadata=bounded contract v2-1)
  │     [durable row + idempotency index (NULL key) + job_created event]
  └─ service.dispatch_job(job, payload=reconstructed, background_tasks)
        ├─ celery:   publisher → preallocated task id → attempt prepared
        │            → CONFIRMED/REJECTED/AMBIGUOUS publication model (Spec 025 §3.4)
        └─ local:    LocalExecutor runner on BackgroundTasks (same protocol)
Celery worker (existing image, existing queue):
  process_artifact_task → _execute_via_protocol (claim T10, duplicate
  delivery no-op) → artifact handler:
        ├─ DurableProgressRecorder (phases: fetching_data → rendering → finalizing)
        ├─ data = fetch_artifact_data(…)          [unchanged product logic]
        ├─ renderer.render(parameters, output_path=<artifact_id><ext>.partial,
        │                 cancel_check=recorder.is_cancel_requested)
        │     [cooperative cancel checkpoint preserved]
        ├─ os.replace(.partial → .final)          [atomic finalization]
        ├─ result = {artifact_id, storage_ref, media_type, size, sha256}
        └─ complete_success(…, result)  /  sanitized failure (retryable=False)
GET /{artifact_id}    → durable-first lookup (tenant-scoped, by
                        input_metadata.artifact_id) → JobResponseV2 projection
GET /{artifact_id}/content → status + LocalStorageProvider.get_path; sanitized 410
DELETE /{artifact_id} → request_cancel (best-effort revoke aid) + file delete
```

Reconciliation: unchanged R1–R9. With `restart_safe=False`, stale
running → `lost` + `manual_review`; dispatch_unknown → R2
same-generation republication (payload fully reconstructable from
durable metadata; the type is non-restart-safe for EXECUTION replay
after process loss, while same-generation republication of an
unclaimed dispatch is safe — no execution has started).

## 3. Component responsibilities (all in retriva-core)

| component | change |
|---|---|
| `jobs/registry.py` | register `v2_artifact` (restart_safe=False, task name, queue `ingestion`) |
| `ingestion_api/tasks.py` | `_register_tasks` adds `process_artifact_task` (Celery body mirrors document/mediawiki bodies) |
| `ingestion_api/durable_jobs.py` | `artifact_handler` (mirrors `document_handler`): recorder, payload normalization, sanitized failures, `finalize_artifact_output` (atomic replace + checksum + bounded result metadata), retried=False classification for handler failures |
| `jobs/service.py` | (no change expected — payload reconstruction is generic over `input_metadata`) |
| `routers/v2_artifacts.py` | durable submit/dispatch; durable-first projection; sanitized 410; DELETE via durable cancel; delete JobManager usage |
| `routers/v2_jobs.py` | unchanged (durable list already covers `v2_artifact` jobs) |
| rendering/* | unchanged (renderers keep writing to a caller-provided path; the handler chooses the temp path) |
| infrastructure/storage.py | unchanged (LocalStorageProvider; handler passes the base dir) |
| deployment | none required (same image/queue/env) |

## 4. Invariants honored

- Single authoritative lifecycle (no JobManager/Redis co-writes).
- Core ownership (no Pro dependency; no CRM imports).
- Spec 025 state machine reused; no new states.
- Tenant isolation (resolver + RLS + fail-closed reads).
- Transport independence (one handler, two transports).
- Bounded metadata; sanitized errors; no raw content persisted.
- Append-only `job_events`; conservative `manual_review`.
- No public retry route; operator CLI remains the only retry path.

## 5. Deferred (explicitly out of scope)

Gateway proxying; v1 flows; other v2 flows; JobManager removal;
schedulers; session-artifact changes; retrieval-scope product change
(owner decision, §8 of spec.md).
