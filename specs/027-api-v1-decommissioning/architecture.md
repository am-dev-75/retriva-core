# Spec 027 — Architecture

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

Status: PROPOSED — revision 3 (2026-10-05): API v1 decommissioning
and legacy job-state retirement (owner direction; revisions 1–2
superseded). Companion to spec.md; not binding until owner
acceptance.

## 1. Current architecture (verified)

- The ingestion app (`ingestion_api/main.py`) registers nine v1
  routers (lines 137–145: ingest, ingest_HTML, ingest_image,
  ingest_text, ingest_mediawiki, ingest_pdf, ingest_markdown, jobs,
  documents) alongside seven v2 routers (146–153) and advertises
  both at the root discovery payload (`main.py:128`).
- v1 routes execute via FastAPI BackgroundTasks; state lives in the
  in-process `JobManager` singleton; the v1 jobs router layers
  Redis `retriva:job:*` shims and a raw Celery `AsyncResult`
  fallback on top. All Redis legacy helpers have been writer-less
  since the Spec 025/026 rewrites.
- `JobManager` hosts SHARED symbols consumed by v2 durable code:
  `CancellationError` (cancel-checkpoint machinery in
  `qdrant_store`, `embeddings`, `image_parser`, `docling_parser`,
  `durable_jobs`, `v2_documents`, `mediawiki_v2_parser`) and
  `JobStatus` (durable projection recorder). `schemas.py` hosts
  `validate_user_metadata` / `UserMetadataValidationError` /
  `DeleteMetadataRequest` used by `schemas_v2.py` and
  `v2_documents.py`.
- v2 fallbacks to legacy state: `v2_jobs.py:106`,
  `v2_documents.py:401` (JobManager lookup for unknown ids) and
  `mediawiki_v2_parser.py:299` (recorder-less fallback).
- No gateway/connector/client/deployment dependency on v1 exists
  (evidence in spec §2 and plan.md §1). The only internal consumer
  is the Core CLI (`cli.py`), which already has v2 branches for
  text/pdf/markdown and v2-only paths for document upload.

## 2. Target architecture (after this phase)

```
API v2 (+ openai_api surface, untouched)
    -> durable Core jobs (Specs 025/026), tenant-scoped, PostgreSQL

API v1
    -> ABSENT (no routers, no models, no 404 detail leakage)

JobManager / Redis job shims / AsyncResult fallback
    -> DELETED (shared symbols relocated; keys expire naturally)
```

- `main.py` registers ONLY the v2 routers (+ `/`, `/health`);
  the root discovery payload carries `api_v2` only.
- Shared cancellation/projection symbols move from
  `job_manager.py` into a neutral module; user-metadata validation
  moves out of `schemas.py` (v1-only models deleted); v2 imports are
  updated. Durable execution semantics are UNCHANGED.
- `tasks.py` retains ONLY the three durable task bodies
  (`process_document_task`, `process_mediawiki_task`,
  `process_artifact_task`); the legacy Redis/cancellation/status
  helper block is deleted. `celery_app.py` stays (transport;
  STARTED-state reporting stays — it is general Celery behavior).
- `v2_jobs`/`v2_documents` become durable-only (404 for unknown
  ids); the mediawiki v2 parser keeps its durable-recorder path and
  loses the `JobManager` fallback.
- CLI: v2 branches are the only ingestion paths; v1-only CLI
  branches (html, image, mediawiki pages/assets, collection delete)
  are removed.

## 3. Component disposition

| component | disposition |
|---|---|
| `ingestion_api/routers/{ingest,ingest_HTML,ingest_text,ingest_markdown,ingest_pdf,ingest_image,ingest_mediawiki,jobs,documents}.py` | DELETE |
| `ingestion_api/main.py` | un-register v1; discovery payload v2-only |
| `ingestion_api/schemas.py` | delete v1-only models; relocate shared validator symbols |
| `ingestion_api/job_manager.py` | DELETE (after relocating `CancellationError`/`JobStatus`) |
| `ingestion_api/tasks.py` | delete legacy Redis/status/cancel helpers (lines ~62–136, 304, 321–333); durable tasks untouched |
| `v2_jobs.py`, `v2_documents.py`, `mediawiki_v2_parser.py` | remove legacy fallbacks/imports; durable behavior unchanged |
| `cli.py` | remove v1 call paths + `--api-version v1` branches |
| `qdrant_store.py`, `embeddings.py`, `image_parser.py`, `docling_parser.py` | import updates only (relocated `CancellationError`) |
| `jobs/*`, `durable_jobs.py`, `celery_app.py`, `worker.py` | UNCHANGED (import updates only) |
| `CollectionMiddleware` | UNCHANGED (shared; verified at implementation) |
| `docs/openapi.yaml`, `docs/ingestion-api.md`, `docs/implementation.md`, `AGENTS.md`, scratch scripts | doc updates per spec §4 |
| deployment / CRM / Messaging / gateway / connectors / webui / web-research | NO changes |

## 4. Invariants honored

- PostgreSQL remains the only authoritative job store; durable v2
  workflows (document, mediawiki, artifact) are preserved
  byte-for-byte in behavior; tenant scoping and sanitized errors
  unchanged.
- No dead compatibility code is retained merely for history.
- No destructive Redis/DB operations; no schema migration.
- Historical accepted specs (003/010/011/014) remain records; only
  living docs (openapi, ingestion-api, implementation, AGENTS.md)
  are updated, as governed doc changes.

## 5. Deferred (explicitly out of scope)

- v2-native image ingestion (the CLI image gap) — future governed
  change if the owner wants it.
- Any 410 retirement router or config-gated re-enablement (rejected
  in §3 of spec.md; could be added later only via a new governed
  change with explicit owner acceptance).
- v2 API redesign, gateway changes, scheduler/outbox work.
- Historical spec packs' text (never rewritten).
