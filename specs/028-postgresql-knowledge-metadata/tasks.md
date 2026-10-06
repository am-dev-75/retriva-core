# Spec 028 — Tasks

- **Status:** ACCEPTED — revision 2 (2026-10-05; revision 1 returned
  CHANGES_REQUESTED, owner decisions applied).
- Boundaries per Constitution §44: only the accepted scope; phase
  gates per §37/§38; no scheduler; no Pro schema/code changes; no
  GraphRAG changes; NO boolean legacy-authority fallback.

## P0 — Governance
- T0.1 Record owner acceptance in spec/ADR status sections; registry
  statuses → `accepted` (same governed change).
- T0.2 Apply recorded non-blocking decision outcomes (operational
  identity naming; readiness field naming; purge retention default).

## P1 — Migration stream (`core.knowledge`)
- T1.1 `src/retriva/knowledge/migrations.py` provider + `sql/
  V001__knowledge_foundation.up.sql` (+ down): schema; tables
  knowledge_bases, sources, documents, kb_memberships,
  document_versions, ingestions, version_chunks, qdrant_operations,
  authority; CHECKs; UNIQUE/partial-unique indexes per architecture
  §14; forced RLS + policies; grants (migrator owner; `retriva_core`
  USAGE+DML+sequences only; PUBLIC/Pro denied); `op_state` regression
  trigger; single-active-replacement partial index; downgrade guard
  (refuses when native rows exist).
- T1.2 Register provider after `core.jobs` in
  `CORE_STREAM_PROVIDER_MODULES` (ordering/role-readiness dependency;
  NO cascading FKs into job-history tables).
- T1.3 Tests: clean DB; existing-DB upgrade (restored live copy);
  idempotent rerun; checksum drift detection; concurrent runner
  (advisory lock); order matrix (`platform → jobs → knowledge`
  permutations); ownership/grants/RLS/denied-Pro/runtime-DDL probes
  (pattern: Spec 025 acceptance §B).

## P2 — Domain package
- T2.1 `knowledge/domain.py` (state machine + transition guards),
  `repository.py` (parameterized SQL, fail-closed tenant context),
  `service.py` (common knowledge service), `authority.py`
  (readiness/authority state), `ids.py` (namespaced source-identity
  normalization per architecture §4).
- T2.2 Domain tests: allowed/forbidden transitions; state
  monotonicity; duplicate-callback no-ops; terminal immutability;
  version uniqueness (document+fingerprint+contract); provenance
  classes; authority fail-closed behavior; no silent legacy
  fallback (config probe).

## P3 — KB registry PostgreSQL migration
- T3.1 `knowledge/kb_registry.py`: dry-run read/validate of SQLite
  `registry.db`; idempotent apply; conflict report; verify
  counts/mappings; runtime read/write cutover switches; SQLite
  freeze + post-freeze write refusal; retained as rollback evidence
  (never deleted).
- T3.2 Tests: dry run; apply; idempotency; conflicts; id/mapping
  preservation; cutover; freeze; no indefinite dual write;
  active-kb-id partial-unique semantics.

## P4 — Cohort integration (one common service; three adapters)
- T4.1 Submission adapters (v2_document / v2_upload / v2_mediawiki):
  namespaced source resolution (upload/mediawiki/path-legacy
  namespaces); ONE transaction creating/resolving source, document,
  memberships, version (or idempotent existing-version resolution),
  ingestion row, manifest seeds; durable job submit with
  `subject_type='document'`, `subject_id=document_id` for ALL
  cohort types; response shapes preserved (additive fields only).
- T4.2 Handler adapters: stage transitions (parsing/embedding/
  indexing); `chunk_id_seed` + chunk-contract persistence;
  per-batch `qdrant_operations` evidence + `version_chunks` sync
  flips; expected/observed counts; parse-checkpoint resume
  integration; cancellation checkpoints unchanged; verified
  completion gate; sanitized failures; Celery + local fallback
  parity (same code path).
- T4.3 Transaction/error boundaries per architecture §15 (Qdrant
  calls always outside PG transactions; evidence before/after).
