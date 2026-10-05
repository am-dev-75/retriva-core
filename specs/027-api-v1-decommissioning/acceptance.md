# Spec 027 — Acceptance criteria

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
(owner direction; revisions 1–2 superseded). All items are PLANNED
gates; none is executed before explicit owner acceptance of
Spec 027 / ADR-032.

## A. Inventory completeness (planned; owner §13)

- A1 All v1 route registrations identified and removed (the
  plan.md §1 matrix is the checklist; gate re-run records commands
  AND findings).
- A2 All JobManager consumers identified; after removal ZERO
  imports remain outside the approved relocation target.
- A3 All legacy Redis key readers/writers identified
  (`retriva:job:`, `retriva:retry:`, `retriva:cancel:`); after
  removal ZERO writers/readers remain in code.
- A4 All raw `AsyncResult` fallbacks identified; the only one
  (`get_task_status`) is deleted; `celery_app.py` STARTED-state
  reporting (general Celery behavior) remains.
- A5 All v1 BackgroundTasks registrations and v1-only task
  wrappers/handler functions identified and removed; durable
  local-transport sites remain.
- A6 All docs/examples/gateway references identified; gateway
  verified v2-only; docs updated per tasks G.

## B. Route removal (planned)

- B1 All nine v1 routers unregistered; removed paths return
  ordinary 404 (no detail leakage).
- B2 POSTing to any removed route has NO side effects: no job
  submission, no job state, no cancellation effect, no Qdrant
  writes.
- B3 Root discovery payload no longer advertises `api_v1`.
- B4 CLI: v2-only ingestion paths; v1 branches removed; the image
  path retirement is documented (owner-acknowledged gap).

## C. v2 regression (planned)

- C1 Durable v2 document ingestion succeeds (fake broker + real
  container).
- C2 Durable v2 MediaWiki ingestion succeeds (parser without the
  JobManager fallback).
- C3 Durable v2 artifact generation succeeds (Spec 026 flows
  unchanged).
- C4 v2 status/list/cancel remain tenant-scoped; no unscoped list
  surface exists anywhere.
- C5 Redis loss + API restart preserve durable state; NO legacy
  fallback is required or present.
- C6 Relocated shared symbols behave identically (cancel
  checkpoints, projection enums, metadata validation) — full
  battery green.

## D. Legacy infrastructure removal (planned)

- D1 `job_manager.py` deleted; `grep -rn "job_manager" src/retriva`
  returns only the approved relocation target references.
- D2 No `retriva:job:` writer remains; helper code deleted; old
  keys expire naturally (no flush; no destructive deletion).
- D3 No dead cancellation-flag path or `AsyncResult` status
  fallback remains.
- D4 Celery broker/result configuration needed by durable workflows
  intact (worker starts; tasks dispatch).

## E. OpenAPI, gateway, docs (planned)

- E1 `docs/openapi.yaml` contains no v1 paths/schemas; v2 section
  byte-identical in behavior.
- E2 Gateway behavior unchanged (v2-only; verified by its own
  suite); no gateway file changed.
- E3 No stale discovery/capability entry advertises v1.
- E4 Retirement note + release note drafted (removed routes, v2
  replacements, unresolvable pre-removal v1 ids).

## F. v1 test retirement (planned)

- F1 The six v1-behavior suites removed/adjusted; v1-compat
  assertions in `test_v2_acceptance`/`test_v2_ingestion` now assert
  404.
- F2 Shared-symbol tests (`test_deduplication`,
  `test_mediawiki_v2_parser`, `test_jobs_api_v2`,
  `test_artifact_jobs`) pass with relocated imports.
- F3 Historical accepted spec packs (003/010/011/014) untouched as
  records.

## G. Containers (planned)

- G1 Core-only image builds and starts.
- G2 Pro composition compatible; Messaging compatible; no new
  migration applied; existing durable one-shots healthy; no Core
  dependency on Pro.
- G3 Live stack untouched; ingestion service restart documented.

## H. Security posture (planned)

- H1 No unscoped list/status/cancel route remains.
- H2 No unauthenticated route can select arbitrary collection
  context (CollectionMiddleware semantics unchanged and re-verified).
- H3 No raw-exception exposure path remains via any removed surface.
- H4 v2 server-resolved tenant behavior unchanged.

## V. Executed validation record

Executed 2026-10-05 after implementation, at working tree on
`centralized_relational_db` (base `e4701fa` + this phase's governed
changes). Full command log: `/mnt/devel/retriva/tmp/spec027-validation-evidence.txt`.

