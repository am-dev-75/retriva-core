# ADR-032: Retire API v1 and legacy in-memory job authority

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

**ACCEPTED** — revision 3 (2026-10-05). Revision 1 (2026-10-05)
proposed applying the durable-jobs lifecycle to the legacy v1
ingestion routes; revision 2 narrowed it to one bounded-text route
after CHANGES_REQUESTED; the owner then DIRECTED that Retriva API v1
may be dropped, SUPERSEDING the durable-migration direction.
Revision 3 records the decommissioning decision. The owner
**explicitly accepted** Spec 027 revision 3 and this ADR for
implementation on 2026-10-05 with the binding decisions: hard
removal of API v1 (Option A, ordinary 404; no 410 router, no
configuration gate); complete JobManager retirement after
shared-symbol relocation (`CancellationError`, `JobStatus`) and
removal of the v2/parser legacy fallbacks; removal of dead Redis
job-state code with natural key expiry (no flush, no destructive
deletion); removal of the raw `AsyncResult` fallback; CLI updated to
supported v2 routes only; **temporary loss of standalone CLI image
ingestion explicitly accepted** (no v2 equivalent; v2-native image
ingestion is deferred governed work); **no revision of the
unrelated Spec 014 mission block in AGENTS.md** (the order-of-
authority reference remains; a future governed update may record the
retired surface); no PostgreSQL migration; no deployment runtime
change; no commit/push/release within this phase (a separate owner
instruction authorizes commits).

Revisions 1–2 are preserved as superseded history in the registry
notes and Spec 027; they were never ACCEPTED and were not rewritten.

**Implementation status: IMPLEMENTED AND VALIDATED (2026-10-05).**
All eight decision points executed: (1) nine v1 routers hard-removed;
routes return ordinary 404; no 410 router, no flag. (2) Shared
symbols relocated (`CancellationError`, `JobStatus` →
`retriva.ingestion_api.execution`; user-metadata validation →
`retriva.ingestion_api.metadata_validation`; `DeleteMetadataRequest`
→ `schemas_v2`). (3) `JobManager` module deleted; v2/parser legacy
fallbacks removed; durable-only resolution with bounded 404 (503 on
degraded durable read). (4) Redis legacy helpers deleted; keys expire
naturally; Redis retained for Celery transport. (5) Durable v2
workflows, shared ingestion internals, `CollectionMiddleware`, the
openai_api surface, gateway, and connectors unchanged. (6) Retirement
note + release guidance published (`docs/ingestion-api.md`); OpenAPI
cleaned (zero v1 paths); `implementation.md` updated; AGENTS.md
untouched per explicit owner decision. (7) Grep/call-graph gates are
permanent tests (`tests/test_spec027_decommissioning.py`). (8) No
schema migration; no deployment change. Validation: full battery 851
passed with all failures proven baseline-identical at HEAD; isolated
container run passed all 17 checks (real Celery completions for
document/mediawiki/artifact, local fallback, Redis-loss + restart
durability, Pro/Messaging one-shots, migration no-op, live stack
verified untouched). Commit deferred to a separate owner
instruction. Full detail: Spec 027 acceptance.md §V and
`/mnt/devel/retriva/tmp/spec027-final-report.md`.

## Context

ADR-030 made PostgreSQL the authoritative logical job store;
Spec 025 delivered the durable Core jobs subsystem (v2
document/mediawiki/upload); Spec 026 (ADR-031) migrated v2 artifact
generation. Spec 025 §4 named the legacy v1 flows as follow-ups, and
Spec 027 revisions 1–2 proposed migrating them. The owner has now
directed that API v1 may be dropped instead.

Verified state (discovery 2026-10-05, baseline `e4701fa`; commands
and the full dependency matrix in Spec 027 plan.md §1):

1. Nine v1 routers (`/api/v1/ingest/*` incl. synchronous collection
   delete, `/api/v1/jobs` list/get/cancel, `/api/v1/documents`
   deletions) run on BackgroundTasks with in-memory `JobManager`
   state, writer-less Redis shims (`retriva:job:*`, `retriva:retry:*`,
   `retriva:cancel:*`), and a raw Celery `AsyncResult` fallback.
2. NO external dependency: the gateway uses only `/api/v2/...`
   (`core/client.py:136–161`); connectors are gateway-mediated;
   WebUI, web-research, IAM, CRM, Messaging have zero Retriva-v1
   references; deployment health checks hit TCP ports and `/health`
   only. The sole internal consumer is the Core CLI, which already
   has v2 branches for text/pdf/markdown/upload and v1-only paths
   for html, image, mediawiki pages/assets, and collection delete.