- T4.4 Tests (real PostgreSQL/Qdrant/Celery + local fallback):
  happy path; partial batch failure; duplicate delivery; callback
  loss + crash windows (§12 protocol); Qdrant unavailable;
  PostgreSQL unavailable; idempotent rerun; job retention
  independence (purge jobs, knowledge rows survive).

## P5 — Qdrant visibility mechanism
- T5.1 Payload writer: native points gain `document_id`,
  `version_id`, `serving=false` at upsert, `tenant_id`, top-level
  `kb_ids`; payload indexes (version_id, serving, document_id,
  kb_ids) at collection init.
- T5.2 Shared filter builder: static serving clause
  (serving=true OR serving absent) in search/discovery paths;
  NO per-search/per-result PostgreSQL queries.
- T5.3 Promotion sequence (architecture §6): verify → flip new
  serving → PG promote transaction → flip old serving → async
  evidenced superseded cleanup; crash-window tests (each step).
- T5.4 Tests: prior version visible during replacement; partial
  replacement invisible; no empty visibility window; failed
  replacement cleanup; promotion correctness; tenant/KB filtering;
  retrieval equivalence.

## P6 — Versioning + deletion
- T6.1 Promotion/supersession (P5.3) + metadata-only update records
  (payload-patch op evidence, no new version); changed-contract ⇒
  new version (parser/embedding contract stamps).
- T6.2 Deletion: async tombstone sequence (202 + correlated job);
  zero-vector verification; idempotent repeats; metadata-filter and
  KB deletion as bounded batch tombstones + evidenced cleanup;
  adopted_uncertain protection; `knowledge purge --dry-run|--apply`
  (privileged, operator-only).
- T6.3 Tests: tombstone sequence; repeat idempotency; partial
  delete → reconciliation; batch bounds; purge dry-run/apply; job
  retention independence; rollback limits documented.

## P7 — Adoption (isolated first; live via plan §4)
- T7.1 `knowledge adopt` CLI (dry-run default): evidence priority
  catalog → Qdrant scan → KB mapping → reliable job correlation →
  explicit uncertainty; preserves point IDs; metadata-only payload
  patch (visibility fields); no vector rewrites/deletes; no
  re-chunk/re-embed; batched + resumable + idempotent; conflict +
  uncertainty reports; suspension/rollback before cutover.
- T7.2 Tests: catalog-first; Qdrant validation; verified vs
  uncertain classification; interruption/resume; partial failure;
  conflict report; mixed legacy/native retrieval; authority gate
  behavior; provenance honesty in API output.

## P8 — Reconciliation + operator tooling + authority cutover
- T8.1 `knowledge reconcile --dry-run|--apply` (missing/orphan/
  stale-tail/fingerprint mismatch/restore divergence classifications
  per architecture §12 protocol; exit codes per Spec 025
  conventions; bounded; event-logged; no uncertain auto-replay).
- T8.2 `knowledge status` / `knowledge verify <document-id>` /
  authority cutover command (dry-run first; evidence-gated).
- T8.3 Operational identity + privilege checks (maintenance paths
  explicitly privileged; not available to API runtime credentials).
- T8.4 Tests: crash-window protocol; absence-proven replay;
  reconciliation-required; suspension mode; fail-closed startup.

## P9 — API, observability, docs, acceptance
- T9.1 Additive fields (`document_id`, `version_id`, `ingestion_id`,
  `sync_state`) on existing shapes; async-delete compatibility;
  OpenAPI sync; compat suite; no-v1 probes; provenance honesty.
- T9.2 Observability counters/logs (bounded labels).
- T9.3 Docs: persistence architecture + operator runbook + backup
  classification note.
- T9.4 Full acceptance.md execution in isolation (real containers);
  retrieval-equivalence suite; closure audit; truthful report;
  commit deferred to a separate owner instruction.

## P10 — LIVE sequence (only via explicit later deployment prompt)
- T10.1 Execute plan.md §4 steps 10–19 (live backups/snapshot →
  live dry runs → operator review → live apply → reconcile dry run
  → cutover → freezes → post-cutover validation + rollback
  readiness).  Final phase SUCCESS requires this sequence.