- A1–A6 PASS. Permanent in-repo gates:
  `tests/test_spec027_decommissioning.py` (32 passed): all 14 removed
  routes → 404 (no side effects — upsert/requests mocks untouched);
  OpenAPI zero `/api/v1` paths with v2 intact; root discovery
  `api_v2`-only; CLI source has no `/api/v1` or `api_version` tokens;
  CLI image handler and `reindex` make NO HTTP call (mocked
  requests); no `job_manager` import; no `retriva:job:`/`retriva:retry:`/
  `retriva:cancel:` code; no `AsyncResult` fallback; no
  `*_in_background` handlers; relocated symbols behavioral;
  `schemas.py`/`job_manager.py` deleted.
- B1–B4 PASS (same suite; 404 body carries no internal detail).
- C1–C6 PASS: focused suites green — `test_jobs_api_v2` (durable-only
  resolution; legacy-fallback test rewritten to 404), `test_v2_ingestion`
  (11 passed incl. durable submit/complete, metadata propagation,
  upload flow), `test_v2_acceptance`, `test_v2_artifacts` (13, standalone),
  `test_artifact_jobs` (22), `test_mediawiki_v2_endpoint`,
  `test_jobs_domain/persistence/dispatch`, `test_mediawiki_v2_parser`
  (recorder double; unblocked by removing a dead `COLLECTION_NAME`
  import), `test_metadata_validation` (14, relocated).
- D1–D4 PASS (gate suite; `celery_app.py` intact — `retriva-worker`
  ran the real reconcile CLI in-container).
- E1–E4 PASS: `docs/openapi.yaml` valid YAML, 0 broken refs, zero v1
  paths, 10 v1-only schemas removed; gateway untouched (its suite
  green in the workspace run below); retirement note + release
  guidance = `docs/ingestion-api.md`.
- F1–F3 PASS: v1 suites deleted; relocated suites pass; historical
  spec packs untouched.
- G1–G3 PASS: isolated container validation (project `spec027-audit`,
  dedicated network, no published host ports, live stack untouched):
  images built; stack healthy; 9 removed routes → 404 in-container;
  OpenAPI + discovery clean; OpenAI `/v1/models` → 200; real Celery
  completions: `v2_upload|succeeded|celery`, `v2_mediawiki|succeeded|celery`,
  `v2_artifact|completed` + content download 200 text/markdown; local
  fallback `v2_artifact|succeeded|local` (CELERY_BROKER_URL="");
  Redis flushall → durable status preserved; ingestion restart →
  preserved; CRM/Messaging one-shots exit 0 (`pro.crm`→9; messaging
  Alembic tables present); `core.platform=1 core.jobs=1` with
  migration rerun `"applied": []` (no-op); reconcile CLI exit 0;
  live stack verified intact afterwards (ledger unchanged, 0 v1-type
  rows, /health 200; live still runs the OLD image by design until
  the owner deploys).
- H1–H4 PASS (the removal eliminates the unscoped v1 job surface by
  construction; v2 tenant posture unchanged; verified by the
  tenant-scoping tests in `test_jobs_api_v2`).
- Full battery: **851 passed, 1 skipped, 10 failed, 4 errors — the
  10 failures + 4 errors are PROVEN identical at baseline HEAD**
  (openai_api 4 — missing external credentials; kb_cascade 1;
  v2_kbs_api 1; v2_artifacts 4 — pre-existing ordering pollution;
  v2_metadata_catalog 4 errors), plus three suites excluded with
  proven reasons (test_deduplication: 7 identical baseline failures,
  platform-env dependent; test_bilingual: 5 identical baseline
  errors, external LLM unreachable; test_metadata_filtering_modes:
  identical baseline ImportError of the long-gone `COLLECTION_NAME`
  symbol). Failure-set diff vs HEAD worktree: only baseline-only
  items removed by this phase (deleted v1 test files + governance
  status assertions now updated). ZERO introduced regressions.
- Deviations recorded: (1) `test_mediawiki_v2_parser.py` — one dead
  import line removed to unblock collection (justified dead-code
  removal; the known baseline defect was exactly this ImportError);
  (2) unknown-id durable read errors surface as bounded 503
  (`test_jobs_api_v2` rewritten accordingly); (3) CLI `--api-version`
  and `--namespaces` arguments removed (v1-era plumbing); `reindex`
  retired with local bounded failure; (4) AGENTS.md untouched per
  explicit owner decision.