3. `JobManager` hosts shared symbols used by v2 durable execution
   (`CancellationError`, `JobStatus`); `schemas.py` hosts shared
   validation symbols used by v2 (`validate_user_metadata`,
   `UserMetadataValidationError`, `DeleteMetadataRequest`).
4. v2 read-side fallbacks to legacy state (`v2_jobs.py:106`,
   `v2_documents.py:401`, `mediawiki_v2_parser.py:299`) can only
   ever resolve pre-Spec-025 v1-era ids.
5. No PostgreSQL object is used only by v1; no v1-only environment
   variable exists; Redis itself is Celery transport and remains.

The problem: the v1 surface is an unscoped, tenant-free, ephemeral
API whose only supported consumer is an internal CLI already
mid-migration to v2 — dead weight that blocks `JobManager` removal
and keeps unsafe surfaces (unscoped job list/cancel, raw-exception
storage) alive.

## Decision

PROPOSED decision (binding only upon acceptance):

1. **Hard removal of API v1 (Option A).** Stop registering all nine
   v1 routers; delete the v1 endpoints, request/response models,
   BackgroundTasks handlers, CLI v1 call paths, and scratch scripts.
   Removed paths return ordinary 404. No 410 retirement router and
   no configuration gate (the sole verified consumer is internal and
   co-versioned; a 410 phase benefits only uncoordinated external
   callers, of which none exist).
2. **Shared-symbol relocation before deletion.** `CancellationError`
   and `JobStatus` move out of `job_manager.py`; the user-metadata
   validation family moves out of `schemas.py`; v2 imports are
   updated. Durable v2 execution semantics are unchanged.
3. **Complete `JobManager` retirement.** After v1 removal and
   fallback removal there are ZERO supported writers or readers:
   delete the module (singleton, models, projection, cancel state)
   and its tests. The three v2/parser legacy fallbacks
   (`v2_jobs.py:106`, `v2_documents.py:401`,
   `mediawiki_v2_parser.py:299`) are removed; v2 surfaces become
   durable-first with plain 404 for unknown ids.
4. **Redis cleanup without destruction.** Delete the legacy
   helper/shim code; existing keys expire naturally (7-day / 24-hour
   TTLs); NO flush, NO key deletion, Redis stays as Celery
   transport.
5. **Preserve supported surfaces.** Durable v2 workflows
   (document/mediawiki/artifact), the Core jobs subsystem, shared
   ingestion internals (parsers, chunker, `upsert_chunks`),
   `CollectionMiddleware`, the openai_api surface, the gateway, and
   connectors are untouched (import updates only where symbols
   moved). The CLI's standalone image-ingestion path has no v2
   equivalent and is retired with v1 (owner-acknowledged gap;
   a v2 image surface would be a future governed change).
6. **Documented, not preserved, history.** Removed routes, v2
   replacements, permanently-unresolvable pre-removal v1 job ids
   (no synthetic durable records), OpenAPI changes, and the required
   service restart are documented in a release note;
   `docs/openapi.yaml` loses its 14 v1 path items;
   `docs/ingestion-api.md` becomes a retirement note; AGENTS.md's
   Spec 014 order-of-authority block is revised as a governed doc
   change; accepted historical specs remain records.

## Consequences

- The unscoped v1 job surface and its raw-exception storage paths
  disappear; every remaining asynchronous surface is durable,
  tenant-scoped, and reconciliable.
- `JobManager` and the legacy Redis shims are deleted rather than
  carried as dead compatibility code; old keys expire naturally.
- Client impact: none known externally; the internal CLI is migrated
  in the same change; undocumented v1 callers would receive 404.
- Rollback is a pure version-control revert (no schema, no Redis
  destructive step, no gateway change); durable state is never
  deleted.
- Pre-removal v1 job ids become unresolvable — consistent with
  their pre-existing restart-loss behavior.

## Related

- Spec 027 pack revision 3
  (`specs/027-api-v1-decommissioning/`) — binding detail, dependency
  matrix, removal inventory, acceptance gates. Revisions 1–2
  (durable migration of v1) are superseded by owner direction.
- ADR-030 / Spec 025, ADR-031 / Spec 026 — the durable jobs
  subsystem and its v2 adopters, preserved unchanged.
- Future governed change (optional): v2-native image ingestion for
  the retired CLI image path.