## Implementation status — 2026-10-05 (isolated, non-live)

Accepted revision 2 (2026-10-05).  Implemented and validated in
isolation; live adoption/cutover NOT performed.

- P0  DONE  governance acceptance recorded; registry updated;
  non-blocking decisions D1/D2/D3 applied.
- P1  DONE  `core.knowledge` V001 stream (schema, 9 tables, RLS,
  grants, monotonic-operation trigger, database-level downgrade
  guard); registered in `CORE_STREAM_PROVIDER_MODULES`; 12 migration
  tests green (order, idempotent rerun, checksum drift, ownership,
  RLS/grants, Pro/PUBLIC denial, runtime DDL denial, downgrade guard).
- P2  DONE  domain package (ids, contracts, state machines,
  repository, service, authority); focused domain/service tests green
  (identity, distinct-same-content, version reuse/new, immutable
  promotion, failed replacement preserving prior, duplicate callback,
  monotonicity, tenant fail-closed, authority gates).
- P3  DONE  KB registry dry-run/apply/verify + authority-gated runtime
  accessor + post-cutover legacy-write refusal; tests green.  Live
  SQLite not frozen.
- P4  PARTIAL  ONE common `KnowledgeService` with document/upload/
  mediawiki adapters and a schema-presence gate implemented and
  tested; existing v2 endpoint handlers are NOT yet rewired to call
  the adapters (deferred; no live/runtime regression introduced).
- P5  DONE  version-aware serving contract (payload fields, static
  filter clause, 4 payload indexes, incomplete-payload gate) integrated
  into `upsert_chunks`, `search_chunks` (hard + soft recall), and
  discovery; visibility tests green.
- P6  DONE  asynchronous deletion + tombstone + purge tooling
  (retention 90d default; adopted_uncertain protection); tests green.
- P7  PARTIAL  hybrid adoption (catalog-first + Qdrant-only gap scan,
  metadata-only payload patch, preserved point ids, verified/uncertain
  classification, resume/batch, idempotent) implemented and tested;
  KB-registry-mapping and durable-job-correlation evidence layers
  beyond catalog+scan are deferred.
- P8  DONE  reconciliation classifications + operator commands
  (`status/adopt/reconcile/verify/purge/authority`) + privileged
  authority cutover with gates; tests green.
- P9  PARTIAL  additive v2 fields + `knowledge_metadata` readiness
  object implemented; CLI `knowledge` group implemented; OpenAPI
  hand-sync and the 26-item real-container isolated integration
  matrix are NOT completed.
- P10 NOT STARTED  live sequence (correctly gated behind a separate
  deployment prompt; live state untouched).

## Implementation status update — 2026-10-05 (isolated completion)

Supersedes the PARTIAL status above for the items now completed.

- P4  DONE  All three workflows wired to the ONE common knowledge
  service: `v2_document` and `v2_upload` through `process_document_v2`
  hooks (record intent → staged `serving=false` upsert → applied →
  verified promotion → activate new → deactivate prior; job success
  blocked on verification via `KnowledgeVerificationFailed`), and
  `v2_mediawiki` through per-page hooks in the MediaWiki parser
  (`mediawiki:<site>:page:<id>` identity; revision/content change →
  new version; dedup path activates the existing version).  Durable
  job subjects/attempt correlation populated by value.  Validated
  end-to-end with REAL Celery + real PostgreSQL + real Qdrant + real
  Redis (isolated) for upload, generic document, and MediaWiki.
- P7  DONE  Adoption evidence layers 3 (adopted `knowledge_bases`
  mapping validation / mismatch downgrade) and 4 (reliable
  durable-job subject correlation, `v2_upload` terminal succeeded)
  implemented; resume/checkpoint and idempotent rerun tested across
  all layers.
- P9  DONE (public surface)  Runtime request/response models carry the
  additive optional fields; the `knowledge_metadata` readiness object
  is a typed runtime model with invalid-combination validation; the
  tracked `docs/openapi.yaml` is synchronized; no operator command or
  API v1 is public.  NOTE: `openapi.yaml` remains hand-maintained
  (no generator in-repo).
