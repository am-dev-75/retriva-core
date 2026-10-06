# Spec 028 — Acceptance criteria

- **Status:** ACCEPTED — revision 2 (2026-10-05; revision 1 returned
  CHANGES_REQUESTED, owner decisions applied).
- A feature is done only when these criteria pass (Constitution
  §37); operational acceptance includes live deployed-interface runs
  with representative data, authoritative-store inspection,
  container-recreation persistence, repeated execution (semantic
  idempotency), failure paths, and reconciliation.  Final phase
  SUCCESS additionally requires the plan.md §4 live sequence.

## A. Governance and migration
1. Registry lifecycle updated only via owner acceptance;
   `test_governance_registry` passes; no accepted historical
   artifacts edited; AGENTS.md untouched.
2. Clean-database bootstrap+migration; existing-database upgrade on
   a restored live copy; restored-copy backup validation; stream
   dependency/order (`platform → jobs → knowledge` matrix)
   converges; idempotent rerun; checksum drift detected; concurrent
   runner serialized (advisory lock); downgrade guard refuses when
   native rows exist; `retriva_migrator` ownership verified.

## B. Domain and concurrency
1. Source/document/version uniqueness enforced (DB probes);
   immutable versions; no content-hash identity for native
   ingestion; namespaced source identity normalization/collision
   tests (upload/mediawiki/connector/internal/path-legacy).
2. Promotion sequence per architecture §6; failed replacement
   preserves the prior current version and its visibility; late
   callbacks never move domain state backward; duplicate completion
   idempotent; concurrent same-source ingestion serialized via the
   single-active-replacement partial unique index (loser observes
   winner); changed parser/embedding contract ⇒ new version;
   metadata-only changes ⇒ no new version.
3. Provenance classes: native / adopted_verified / adopted_uncertain
   with §15 restrictions enforced in code and DB.

## C. RLS and security
1. Missing tenant context fails closed; cross-tenant read/write
   denied; Pro roles denied direct knowledge writes (probe as
   `retriva_application`); runtime DDL denied; operator
   adoption/reconcile/purge paths explicitly privileged and
   unavailable to API runtime credentials.
2. Bounds enforced (user_metadata ≤4 KB, source refs bounded);
   sanitized errors; connector credentials never stored; no
   content/text/embeddings/payloads in `knowledge` (schema probe +
   code review); parameterized SQL only; `op_state` regression
   trigger blocks illegal updates; append/limited-mutation
   enforcement where applicable.

## D. KB registry migration
1. Dry run validates ids/mappings/conflicts without writes; apply
   idempotent; conflicts reported; SQLite KB ids + collection
   mappings preserved verbatim; active-kb-id partial-unique
   semantics; runtime read→write cutover; SQLite frozen (never
   deleted/overwritten at cutover); post-freeze writes refused and
   logged; NO indefinite dual write (authority-gated).

## E. Adoption
1. Catalog-first evidence; Qdrant validation; KB mapping; reliable
   job correlation only; explicit uncertainty classification;
   verified vs uncertain gates.
2. No vector mutation (no re-chunk/re-embed/no point-id rewrite/no
   vector deletion); metadata-only payload patch adds visibility
   fields (assert vector equality pre/post on fixture collections).
3. Dry-run default; batch-bounded; resumable after interruption;
   idempotent rerun; conflict/uncertainty reports; mixed
   legacy/native retrieval consistent; authority gate blocks
   premature cutover; suspension/rollback before cutover.

## F. Qdrant visibility
1. Prior current version remains visible (serving) until the
   replacement is fully verified and flipped; partial replacement
   vectors invisible; promotion flips in the specified order — no
   window in which NEITHER version is visible; the bounded
   both-visible window is asserted and documented.
2. Failed replacement cleanup identifies its vectors by
   version/manifest; adopted legacy points patched (metadata-only)
   and remain visible; unattributed points remain visible via the
   is-empty clause; tenant/KB filtering consistent with today.
3. Retrieval performs NO PostgreSQL query per search or per result
   (code probe + latency-shape test); payload indexes exist for the
   four fields.

