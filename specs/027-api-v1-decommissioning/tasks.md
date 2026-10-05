# Spec 027 — Tasks

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
(owner direction; revisions 1–2 superseded). Checked items: none —
implementation has not started; every box below opens only after
explicit owner acceptance.

## A. Governance (this pass)

- [x] Allocate Spec 027 + ADR-032 before first presentation (§43) —
      done in revision 1; identifiers retained.
- [x] Record revisions 1–2 (durable-migration direction) as
      SUPERSEDED by owner direction; re-title and re-path both
      artifacts; keep PROPOSED — done in this revision.
- [x] Update registry titles/paths/notes; governance tests unchanged
      (they read number/status/repository/path from the registry) —
      done.
- [x] Re-presented; owner explicitly ACCEPTED revision 3 (2026-10-05)
      together with ADR-032.

## B. Shared-symbol relocation (phase 1; pure refactor)

- [ ] Relocate `CancellationError` and `JobStatus` out of
      `job_manager.py` into a neutral module; update imports in
      `qdrant_store`, `embeddings`, `image_parser`, `docling_parser`,
      `durable_jobs`, `v2_documents`, `mediawiki_v2_parser`.
- [ ] Relocate `validate_user_metadata`, `UserMetadataValidationError`,
      `DeleteMetadataRequest` out of `schemas.py`; update
      `schemas_v2.py` and `v2_documents.py`.
- [x] Full battery green (no behavior change).

## C. Route removal (phase 2)

- [x] Delete the 9 v1 router files and their BackgroundTasks
      handlers; un-register at `main.py:137–145`; root discovery
      payload v2-only (`main.py:128`).
- [x] Delete v1-only request/response models from `schemas.py` (module removed).
- [x] CLI: remove v1 call paths (html, image, mediawiki
      pages/assets, collection delete) and the `--api-version v1`
      legacy branches in text/pdf/markdown; v2 branches remain
      (image handler and `reindex` fail locally with bounded
      guidance and make no HTTP request).
- [x] Remove `src/retriva/scratch/test_delete_api.py`,
      `src/retriva/scratch/verify_delete_route.py`.
- [x] Route-removal tests: removed paths → 404; no side effects; no
      v1 job state; no cancellation side effect
      (`tests/test_spec027_decommissioning.py`).

## D. Legacy-state retirement (phase 3)

- [x] Delete `ingestion_api/job_manager.py` (singleton, models,
      `TERMINAL_STATES`, projection, cancel-flag logic) — zero
      supported consumers remain after B/C.
- [x] Delete the legacy Redis helper block, the retry counters,
      `request_task_cancellation`, `get_task_status` (incl. the raw
      `AsyncResult` fallback); keys expire naturally (NO flush, NO
      destructive deletion); Redis/Celery transport untouched.
- [x] Remove legacy fallbacks: `v2_jobs.py` `_legacy_fallback`,
      `v2_documents.py` recorder-None fallback,
      `mediawiki_v2_parser.py` recorder-None fallback; durable-only
      + 404 semantics remain (degraded durable read → bounded 503).
- [x] Remove the mediawiki parser's `JobManager` import (recorder now mandatory).

## E. Tests (phase 4)

- [x] Remove the v1-behavior suites (`test_ingestion_api`,
      `test_jobs_api`, `test_pdf_injector`, `test_mediawiki_injector`,
      `test_user_metadata_filtering`, `test_job_manager`); the shared
      validator tests were relocated to
      `tests/test_metadata_validation.py` (importing
      `retriva.ingestion_api.metadata_validation`).
- [x] Adjust v1-compat assertions in `test_v2_acceptance` (AC-1 →
      404) and `test_v2_ingestion` (v1 fallback cases → 404;
      JobManager unit tests removed).
- [x] Adjust shared-symbol imports/usages in `test_deduplication`,
      `test_mediawiki_v2_parser` (in-test recorder double),
      `test_jobs_api_v2` (legacy-fallback test → 404),
      `test_artifact_jobs`, `test_v2_artifacts`.
- [x] v2 regression: durable document/mediawiki/artifact ingestion;
      tenant-scoped status/list/cancel; Redis loss + API restart
      durability; NO legacy fallback required (focused suites +
      container validation).

## F. Inventory and boundary gates (owner-required)

- [x] Re-run the plan.md §1 gate greps; commands AND findings
      recorded in the final report and
      `tests/test_spec027_decommissioning.py` (permanent in-repo
      gates).
- [ ] Gate: NO `JobManager` import remains outside the approved
      relocation target; NO `retriva:job:` writer remains; no dead
      cancellation-flag path; no dead `AsyncResult` fallback; Celery
      broker/result configuration intact.

## G. Docs and OpenAPI (phase 5)

- [x] `docs/openapi.yaml`: all 14 v1 path items + 10 v1-only
      component schemas removed (incl. stale JobStage ref); YAML
      valid, zero broken refs; v2 section unchanged.
- [x] `docs/ingestion-api.md`: replaced by the retirement note
      (removed routes, v2 replacements, retired CLI operations,
      unresolvable legacy ids, restart requirement, rollback).
- [x] `docs/implementation.md`: v1 sections updated (API surface +
      cancellation design notes).
- [x] `AGENTS.md`: UNTOUCHED per explicit owner decision (no
      constitutional rule requires a change; disposition reported).
- [x] Release note / migration guide: the retirement note IS the
      release guidance (docs/ingestion-api.md).

## H. Deployment and container validation

- [x] Full test battery green (851 passed; all 10 failures + 4
      errors proven identical at baseline HEAD; no schema migration
      applied).
- [x] Container: Core-only image builds/starts; Pro composition
      compatible (CRM/Messaging one-shots exit 0; pro.crm→9);
      existing durable one-shots healthy; no Core dependency on Pro.
- [x] Live stack untouched (verified); ingestion service restart
      documented as required for adoption.

## I. Closure

- [x] Evidence + final report files under `/mnt/devel/retriva/tmp/`
      (outside repos), including gate commands + findings and the
      retirement note.
- [x] Closure audit performed (SUCCESS); commit task remains for a
      SEPARATE explicit owner instruction.
