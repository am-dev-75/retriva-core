# Spec 026 — plan

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

Status: **ACCEPTED** — revision 1 (2026-10-05; explicit owner
acceptance with the four decisions recorded in spec.md §8).

## 1. Discovery sources (verified 2026-10-05)

- `src/retriva/ingestion_api/routers/v2_artifacts.py` (239 lines —
  full route + BackgroundTasks worker + JobManager usage).
- `src/retriva/rendering/__init__.py`, `pdf_renderer.py`,
  `markdown_renderer.py`, `docx_renderer.py`, `xlsx_renderer.py`,
  `opendocument_renderer.py` (Renderer protocol, capability
  registration, single cancel checkpoints, direct final-path writes).
- `src/retriva/rendering/services.py` (`fetch_artifact_data`:
  `_retrieve_and_select` for document_list; `ask_question` — LLM
  provider call — for basic_report; deterministic fallback from
  `parameters.title/content`).
- `src/retriva/infrastructure/storage.py` (`LocalStorageProvider`:
  collection-scoped path, glob lookup, delete).
- `src/retriva/domain/artifacts.py` (ArtifactStatus enum; not a state
  store by itself).
- `src/retriva/ingestion_api/schemas_v2.py` (ArtifactRequestV2 /
  ArtifactResponseV2 / ArtifactCapabilitiesResponseV2; JobResponseV2).
- `src/retriva/ingestion_api/main.py` (router mount; session
  sweep_expired is the OTHER, session-scoped artifact feature).
- `tests/test_v2_artifacts.py` (compatibility surface: 202 contract,
  polling, content download, capabilities, idempotent delete).
- `tests/test_v2_discovery.py` (capabilities surfaced in /v2/discovery).
- Spec 025 pack (accepted) + committed implementation (jobs service,
  registry, dispatch, execution, tasks registration, durable API).
- NO artifact Celery task exists; no Redis artifact state; no retry.

## 2. Implementation phases (after acceptance)

- **A. Contract registration**: `v2_artifact` in the job-type
  registry (payload v2-1, restart_safe=False, task name, queue);
  payload reconstruction (generic mechanism); bounded-input
  validation; tests.
- **B. Execution handler**: `artifact_handler` in durable_jobs
  (recorder phases, sanitized failures, non-retryable handler
  failures, atomic finalization, checksum, bounded result metadata);
  Celery task `process_artifact_task`; local fallback via the same
  handler; tests.
- **C. Route integration**: v2_artifacts submission through the
  durable service (tenant resolution, bounded contract), durable-
  first status projection, sanitized 410, durable cancel on DELETE;
  REMOVE all JobManager touches from this router; tests incl.
  no-legacy-write proof.
- **D. Reconciliation coverage**: verify R7 (lost → manual_review),
  R2 republication, missing-output anomaly behavior against the new
  job type; add focused tests (no new reconciliation rules required
  — the accepted conservative rules already cover the artifact
  classification; this phase only proves them).
- **E. Container validation**: real Celery completion, ambiguous
  recovery, Redis flush, restart persistence, Pro compatibility,
  no-Pro-dependency proof (same image/queue as Spec 025).
- **F. Closure**: docs, governed evidence, truthful report.

## 3. Compatibility contract

The exact API surface listed in spec.md §7 (binding). Existing
`tests/test_v2_artifacts.py` assertions are preserved where they
express the contract (202 shape, polling to terminal, content
download markers, capabilities, idempotent DELETE, 404s); the
in-memory `JobManager._reset` fixture is replaced by a durable
fixture (scratch PG, Spec 025 pattern); status projection keeps the
`completed`/`running`/`failed` strings clients already see.

## 4. Risks and mitigations

- LLM provider call inside `basic_report` is provider-cost →
  non-restart-safe classification + non-retryable handler failures
  (no automatic replay; conservative manual_review on uncertainty).
- Collection-dependent output path: preserved; recorded in result
  metadata; download resolves via the same provider (no behavior
  change).
- Orphaned legacy output files after restart: today 404-on-download;
  unchanged (documented boundary).
- Concurrency: two concurrent renders for the same artifact_id
  cannot occur (artifact_id is server-generated per submission;
  idempotency key NULL; each job has a distinct file name).

## 5. Owner-authorization note

**ACCEPTED by the owner on 2026-10-05** (explicit acceptance event
recorded in ADR-031). The former open decisions in spec.md §8 are
now binding owner decisions: (1) retrieval semantics preserved with
server-resolved, durably validated collection context; (2) artifact
files are NEVER deleted by job retention; (3) no client idempotency
key — a canonical input fingerprint is persisted for diagnostics/
reconciliation; (4) ADR-031 is Core-owned. Additional binding
clarifications (result-metadata facility via the bounded
`jobs.result_metadata` column; provenance sidecar for the
finalized→crash window; deterministic media types; no automatic
handler retries) are recorded in spec.md §8.