## G. Qdrant operation evidence
1. Mutation success + recorded outcome ⇒ verified; mutation success
   + worker crash before recording ⇒ reconciliation verifies via
   deterministic point ids/counts and marks verified WITHOUT replay;
   absence-proven replay only for idempotent ops; duplicate delivery
   safe; partial batch records the exact boundary; stale-tail
   deletion evidenced; restore mismatches classified
   (reconciliation_required when unprovable); sanitized failures.

## H. Deletion and purge
1. Tombstone sequence (transactional intent → async evidenced
   removal → zero-vector verification → deleted tombstone);
   idempotent repeats; partial delete = reconciliation work; no
   hard-delete before verification; metadata-filter and KB deletion
   bounded batches; adopted_uncertain cannot be auto-deleted; purge
   dry-run default + privileged; job retention provably never
   deletes knowledge rows; source files/artifacts independently
   owned; deletion rollback limits documented.

## I. Durable jobs and API
1. All three cohort adapters use the ONE common knowledge service
   (no per-workflow lifecycles); job/attempt correlation by value +
   subject_id population for all cohort types (two-way lookup);
   terminal job success requires domain+Qdrant evidence; operator
   retry preserves ingestion history; local fallback parity.
2. Existing v2 contracts unchanged; additive optional fields only
   (`document_id`, `version_id`, `ingestion_id`, `sync_state`);
   async-delete compatibility once authoritative; no raw source
   refs/storage paths/op records/manifests/point IDs/uncertainty
   internals exposed; provenance honest; no public
   adoption/reconcile endpoints; no broad history API; no API v1;
   OpenAPI synced.

## J. Real integration
1. Real PostgreSQL; real Qdrant; real Celery; local fallback; Redis
   loss (durable status + knowledge state survive); API restart;
   clean and restored DBs; isolated adoption; retrieval equivalence;
   Pro and Messaging stacks healthy (compatibility unchanged); no
   Core→Pro import (probe).

## K. Observability and tooling
1. Bounded counters/logs (no content/secrets/high-cardinality
   labels); `knowledge status/adopt/reconcile/verify/purge` outputs
   structured with documented exit codes; no schedulers.

## L. Operational acceptance and rollback posture
1. Live sequence (plan.md §4) executed via an explicit deployment
   prompt: live backups/snapshot before mutation; live dry runs;
   operator review; live apply; post-adoption reconcile dry run;
   cutover only when all gates pass; runtime switch; catalog +
   registry freeze; post-cutover validation; rollback readiness
   (suspension mode demonstrated: stops new metadata-dependent
   ingestion, preserves retrieval + operator access).
2. Rollback matrix verified: pre-cutover code rollback; suspension
   after cutover; migration downgrade guard; registry rollback
   (SQLite evidence) pre-cutover; failed/partial adoption recovery;
   restore divergence classification.  Database downgrade is NEVER
   presented as safe after native knowledge writes.
3. Reports written outside repos with checksums; formal
   SUCCESS/PARTIAL/ROLLED_BACK/BLOCKED statement.

## Implementation evidence — 2026-10-05 (isolated)

Focused suite (green): `tests/test_knowledge_migration.py` (12),
`test_knowledge_domain.py`, `test_knowledge_qdrant.py`,
`test_knowledge_kb_registry.py`, `test_knowledge_integration.py`,
plus `test_governance_registry.py`, `test_constitution_integrity.py`,
`test_pg_migration_contract.py`, `test_pg_platform_integration.py`,
`test_jobs_persistence.py` — 126 passed in one isolated run.

Full suite: 904 passed / 17 failed / 13 errors.  The 17/13 are the
pre-existing environmental failures (no live Qdrant/OpenAI network;
`RETRIVA_PG_CORE_PASSWORD` unset for tests that do not use the
scratch-cluster fixtures).  The same counts held at the
pre-implementation baseline (859 passed / 17 failed / 13 errors);
the +45 passed are the new Spec 028 focused tests.  No regression.

Not yet satisfied (honest PARTIAL): P4 endpoint wiring; P7 remaining
adoption evidence layers; P9 OpenAPI hand-sync + the real-container
isolated integration matrix; P10 live sequence.  No live state was
modified; no commit/push/merge/tag/PR/release occurred.