- Real isolated validation DONE: clean + restored-copy migration
  (from the verified pre-Spec-028 backup), role/RLS/downgrade-guard
  probes, real Qdrant serving visibility, Redis-loss recovery, API
  restart, retrieval visibility, 100k-row manifest measurement
  (~284 B/row with indexes), and fail-closed authority cutover.
- PARTIAL/DEFERRED: full rebuilt-Core container matrix (validation
  used host Core processes against real isolated PostgreSQL/Qdrant/
  Redis rather than rebuilt images); PostGIS/Pro-style cross-repo
  container composition.
- P10 NOT STARTED (live)  Correctly gated behind a separate
  deployment prompt; live state untouched.

## Rebuilt-image isolated validation update — 2026-10-05 (third pass)

- OpenAPI (§17): repository evidence shows NO canonical generator;
  Disposition B applied — `tests/test_knowledge_openapi_consistency.py`
  (5 tests) proves deterministic runtime/tracked semantic consistency
  for the Spec 028 surface (additive fields optional on both sides;
  `knowledge_metadata` with exactly the 3 accepted fields; no v1 or
  operator path public).  One pre-existing unrelated runtime path
  (`/api/v2/jobs/{job_id}/cancel`) is untracked (allowlisted).
- Fresh images built from the working tree: `retriva-core:spec028-
  isolated` == `retriva-ingestion:spec028-isolated` (base), and
  `retriva-pro:spec028-isolated` (base + CRM + web-research).  Base
  image verified: knowledge SQL + pipeline import present, no Pro
  package; Pro image imports both CRM and knowledge.
- Durability change (§16): authority cutover now REQUIRES durable
  retrieval-equivalence evidence — a verified `adopt_verify`
  operation id referenced by `set_authoritative`; unit-tested
  (rejected without it; accepted with it).
- Rebuilt-image Celery HTTP matrix (all three adapters): upload,
  generic document, and MediaWiki completed with knowledge rows
  promoted (`serving=true` real Qdrant).  A transient durable-jobs
  `job_attempts` dispatch/claim deadlock interrupted one MediaWiki
  attempt; a clean worker restart recovered it (the deadlock is in
  the durable-jobs lock path, not the knowledge data path).
- HTTP local-transport matrix (Celery disabled): upload, document,
  MediaWiki all completed through the same common service.
- Redis-loss + API-restart (rebuilt, isolated): readiness stayed
  `authoritative`; job/knowledge evidence persisted.
- STILL UNRESOLVED (honest PARTIAL): the full deterministic
  real-Qdrant retrieval-equivalence corpus (differential), Pro
  runtime composition, Messaging runtime compatibility, and the full
  Pro/Messaging container matrix.  Live adoption/cutover pending.

## Fourth pass — 2026-10-05 (deadlock classification + gate status)

- Deadlock evidence capture (code level): `jobs/repository.py` is
  UNMODIFIED by Spec 028. It contains a pre-existing lock-order
  inversion: `claim_for_delivery` locks `jobs` then `job_attempts`
  (documented invariant), while `complete_success` (UPDATE
  job_attempts -> jobs FOR UPDATE) and `complete_failure` (SELECT
  job_attempts FOR UPDATE -> jobs FOR UPDATE) lock in the reverse
  order. The observed deadlock waiters were both on `job_attempts`;
  no `knowledge.*` relation was part of the cycle.
- Formal classification: **D (unresolved)**.  The inverse
  jobs/job_attempts lock ordering exists in repository code unchanged
  from baseline, and no knowledge relation participated in the observed
  cycle (strong source-level evidence supporting B, a pre-existing
  durable-jobs concurrency defect).  The required pristine-baseline
  runtime reproduction has not yet been obtained, so the formal
  classification remains unresolved. Recommended separately-governed follow-up:
  enforce a single job->attempt lock order in the jobs repository
  (out of Spec 028 scope; do not silently fix).
- Still unresolved: deterministic real-Qdrant retrieval-equivalence
  corpus; Pro runtime composition; Messaging runtime compatibility;
  full combined composition.  Live adoption/cutover pending.
