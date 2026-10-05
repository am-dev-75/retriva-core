# Spec 027 — Plan

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
(owner direction; revisions 1–2 superseded). Implementation phases
below execute ONLY after explicit owner acceptance.

## 1. Discovery record (verified 2026-10-05, baseline `e4701fa`)

### Gate-grep commands and findings (re-run during implementation)

- `grep -rn "JobManager()" src/retriva --include="*.py"` → 22
  hits: the 8 v1 ingest/jobs routers (writers), `jobs.py:27,44,83`
  (read surface), `v2_jobs.py:106` + `v2_documents.py:401` (v2
  fallback READS), `mediawiki_v2_parser.py:299` (recorder-less
  fallback), plus per-router imports (`ingest.py:27,57`,
  `ingest_text.py:31,67`, `ingest_markdown.py:31,83`,
  `ingest_HTML.py:32,78`, `ingest_image.py:34,96`,
  `ingest_pdf.py:43,98,177,228`, `ingest_mediawiki.py:39,100`).
- `grep -rn "retriva:job:\|retriva:retry:\|retriva:cancel:"
  src/retriva --include="*.py"` → all inside `tasks.py:62–136`
  (writer-less residue; 7-day and 24-hour TTLs).
- `grep -rn "AsyncResult" src/retriva --include="*.py"` →
  `tasks.py:328–333` (inside `get_task_status`, consumed only by the
  v1 jobs router) + a comment in `celery_app.py:103` (general
  STARTED-state reporting; stays).
- `grep -rn "background_tasks.add_task" src/retriva --include="*.py"`
  → 8 v1 registrations + durable local-transport sites
  (`durable_jobs.py:714`, `jobs/service.py:91`, `jobs/local.py:64`).
- `grep -rn "api/v1" src/retriva --include="*.py"` → 9 v1 router
  prefixes + `main.py:128` + `cli.py` v1 call sites + openai
  intent/reranker OPENROUTER base URLs (external, unrelated).
- `grep -rn "api/v1" docs/openapi.yaml` → 14 v1 path items.
- Cross-repository: gateway `client.py` uses `/api/v2/...` only
  (0 v1 hits; one historical report mentions an external LLM host's
  `/api/v1/models`); connectors (mediawiki, email-agent), webui,
  web-research (Apollo external URLs only), iam-entra, CRM
  (Apollo comment), messaging: ZERO Retriva-v1 references; no
  connector calls v1 directly (gateway-mediated).
- Deployment: health checks = TCP probes + `/health` +
  `/gateway/health` (NO v1); `ingest_crm.sh` uses the GATEWAY;
  `.env` `openrouter.ai/api/v1` / `apollo.io/api/v1` are external
  provider URLs.
- CLI (`cli.py`): v2 branches already exist for text/pdf/markdown/
  document upload (`/api/v2/documents`,
  `/api/v2/documents/mediawiki`); v1-only paths remain for html,
  image (no v2 equivalent), mediawiki pages/assets, collection
  delete, plus `--api-version v1` fallback branches.
- Shared-symbol inventory: `CancellationError`/`JobStatus` imports
  from `job_manager.py` in `qdrant_store.py:114`,
  `embeddings.py:210`, `image_parser.py:104`,
  `docling_parser.py:196`, `durable_jobs.py:106,254,288,338,385,
  573,760`, `mediawiki_v2_parser.py:64`, `v2_documents.py:55`;
  `validate_user_metadata`/`UserMetadataValidationError`/
  `DeleteMetadataRequest` imports from `schemas.py` in
  `schemas_v2.py:27` and `v2_documents.py:56`.
- Env vars: NONE used only by legacy v1 job state
  (`job_manager.py` reads no settings; the Redis URL is shared with
  Celery transport and remains).

### Dependency matrix

