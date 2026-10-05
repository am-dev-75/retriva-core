# Spec 027 — API v1 decommissioning and legacy job-state retirement

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

Status: **ACCEPTED** — revision 3 (2026-10-05). Revision 1 proposed
migrating all eight v1 routes onto durable jobs; revision 2 narrowed
it to one bounded-text route; the owner then DIRECTED that Retriva
API v1 may be dropped entirely, superseding the durable-migration
direction; revision 3 re-scoped the phase to API v1 decommissioning
and legacy job-state retirement. Revisions 1 and 2 are recorded as
SUPERSEDED in the registry and in ADR-032 (history preserved, never
rewritten). The owner **explicitly accepted** revision 3 for
implementation on 2026-10-05 with the binding decisions recorded in
ADR-032's status section and spec §3/§4. The companion ADR-032 is
Core-owned
(`retriva-core/docs/adr/adr-032-retire-api-v1-and-legacy-job-authority.md`).

Registry: `docs/governance/spec-adr-registry.yaml` → specification
027 (`proposed`, path updated to `specs/027-api-v1-decommissioning`),
ADR-032 (`proposed`, path updated to
`docs/adr/adr-032-retire-api-v1-and-legacy-job-authority.md`).
Identifiers retained; no new allocation (Constitution §43: the
numbers were allocated before first presentation and remain
unaccepted — re-scoping in place is the correct transition for a
PROPOSED pack).

## 1. Summary and objective

The owner directed that Retriva API v1 may be dropped. This phase
therefore REMOVES the API v1 surface and retires the legacy
in-memory job infrastructure, instead of migrating v1 workflows onto
the durable Core jobs subsystem. Target architecture:

```
API v2 and other explicitly supported surfaces
    -> durable PostgreSQL-backed Core jobs where asynchronous

API v1
    -> removed (hard 404; no retirement router, no config gate)

legacy JobManager / Redis job shims
    -> removed when no supported consumer remains
```

No API v1 compatibility is preserved: repository-wide discovery
(§2, plan.md §1) found NO active external dependency — the gateway
uses only `/api/v2/...` (`retriva-gateway/src/retriva_gateway/core/
client.py:136–161`), connectors call the GATEWAY, WebUI, web
research, IAM, CRM, and Messaging have zero Retriva-v1 references,
and deployment health checks hit only `/health` and TCP probes. The
ONE verified internal dependency is the Core CLI's v1 call paths
(`src/retriva/cli.py`: html, image, mediawiki pages/assets,
collection delete, plus legacy `--api-version v1` fallback branches
in text/pdf/markdown), which ships in the same repository and is
removed/updated in the SAME change (§4). No compatibility exception
is proposed.

## 2. Discovery summary (full matrix in plan.md)

Verified at baseline `e4701fa` (gate-grep commands and counts in
plan.md §1):

- v1 routers (9 files): `ingest.py` (chunks POST + synchronous
  collection DELETE), `ingest_HTML.py`, `ingest_text.py`,
  `ingest_image.py`, `ingest_mediawiki.py`, `ingest_pdf.py` (page +
  upload), `ingest_markdown.py`, `jobs.py` (list/get/cancel),
  `documents.py` (`DELETE /api/v1/documents/{doc_id}`,
  `DELETE /api/v1/documents/metadata/filter`); registered at
  `ingestion_api/main.py:137–145`; advertised at `main.py:128`
  (`"api_v1": "/api/v1"`) and in `docs/openapi.yaml` (14 v1 path
  items).
- Legacy job state: `JobManager` singleton
  (`ingestion_api/job_manager.py`) written ONLY by v1 routes and
  the mediawiki parser's recorder-less fallback; Redis
  `retriva:job:*` / `retriva:retry:*` / `retriva:cancel:*` helpers
  (`tasks.py:62–136`) have NO remaining writers since the
  Spec 025/026 rewrites; `get_task_status`/`request_task_cancellation`
  shims consumed only by the v1 jobs router; raw Celery
  `AsyncResult` fallback exists ONLY inside `get_task_status`
  (`tasks.py:328–333`).