## Implementation evidence update — 2026-10-05 (isolated completion)

Focused suite: 145 passed (migration 12, domain/scale, qdrant,
kb_registry, integration, pipeline, api_surface, lifecycle_sim, plus
governance/constitution/migration-contract/platform/jobs).  Full
suite: 927 passed / 17 failed / 13 errors; the 17/13 are the
pre-existing environmental set (no live Qdrant/OpenAI network;
`RETRIVA_PG_CORE_PASSWORD` unset for non-fixture tests) unchanged from
the 859/17/13 baseline; +68 new tests, no regression.

Real isolated services (unique project/ports; live stack untouched):
- upload, generic document, and MediaWiki ingestion completed through
  real Celery workers + real PostgreSQL + real Qdrant + real Redis;
  knowledge rows promoted, Qdrant points `serving=true` with the
  visibility contract, manifest + operation evidence verified.
- Redis FLUSHALL then re-ingest: prior jobs queryable, new ingestion
  completed, no duplicate version/source on resubmit.
- API restart: readiness, completed jobs, identifiers, and serving
  points persisted.
- Restored pre-Spec-028 backup copy: `core.knowledge` V001 applied by
  the normal runner, idempotent rerun no-op, existing job rows intact,
  forced RLS present, downgrade guard refused with native rows.

Manifest measurement: 100,000 rows → heap 14.9 MB, indexes 13.5 MB,
≈284 B/row including indexes (≈149 B/row heap); `ANALYZE` +
`EXPLAIN (ANALYZE, BUFFERS)` reconciliation plan captured.

Live adoption and live authority cutover were NOT executed.

## Rebuilt-image evidence update — 2026-10-05 (third pass)

Fresh `base` + `pro` images built from the working tree; in-image
content verified.  Rebuilt-image matrices passed: Core-only
migrations (clean + idempotent rerun) and API/worker/readiness; Celery
HTTP ingestion for upload, document, and MediaWiki (MediaWiki recovered
after one transient durable-jobs deadlock by worker restart); HTTP
local-transport ingestion for all three; Redis-loss + API-restart
persistence; real isolated Qdrant serving visibility.  OpenAPI
semantic consistency: 5 tests pass (no canonical generator exists;
Disposition B).  Authority cutover requires durable equivalence
evidence.  Unresolved: full real-Qdrant retrieval-equivalence corpus,
Pro/Messaging runtime composition, full container matrix.

## Fourth pass — 2026-10-05

Deadlock classification evidence (code-level): jobs/repository.py is
unmodified by Spec 028 and has a lock-order inversion (claim:
jobs->attempts; complete_success/complete_failure: attempts->jobs);
the deadlock cycle involved only `job_attempts` (no knowledge
relation).  Final disposition **B accepted for Spec 028 closure by explicit owner
decision** (source provenance + consistency evidence); the defect is
deferred to a separately governed durable-jobs follow-up and is NOT
fixed in Spec 028.
Unresolved: real-Qdrant retrieval-equivalence corpus, Pro runtime
composition, Messaging runtime compatibility, combined composition.

## Executed gates — 2026-10-05 (fifth pass, isolated spec028iso3)

- Deterministic real-Qdrant corpus: 13 points covering native,
  replacement (current + staging serving=false + superseded),
  adopted_verified, adopted_uncertain, two same-content distinct
  sources, metadata-filtered, stale-tail, legacy missing-field, orphan,
  conflict-kb; two KBs.  Query corpus run twice pre and twice post:
  deterministic (run1==run2); staging/superseded/stale excluded;
  same-content distinct sources both present; legacy visible
  pre-cutover; adopted_uncertain visible.
- Durable equivalence: a verified `adopt_verify` operation recorded
  (bounded summary incl. corpus fingerprint).
- Cutover rejection matrix: 12 cases (11 gate-missing + no-durable +
  unverified) all REJECTED with authority state unchanged.
- Cutover success: `authoritative=true`, `native_ingestion_available=
  true`; suspension: ingestion rejected, retrieval/operator preserved.