| item | defined in | consumers | tests | docs | disposition | removal order | rollback |
|---|---|---|---|---|---|---|---|
| `POST /api/v1/ingest/chunks` | `ingest.py:54` | CLI: none found; tests only | `test_ingestion_api` | openapi, ingestion-api.md | remove | 1 | git revert |
| `DELETE /api/v1/ingest/collection` | `ingest.py:62` | CLI `reindex`/clear (`cli.py:641`) | `test_ingestion_api` | openapi, ingestion-api.md | remove; CLI branch removed | 1 | git revert |
| `POST /api/v1/ingest/html` | `ingest_HTML.py:75` | CLI (`cli.py:66`; v2 branch exists at `cli.py:42`) | `test_ingestion_api` | openapi, ingestion-api.md | remove; CLI v1 branch removed | 1 | git revert |
| `POST /api/v1/ingest/text` | `ingest_text.py:64` | CLI legacy branch (`cli.py:115`; v2 default) | v1 suites + v2 compat asserts | openapi, ingestion-api.md | remove + CLI branch | 1 | git revert |
| `POST /api/v1/ingest/markdown` | `ingest_markdown.py:80` | CLI legacy branch (`cli.py:505`; v2 at 485) | `test_ingestion_api` | openapi | remove + CLI branch | 1 | git revert |
| `POST /api/v1/ingest/pdf` (page) | `ingest_pdf.py:163` | CLI page flow legacy branch (`cli.py:421`; v2 at 384) | `test_pdf_injector` | specs 011 (historical), openapi | remove + CLI branch | 1 | git revert |
| `POST /api/v1/ingest/upload/pdf` | `ingest_pdf.py:190` | none found (gateway uses v2 upload) | `test_ingestion_api` | openapi | remove | 1 | git revert |
| `POST /api/v1/ingest/image` | `ingest_image.py:93` | CLI image flow + mediawiki asset enrichment (`cli.py:84,328`) — **no v2 equivalent** | `test_ingestion_api` | openapi, ingestion-api.md | remove; CLI image path retired (owner-acknowledged gap, spec §3) | 1 | git revert |
| `POST /api/v1/ingest/mediawiki` | `ingest_mediawiki.py:86` | CLI page flow (`cli.py:311`; v2 upload path exists at 255) | `test_mediawiki_injector` | specs 010 (historical), openapi | remove + CLI branch | 1 | git revert |
| `GET /api/v1/jobs`, `GET /api/v1/jobs/{id}`, `POST .../cancel` | `jobs.py:24,32,73` | tests only | `test_jobs_api` | openapi, ingestion-api.md | remove (durable v2 jobs surface is the replacement) | 2 | git revert |
| `DELETE /api/v1/documents/{doc_id}` + `/metadata/filter` | `documents.py:25,76` | none found (gateway uses `/api/v2/documents/{doc_id}`); v2 equivalents exist | `test_user_metadata*` | SDD_Delete_Document (historical), openapi | remove | 1 | git revert |
| `JobManager` module | `job_manager.py` | v1 routers + parser fallback (all removed) + shared symbols (`CancellationError`, `JobStatus`) | v1 suites; shared-symbol imports in `test_deduplication`, `test_mediawiki_v2_parser`, `test_jobs_api_v2`, `test_artifact_jobs` | implementation.md mentions | DELETE after symbol relocation | 3 (after routes) | git revert |
| Redis job-state helpers + shims + `AsyncResult` fallback | `tasks.py:62–136,304,321–333` | v1 jobs router only (removed) | `test_jobs_api` | implementation.md | delete code; keys expire naturally | 2 | git revert |
| v2 legacy fallbacks (`v2_jobs.py:106`, `v2_documents.py:401`, parser `:299`) | v2 routers/parser | legacy ids only | `test_jobs_api_v2` fallback cases | implementation.md | remove; durable-first + 404 | 3 | git revert |
| shared validator symbols in `schemas.py` | `schemas.py:29–131` | `schemas_v2.py`, `v2_documents.py` | `test_user_metadata*` (relocated tests keep passing) | — | RELOCATE (not remove) | 3 | git revert |
| `CollectionMiddleware` | `main.py:127` | v1+v2 requests | — | — | PRESERVE (shared) | — | — |
| parsers/chunker/`qdrant_store`/`embeddings` | `ingestion/*`, `indexing/*` | v2 durable handlers, CLI v2 | v2 suites | — | PRESERVE (import updates only) | — | — |
| v1 discovery entry + openapi v1 paths | `main.py:128`, `openapi.yaml` | clients | — | docs | remove | 2 | git revert |

## 2. Implementation phases (after acceptance)

1. **Shared-symbol relocation:** move `CancellationError`/`JobStatus`
   to a neutral module and `validate_user_metadata`-family to a
   shared home; update v2 imports; battery green (pure refactor).
2. **Route removal:** delete the 9 v1 router files, un-register
   (`main.py`), remove v1 models, remove v1 CLI branches, remove
   scratch scripts; route-removal tests (404, zero side effects).
3. **Legacy-state retirement:** delete `job_manager.py`; delete the
   Redis helper block + shims + `AsyncResult` fallback; remove the
   three v2/parser legacy fallbacks; update imports.
4. **Tests:** remove the six v1 suites; adjust v2 compat assertions
   (v1 routes → assert 404), fallback tests → durable-only;
   v2 regression suite green.
5. **Docs:** openapi v1 paths/schemas removed; `ingestion-api.md`
   removed/replaced by a short retirement note; implementation.md v1
   sections updated; AGENTS.md order-of-authority revision recorded
   as a governed doc change; release note drafted.
6. **Validation:** full battery; container validation (Core-only
   image, Pro/Messaging compatibility, durable one-shots healthy);
   gate-grep re-run with commands+findings in the report; closure
   audit; commit task ONLY after explicit owner instruction.

## 3. Compatibility contract

- Removed routes return ordinary 404 (no body detail).
- v2 surfaces unchanged; durable ids/status/cancel semantics
  unchanged; pre-Spec-025 v1-era job ids become permanently
  unresolvable (documented; no synthetic records).
- CLI: v2-only ingestion; image path retired (acknowledged gap).

## 4. Risks and mitigations

| risk | mitigation |
|---|---|
| hidden v1 consumer | repo-wide gate greps (§1) re-run in acceptance; none found across gateway/connectors/clients/deployment; owner informed of the CLI image gap |
| shared-symbol breakage | relocation is phase 1, battery-green gate before any removal |
| stale Redis keys | natural TTL expiry; no flush; helpers deleted only after route removal proves no readers |
| v2 regression | v2 regression suite + container one-shots (acceptance H) |
| rollback needs | pure git revert; no DB/Redis destructive step; 410 router deliberately absent |

## 5. Owner-authorization note

No implementation starts before the owner explicitly accepts
Spec 027 and ADR-032 (revision 3, decommissioning scope). This pass
ends at PROPOSED.