- Shared symbols HOSTED in v1 modules but used by v2/durable code
  (must be relocated, not deleted):
  - `CancellationError`, `JobStatus` in `job_manager.py` — used by
    `qdrant_store.py:114`, `embeddings.py:210`,
    `image_parser.py:104`, `docling_parser.py:196`,
    `durable_jobs.py:106,254,288,338,385,573,760`,
    `mediawiki_v2_parser.py:64`, `v2_documents.py:55`;
  - `validate_user_metadata`, `UserMetadataValidationError`,
    `DeleteMetadataRequest` in `schemas.py` — used by
    `schemas_v2.py:27,111,135,208` and `v2_documents.py:56,873`.
- Legacy read-side fallbacks inside v2: `v2_jobs.py:106`,
  `v2_documents.py:401` (JobManager fallback for unknown ids) and
  `mediawiki_v2_parser.py:299` (`JobManager()` fallback when no
  recorder is supplied) — dead once v1 is gone; removed.
- Tests: v1-behavior suites `test_ingestion_api.py`,
  `test_jobs_api.py`, `test_pdf_injector.py`,
  `test_mediawiki_injector.py`, `test_user_metadata.py`,
  `test_user_metadata_filtering.py`; v1-compat assertions inside
  `test_v2_acceptance.py` (AC-1) and `test_v2_ingestion.py`
  (lines 288, 340–348); shared-symbol imports in
  `test_deduplication.py`, `test_mediawiki_v2_parser.py`,
  `test_jobs_api_v2.py`, `test_artifact_jobs.py`.
- Docs: `docs/ingestion-api.md` (v1 reference), `docs/openapi.yaml`
  (v1 paths), `docs/implementation.md`, `docs/sdd/SDD_Delete_Document.md`
  and the APIv2 SDD pack's historical v1 references, `AGENTS.md`
  (Spec 014 v1 block — order-of-authority update), scratch scripts
  (`scratch/test_delete_api.py`, `scratch/verify_delete_route.py`).
- NO dependency: gateway (v2-only), connectors (gateway-mediated),
  webui/web-research/iam-entra (0 hits), CRM/Messaging (Apollo
  external-URL comment only), deployment (health checks are TCP and
  `/health`; `ingest_crm.sh` uses the GATEWAY; `.env`
  `openrouter.ai/api/v1`/`apollo.io/api/v1` matches are EXTERNAL
  provider URLs — unrelated and untouched).
- NO v1-only environment variables exist (`job_manager.py` reads no
  settings; the Redis URL is shared with Celery transport and
  REMAINS).

## 3. Removal strategy (binding proposal)

**Option A — hard removal** (recommended; owner-preferred default):
stop registering all nine v1 routers and remove the v1 code. Requests
to removed paths return normal 404 (no retirement router, no
configuration gate, no transition window). Rationale: the only
verified consumer (the Core CLI) is internal and co-versioned — it
is updated in the same change; a 410 phase only benefits external
callers that cannot be coordinated, and none exist. Option B (410
retirement router) and Option C (config-gated disablement) are
REJECTED; if the owner later identifies a hidden consumer, a bounded
410 phase can be added through a new governed change.

Functional gap flagged for owner acknowledgment: the CLI's standalone
image-ingestion path (`/api/v1/ingest/image`) has NO v2 equivalent
(v2 documents do not natively support images — the CLI itself logs
this); removing v1 retires CLI image ingestion until a v2 image
surface exists in a future governed change. HTML ingestion via the
CLI keeps a v2 path (`/api/v2/documents` with html content type —
`cli.py:42`); its v1 branch is removed.

## 4. Removal inventory (binding)

REMOVE with API v1 (exact symbols/files):