- N+1: 0 PostgreSQL transactions during a retrieval query.
- Pro composition (rebuilt `retriva-pro:spec028-isolated`): CRM
  bootstrap-roles + upgrade applied; `pro.crm` at V009; core.knowledge
  present; `retriva_application` has NO knowledge SELECT while
  `retriva_core` does.
- Messaging: bootstrap succeeded — `messaging` schema owned by
  `retriva_migrator`, coexists with `knowledge`; `migrate` failed with a
  Messaging-side SQLAlchemy `NoSuchModuleError` (isolated DB URL config),
  NOT a Spec 028 defect — UNRESOLVED.
- Deadlock baseline-vs-current harness: NOT executed; formal
  classification remains D.

## Executed gates — 2026-10-05 (sixth pass, isolated spec028iso4)

- Automated Qdrant visible-point gate implemented
  (`knowledge/scan.py`, authority `compute_cutover_scan` +
  `_require_durable_scan_evidence`, `knowledge scan` CLI, authoritative
  retrieval mode in `visibility.with_serving_clause`).  23 focused scan
  tests pass.
- Real isolated Qdrant sequence: legacy visible point missing fields →
  scan detects (incomplete=1) with a durable FAILED `adopt_verify` op →
  cutover REJECTED with state unchanged → point completed via adoption
  patch → scan ok (incomplete=0) with a durable VERIFIED op → cutover
  succeeds (`authoritative=true`) → post-cutover the authoritative
  filter EXCLUDES a reintroduced incomplete visible point while the
  compatibility filter still sees it → authoritative scan reports 0
  incomplete.
- Messaging: root cause of the earlier failure proven —
  `migrations/env.py` requires `RETRIVA_MESSAGING_DATABASE_URL`;
  the prior invocation used discrete vars so Alembic fell back to the
  ini placeholder `driver://`.  With the supported URL +
  `RETRIVA_MESSAGING_DB_*`, bootstrap and `migrate` succeed
  (`0001_initial`, 8 tables, rerun no-op); runtime service healthy
  (`/health` = healthy; `/` = 200); logs clean; core ledgers untouched.
- Combined composition (one isolated DB): core.platform=1, core.jobs=1,
  core.knowledge=1, pro.crm=9, messaging=0001_initial; `retriva_application`
  denied knowledge SELECT; `retriva_core` allowed; 9 forced-RLS
  relations; Core upload/document/MediaWiki all completed and indexed
  with authority `authoritative`.

## Executed runtime-composition gate — 2026-10-06 (isolated spec028iso5)

- Canonical composition discovery: the deployment Compose includes
  `retriva-gateway` in the DEFAULT composition (no profile); CRM
  Assistant is a Pro extension loaded into the Core API process via
  `RETRIVA_EXTENSIONS` (no separate CRM service); Messaging has its own
  `serve` service under the messaging profile.
- Combined migration lifecycle (one isolated DB): core.platform=1,
  core.jobs=1, core.knowledge=1, pro.crm=9, messaging=0001_initial.
- CRM Assistant/Pro runtime (Pro image, `retriva_crm_assistant`
  extension): Core API healthy; `GET /api/v2/crm/health` =
  `{"status":"ok","extension":"retriva-crm-assistant"}`;
  `GET /api/v2/crm/pg/health` = `{"status":"ok","store":"disabled"}`;
  logs clean; `pro.crm` remains V009.
- Role boundary (real probes): `retriva_application` has NO
  SELECT/UPDATE on `knowledge.documents`; `retriva_core` has INSERT;
  PUBLIC none.
- Gateway (canonical composition): `/gateway/health` = healthy;
  `/api/v2/capabilities` routed to isolated Core; `/gateway/system/jobs/
  {job}` returned a completed Core job; gateway v2 ingress
  `POST /api/v2/ingestion/batches` created a batch (`status=active`);
  no `/api/v1` or secrets in logs.
- Messaging runtime: `/health` healthy; migration `0001_initial`;
  `retriva_migrator` can read `messaging.messages`, `retriva_core`
  cannot (no unintended Core→Messaging privilege).
- Combined Core ingestion (Pro API + worker): upload, generic document,
  and MediaWiki all completed with `knowledge.ingestions` indexed;
  authority `authoritative`.
