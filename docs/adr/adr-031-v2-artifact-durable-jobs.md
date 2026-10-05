# ADR-031: v2 artifact generation on the durable Core jobs lifecycle

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

## Status

**ACCEPTED** — revision 1 (2026-10-05). Allocated per Constitution
§43 before first presentation as PROPOSED; presented the same day
with Spec 026; the owner **explicitly accepted** this ADR and
Spec 026 for implementation on 2026-10-05, including the placement
of this ADR in Retriva Core and four binding decisions recorded in
Spec 026 §8 (retrieval semantics preserved with server-resolved
durable collection context; artifact files never deleted by job
retention; no client-supplied idempotency key with a persisted
canonical input fingerprint; Core ADR ownership).

**Explicit acceptance event (recorded verbatim in substance,
2026-10-05):** "The owner explicitly ACCEPTED: Spec 026, 'Durable
v2 Artifact Generation Jobs'; ADR-031, 'Durable v2 Artifact
Generation Jobs'; placement of ADR-031 in Retriva Core; the four
owner decisions and all binding implementation clarifications
stated below." (Implementation authorized only after the governance
acceptance record is updated and validated — done before runtime
work; see the governance checks in the Spec 026 validation record.)

**Implementation status (2026-10-05): implemented and validated
within the accepted scope; the executed validation record lives in
Spec 026 acceptance.md §V (focused suites 223 passed total incl.
22 protocol + 13 API-compatibility artifact tests; real container
validation on an isolated Compose project: real Celery completion,
local fallback via the same handler, Redis-loss and API-restart
durability, cooperative cancellation and finalize-wins race,
migration one-shot no-op, Pro/Messaging one-shot compatibility on
the isolated DB; zero Spec 025 regressions). One validation
incident (shared-network attach) was remediated with the live
database verified intact. Nothing committed.**

## Context

ADR-030 made PostgreSQL the authoritative logical job store and
Spec 025 delivered the durable Core jobs subsystem, integrating the
v2 document and MediaWiki ingestion workflows. The v2 artifact
generation workflow (`/api/v2/artifacts`) was explicitly deferred
(Spec 025 §4: "all other workflows … are follow-ups").

Verified current state of the artifact workflow:

1. Job state lives only in the in-process `JobManager` singleton
   (dict + lock, lost on restart). `GET /api/v2/artifacts/{id}`
   returns 404 after an API restart even when the rendered file
   exists on disk.
2. Execution runs exclusively in the API process via FastAPI
   BackgroundTasks; no Celery task exists for artifacts, so artifact
   rendering cannot use deployment workers.
3. Renderers write directly to the FINAL output path: a crash
   mid-render leaves a partial file at the final location with no
   way to distinguish it from a complete artifact.
4. Raw exception strings are stored and surfaced to clients in the
   download 410 detail (violating the sanitized-error contract every
   other v2 workflow follows).
5. There is no attempt model, no retry, no reconciliation, no event
   log, no retention semantics, and no tenant scoping for artifact
   job records.
6. `basic_report` rendering performs an LLM provider call
   (`ask_question`) — a provider-cost operation whose uncertain
   replay must not happen automatically.

## Decision

Migrate the v2 artifact generation workflow onto the accepted
Spec 025 durable jobs lifecycle, with this contract:

1. **One registered job type**: `v2_artifact`, payload contract
   `v2-1`, subject `artifact:<artifact_id>` (server-generated,
   persisted bounded in `input_metadata.artifact_id`), Celery task
   `process_artifact_task` on the existing `ingestion` queue, and
   the BackgroundTasks fallback through the SAME handler.
2. **Restart-safe = False**: `basic_report` incurs provider cost;
   a lost execution (stale running) is classified conservatively —
   attempt `lost`, job `manual_review` (R7/R9). No automatic replay
   of provider-cost work. Handler/renderer failures are non-
   retryable (deterministic re-execution would fail or repeat cost).
3. **Fully durable input**: the bounded request contract
   (artifact_type, format, parameters, user_metadata) is persisted
   in `input_metadata` — the ONLY input the workflow has, making
   same-generation R2 republication complete and safe.
4. **Atomic output finalization**: the renderer writes to
   `<artifact_id><ext>.partial` and the handler finalizes with
   `os.replace` + sha256 + bounded result metadata (storage_ref,
   media_type, size). A partial render can never occupy the final
   name. This repairs a verified defect (direct final-path writes)
   that blocks durable evidence semantics; no product redesign.
5. **Durable-first API**: the durable job id is the returned
   `job_id`; status/content/delete resolve tenant-scoped via the
   jobs service (`input_metadata.artifact_id` match); the 202
   response contract, status-string family, capabilities, and
   discovery surfaces are preserved; the 410 error detail becomes
   sanitized (bounded text change). No public retry endpoint.
6. **Legacy retirement for this flow only**: every `JobManager`
   touch in `v2_artifacts.py` is removed (no dual writes); the
   O(n) source-scan projection is replaced by tenant-scoped durable
   reads. `JobManager` itself remains untouched for the v1 flows.
   Pre-existing in-memory artifact jobs were already lost on
   restart; they remain unresolvable (no transition window).
7. **No schema change**: Spec 025's V001 already stores everything
   (bounded JSONB metadata, attempts, events, retention). The
   migration one-shot remains a no-op.
8. **No deployment change**: same image, same queue, same
   environment; Celery and local transports verified both.

Rejected alternatives:

- **A parallel artifact-specific job framework**: duplicates the
  accepted lifecycle; forbidden by ADR-030's single-authority rule.
- **Restart-safe=True for artifacts**: would auto-replay provider
  cost (`basic_report` LLM call) after process loss; rejected as
  unsafe for uncertain external side effects.
- **Storing rendered bytes in PostgreSQL**: contradicts
  Spec 025/ADR-030 (do not move large payloads into PostgreSQL);
  the file store stays authoritative for bytes, PostgreSQL for
  logical state + bounded references.
- **Adding a public artifact retry endpoint**: contradicts the
  accepted operator-only retry posture.

## Consequences

- Artifact jobs survive API restarts; status and download resolve
  durably; reconciliation covers ambiguous publication and lost
  executions conservatively.
- Partial-render corruption at the final path is eliminated.
- Client-visible error text on failed artifacts becomes sanitized
  (a bounded, intentional text change to the 410 detail).
- The durable job rows follow the standard retention defaults; the
  output FILES remain owned by the artifact store (owner decision
  open in spec.md §8 whether cleanup should also delete the local
  file at purge — default: no).
- `fetch_artifact_data` retrieval scoping is preserved as-is
  (collection-wide; owner decision open in spec.md §8).

## Related

ADR-030 (durable Core jobs — this ADR applies it to the artifact
follow-up), Spec 025 (accepted implementation), Spec 026 pack
(`retriva-core/specs/026-v2-artifact-durable-jobs`). Constitution
§20 (store of record), §30 (auditable mutations), §33 (bounded
error handling), §37 (risky operations and provider cost).