| item | disposition |
|---|---|
| 9 v1 router files + their BackgroundTasks handler functions (`process_*_in_background`) | delete files/functions; un-register at `main.py:137–145`; remove `"api_v1"` from the root discovery payload (`main.py:128`) |
| v1 request/response models in `schemas.py` (Html/Text/Image/MediaWiki/Pdf/Markdown/ChunkIngest requests, `IngestResponse`, `JobResponse`) | delete (v1-only) |
| `JobManager` module (`job_manager.py`): singleton, job models, `JobStatus`, `TERMINAL_STATES`, cancel-flag logic, status projection | DELETE ENTIRELY after relocating shared symbols (§5) — zero supported consumers remain |
| Redis legacy helpers `tasks.py:62–136` (`_set_job_state`, `_get_job_state`, `_delete_job_state`, retry counters, `_set_cancel_flag`, `_is_cancel_requested`, `_clear_cancel_flag`) + shims `get_task_status` (`tasks.py:321`, incl. the raw `AsyncResult` fallback at `tasks.py:328–333`) + `request_task_cancellation` (`tasks.py:304`) | delete (dead after v1 removal); existing Redis keys expire naturally (7-day / 24-hour TTLs); NO flush, NO destructive deletion; Redis itself REMAINS (Celery transport) |
| `mediawiki_v2_parser.py:299` recorder-less `JobManager()` fallback | remove (durable callers always supply a recorder) |
| `v2_jobs.py:106` and `v2_documents.py:401` legacy fallbacks | remove (they can only ever see v1-era ids; durable-first + 404 remains) |
| `cli.py` v1 call paths: html, image, mediawiki pages/assets, collection delete, and the `--api-version v1` legacy branches in text/pdf/markdown | remove; v2 branches remain (§3 gap note for image) |
| `scratch/test_delete_api.py`, `scratch/verify_delete_route.py` | remove (v1 dev scripts) |
| tests: the six v1-behavior suites; v1-compat assertions in `test_v2_acceptance.py`/`test_v2_ingestion.py` | remove/adjust per §6 of acceptance.md |
| docs: `docs/ingestion-api.md` (v1 reference), v1 paths + component schemas in `docs/openapi.yaml`, v1 sections in `docs/implementation.md` | remove/update; SDD packs and accepted specs 003/010/011/014 remain as HISTORICAL records (annotated, never rewritten) |
| `AGENTS.md` Spec 014 v1 block | updated as a governed doc change in the implementation phase (order-of-authority revision recorded) |

PRESERVE (shared with v2/supported surfaces — NOT removed):

| item | reason |
|---|---|
| `CancellationError`, `JobStatus` (relocated from `job_manager.py` to a neutral module, e.g. `retriva/ingestion_api/cancellation.py` — exact home fixed at implementation; imports updated in `qdrant_store`, `embeddings`, `image_parser`, `docling_parser`, `durable_jobs`, `v2_documents`, `mediawiki_v2_parser`) | used by durable v2 execution (cancel checkpoints, projection enums) |
| `validate_user_metadata`, `UserMetadataValidationError`, `DeleteMetadataRequest` (relocated from `schemas.py`) | used by `schemas_v2.py` and `v2_documents.py` |
| parsers, chunker, `qdrant_store.py` (`upsert_chunks`, `init_collection`, `delete_chunks_by_doc_id`, `get_client`), `embeddings.py`, `get_collection_name()` | shared ingestion internals used by v2 durable handlers and CLI v2 paths |
| `CollectionMiddleware` | shared; v2 requests flow through it; removal is NOT implied by v1 removal (verified at implementation) |
| all durable jobs machinery (`jobs/*`, `durable_jobs.py`, `tasks.py` durable task bodies), `celery_app.py` (incl. STARTED-state reporting) | Specs 025/026 surfaces; unchanged |
| `docs/openapi.yaml` v2 section, `/health`, root discovery (v2 entry) | supported surface |

## 5. JobManager retirement analysis

After v1 removal: writers = ZERO (only v1 routes and the parser
fallback wrote it); readers = ZERO (v1 jobs router gone; v2
fallbacks removed). The ONLY remaining references are the shared
symbol imports, which are relocated (§4). Result: `job_manager.py`
is deleted COMPLETELY — singleton, global accessor, in-memory job
models, legacy status projection, cancellation state, v1-specific
tests, and documentation. No bounded follow-up is required; the
"supported consumer" list is empty. v2 read-side fallbacks to legacy
state are removed (they cannot resolve anything but pre-Spec-025
v1-era ids, which become unresolvable by design — documented in §7).

