# Spec 028 — Plan

- **Status:** ACCEPTED — revision 2 (2026-10-05; revision 1 returned
  CHANGES_REQUESTED, owner decisions applied).

## 1. Discovery and review evidence (verification record)

- Discovery pass (revision 1): constitution v1.2 read; registry
  inspected and allocations 028/033 recorded before first
  presentation; accepted packs Specs 024/025/015 + ADRs 029/030
  read; three parallel read-only code investigations produced the
  file:line evidence in architecture.md §2; governance-test
  conventions verified (`tests/test_governance_registry.py`);
  baselines verified clean.
- Revision-1 governance gates: `test_governance_registry.py` +
  `test_constitution_integrity.py` → 14 passed; full core suite
  859 passed with a pre-existing environment-dependent failure set
  proven identical on pristine HEAD (`git stash -u`/`pop` proof).
- Revision-2 pass (this revision): owner CHANGES_REQUESTED decisions
  applied; NO runtime/SQL/Qdrant/SQLite/live-state changes; registry
  notes updated; all gates re-run (see tasks/acceptance and the
  re-presentation §31).

## 2. Phases (post-acceptance; Constitution §37/§38 gating)

- P0 — Record acceptance; registry statuses → accepted; apply any
  recorded non-blocking decision outcomes.
- P1 — Migration stream `core.knowledge` V001 (schema, tables,
  indexes per architecture §14, RLS, grants, triggers, guards) +
  clean/existing-DB/rerun/order-matrix/ownership tests.
- P2 — Knowledge domain package (domain, repository, service,
  state machine, authority/readiness state) + domain tests.
- P3 — KB registry PostgreSQL migration tooling + runtime cutover
  switches (read → write → freeze) with SQLite preserved.
- P4 — Cohort integration (one common service + three adapters:
  v2_document, v2_upload, v2_mediawiki): submission transaction
  (source/document/version/ingestion/manifest seeds + job subject),
  handler stage transitions, per-batch op evidence, chunk manifest
  writes, verified completion gate; Celery + local fallback parity.
- P5 — Qdrant visibility mechanism (payload fields, payload indexes,
  serving filter in the shared filter builder, promotion sequence,
  superseded cleanup) + retrieval-equivalence tests.
- P6 — Versioning/deletion (promotion, supersession cleanup,
  async tombstone deletion, metadata-filter + KB batch deletion) +
  purge CLI.
- P7 — Adoption tooling (catalog-first + Qdrant scan + KB mapping +
  job correlation; conflict/uncertainty reports; resume) — applies
  in ISOLATION first, live only via §4 sequence.
- P8 — Reconciliation + operator CLIs (`status/adopt/reconcile/
  verify/purge`) + readiness/authority cutover command (dry-run
  first).
- P9 — API additive fields + OpenAPI; observability; docs; full
  acceptance execution (isolated, real containers); closure audit;
  report.
- P10 — LIVE adoption/cutover sequence (§4 below) — only through an
  explicit later deployment prompt; final phase SUCCESS requires it.

Parallelism: P2 after P1's SQL interface is fixed; P3 independent
of P4/P5; P7 after P3+P2; P5 after P4; P6 after P5; P8 after P7;
each phase gates behind its own criteria (no weakening of rollback
or acceptance).

## 3. Rollback design (summary; full matrix in acceptance.md §L)

Forward-only migrations; destructive `core.knowledge` downgrade
refuses when native rows exist; NO boolean legacy-authority
fallback — instead the authority state machine (`suspended` stops
new metadata-dependent ingestion while preserving retrieval and
operator access); SQLite registry and JSON catalogs remain frozen
rollback evidence; adoption is idempotent/resumable; Qdrant payload
fields are additive residue if rolled back.

## 4. Mandatory live existing-data migration sequence (owner §22)

Implementation completion may precede live cutover; final SUCCESS
for the full phase requires, IN ORDER (live steps only via an
explicit deployment prompt):

1. Clean-database migration validation.
2. Existing-database migration validation.
3. Restored-copy backup validation.
4. KB registry dry run in isolation.
5. KB registry apply in isolation.
6. Qdrant/catalog adoption dry run in isolation.
7. Adoption apply + interruption/resume tests in isolation.
8. Retrieval-equivalence verification.
9. Authority-cutover simulation in isolation.
10. Fresh live PostgreSQL backup + Qdrant snapshot.
11. Live KB registry dry run.
12. Live knowledge adoption dry run.
13. Explicit operator review of conflicts/uncertainty.
14. Live apply ONLY through a later deployment prompt.
15. Post-adoption reconcile dry run.
16. Authority cutover only when all gates pass.
17. Runtime reads/writes switch to PostgreSQL.
18. JSON catalog + SQLite registry freeze.
19. Post-cutover validation + rollback readiness.

## 5. Deployment impact (§27 of the phase brief)

New `knowledge` schema + `core.knowledge` stream + provider
registration + migration-order dependency (platform → jobs →
knowledge in the core one-shot); PostgreSQL backup impact
(knowledge data, classified sensitive); Core runtime
authority/readiness behavior; operator commands (§21 of the phase
brief); Qdrant payload/index changes (architecture §6); NO new
database/service/queue; NO CRM/Messaging schema changes;
deployment-repo changes only if accepted one-shots/configuration
require them (decided at implementation, explicitly listed then).

## 6. Deferred (recorded, not implemented)

GraphRAG SQLite migration + GraphIndexer FK investigation
(separately governed); connector cursor migration; KB-registry
SQLite deletion; manifest purge automation; multi-tenant vector
isolation; schedulers; production HA/DR/secrets; gateway store
migration; embedding-model mass reindex automation.

## 7. Open owner decisions (post-revision-2)

Non-blocking recommendations only: operational identity naming for
CLI privilege boundaries; readiness-surface field naming; purge
retention default.  Hard gates: explicit acceptance of revision 2;
the later authorized live adoption/cutover deployment prompt (§4).