## 6. Redis compatibility cleanup

| key family | writers today | readers today | disposition |
|---|---|---|---|
| `retriva:job:{id}` (7-day TTL) | NONE since Spec 025/026 (helpers uncalled) | v1 jobs router shim only | delete helper code; old keys expire naturally (≤ 7 days); no flush |
| `retriva:retry:{content_hash}` | NONE (helpers uncalled) | none | delete code; keys expire naturally |
| `retriva:cancel:{id}` (24 h TTL) | NONE (helpers uncalled) | v1 cancel shim only | delete code; keys expire naturally |
| Redis itself | Celery broker/result transport | durable dispatch + Celery | REMAINS untouched |

## 7. Compatibility and release impact (documented; no compat code)

- Removed routes: the nine v1 routers' complete surface (§4);
  post-removal access returns ordinary 404.
- v2 replacements where equivalents exist: document deletion
  (`DELETE /api/v2/documents/{doc_id}`, used by the gateway),
  metadata-filter deletion (`/api/v2/documents/filter`),
  file ingestion (`/api/v2/documents`, `/api/v2/documents/upload`,
  `/api/v2/documents/mediawiki` — used by the CLI v2 branches).
- No direct replacement: chunks/html/image ingestion routes and the
  v1 jobs surface (status/list/cancel). The durable replacement for
  status is the v2 jobs surface (`/api/v2/jobs`).
- Pre-removal v1 job ids: never durable; become permanently
  unresolvable after removal/restart (previously they were already
  lost on any API restart). Documented explicitly; NO synthetic
  durable records are created.
- Old clients: none known (§2); any undiscovered v1 caller receives
  404. A release note / migration guide IS warranted (implementation
  phase).
- OpenAPI: all 14 v1 path items and their schemas removed; v2
  section unchanged.
- Deployment: restart of the ingestion service required; no compose/
  env/healthcheck change.

## 8. Security and tenant-isolation effect

- The unscoped v1 list/status/cancel surface (no tenant model) is
  GONE; no unauthenticated route can list jobs, select arbitrary
  collection context, or cancel work.
- No raw-exception exposure path remains via v1 (`fail_job` stored
  raw strings; v1 surface deleted).
- No legacy in-memory state can bypass tenant-scoped durable jobs.
- v2 server-resolved tenant behavior unchanged (durable machinery
  untouched).
- Gateway/OpenAPI no longer advertise v1; unknown paths return 404
  without detail leakage.
- `CollectionMiddleware` is shared and PRESERVED; its v2 semantics
  are re-verified during implementation (no behavior change planned).

## 9. Database and deployment impact

- NO PostgreSQL migration; `core.jobs` schema and all durable rows
  (Specs 025/026) untouched; no CRM/Messaging schema changes.
- No database object used only by v1 was found (v1 persisted job
  state in memory/Redis only) — nothing to report for owner review.
- Deployment: no compose, env, healthcheck, script, queue, volume,
  or scheduler change; service restart only.

## 10. Tests (summary; full list in acceptance.md)

Inventory-completeness gates (grep-based, §13 of acceptance.md),
route-removal (404; zero side effects; zero v1 job state), v2
regression (durable document/mediawiki/artifact ingestion; tenant
scoping; Redis-loss + restart durability; NO legacy fallback
required), legacy-infrastructure removal gates (no JobManager
import outside the approved relocation; no `retriva:job:` writer; no
dead AsyncResult fallback; Celery configuration intact), OpenAPI/gateway
consistency, and container validation (Core-only image; Pro and
Messaging compatibility; no new migration; existing durable one-shots
healthy).

## 11. Rollback plan

Version-control revert of the Core change restores the v1 routers,
JobManager, Redis shims, CLI paths, tests, and docs; old Redis keys
have expired naturally (no restore needed — helpers recreate state
on demand); NO durable state is deleted and no migration must be
reversed; gateway unchanged (no gateway rollback needed); a 410
router would NOT simplify rollback (absent). Dead code is NOT kept
for rollback.
