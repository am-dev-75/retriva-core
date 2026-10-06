# ADR-033: PostgreSQL as the authoritative knowledge and ingestion metadata system of record

## Status

ACCEPTED — revision 2 (2026-10-05).  Revision 1 was presented and
returned **CHANGES_REQUESTED**; revision 2 applies the owner's
binding architectural decisions and is re-presented for explicit
acceptance.  Registered in the project-wide registry (number 033)
before first presentation as PROPOSED (Constitution §43); the
CHANGES_REQUESTED → revision-2 cycle is recorded in the registry
notes.  Explicitly accepted for implementation by the owner on
2026-10-05.  Companion pack:
`retriva-core/specs/028-postgresql-knowledge-metadata/` (ACCEPTED,
revision 2).

## Implementation status

Isolated implementation (2026-10-05): the `core.knowledge` schema and
migration stream, the domain package (identity, state machines,
repository, common service, authority), the Qdrant serving-visibility
contract and static filter, KB-registry migration tooling, hybrid
adoption (four evidence layers), reconciliation, deletion and purge,
operator commands, the additive API fields/readiness object, and the
runtime wiring of all three ingestion workflows (generic document,
upload, MediaWiki) are implemented and validated in isolation.
Validated with real isolated PostgreSQL, Qdrant, Redis, and Celery,
including restored-copy migration, Redis-loss recovery, API restart,
and a 100k-row manifest measurement.  Live adoption/cutover is NOT
performed and requires a separate explicit deployment prompt.

Third pass (2026-10-05): fresh `base`/`pro` images rebuilt from the
working tree and validated in isolation (Core-only migrations, Celery
HTTP ingestion for all three adapters, HTTP local-transport for all
three, Redis-loss/API-restart persistence, real-Qdrant serving
visibility); a deterministic runtime/tracked OpenAPI consistency test
was added (no canonical generator exists); and authority cutover now
requires durable retrieval-equivalence evidence (a verified
`adopt_verify` operation).  Remaining unresolved isolated gates: the
full deterministic real-Qdrant retrieval-equivalence corpus, Pro and
Messaging runtime composition, and the full container matrix.

Related: ADR-029 (shared PostgreSQL platform; module-owned schemas,
streams, roles), ADR-030 (durable jobs — authoritative async
lifecycle), ADR-031 (artifact jobs), ADR-032 (API v1 retirement);
Specs 014 (user metadata), 015 (dedup collection awareness — its
collection-scoped idempotency property is preserved; its
content-hash identity collapse is classified LEGACY and corrected by
this decision), 016 (GraphRAG — out of scope), 025/026/027;
Constitution §7 (identity-preserving documents), §19 (identifier
ownership), §20 (store-of-record declaration), §22 (provenance), §26
(idempotent resumable data movement), §28 (explicit deletion), §32
(security trimming; tenant_id from the first migration), §36
(deployment posture), §45 (licensing).

## Context

After Specs 024–027, PostgreSQL is authoritative for durable job
lifecycle (`jobs` schema) and Pro business domains, but
knowledge/ingestion metadata has NO relational system of record.
Verified current state (commit `4db115a`):

- Document identity is content-hash-derived for uploads and
  MediaWiki pages (`doc_` + sha256(kb_id:content_hash)[:32],
  `ingestion/dedup.py:49-57`), collapsing identical content into one
  document — a deviation from Constitution §7 ("Retriva MUST NOT
  automatically merge, collapse, or deduplicate distinct documents
  solely because their content is identical").  For JSON/CLI
  `source_uri` ingestion no document id exists at all: the absolute
  source path becomes the vector payload `doc_id`
  (`chunker.py:256,338`), so moves/renames silently create new
  identities while old vectors persist.
- The de-facto document catalog is a JSON file
  (`storage/collections/<collection>/dedup_catalog.json`,
  `ingestion/dedup.py:64-105`): lock-protected whole-file rewrite,
  no transactions, no RLS, not covered by database backup/restore,
  never reconciled against Qdrant.
- The KB registry authority is a SQLite file
  (`registry.db.knowledge_bases`, `registry_db.py:54-63`), outside
  PostgreSQL's backup/RLS/roles model.
- Qdrant payloads are the only complete chunk-identity record; the
  store has no tenant field, no payload indexes, no aliases, no
  embedding-model stamp, and upserts without wait/consistency;
  deletion is Qdrant-only and leaves residue (catalog records
  survive document deletion; metadata-filter deletion skips graph
  cleanup; MediaWiki changed-page re-sync accumulates orphan
  identities; source_uri re-ingestion leaves stale tails).
- Durable jobs carry ingestion payloads but no durable document
  history (v2_document/v2_mediawiki have no subject_id; ingestion
  handlers write no result_metadata).
- The live Qdrant collection contains legacy content with no
  relational catalog at all.

Constitution §20 requires every store of record to be declared by
ADR, and the same authoritative fact MUST NOT have two permanent
systems of record.  Today document identity/lifecycle is split
between a JSON file, a SQLite registry, Qdrant payloads, and
(partially) job payloads — undeclared, unreconcilable, and partially
in conflict with §7.

## Decision

1. **Store-of-record transition (§20).**  PostgreSQL becomes the
   authoritative system of record for knowledge and ingestion
   metadata in a NEW Core-owned schema `knowledge` with migration
   stream `core.knowledge` (provider `retriva-core`), depending
   explicitly on `core.platform` (ledger/bootstrap) and `core.jobs`
   (migration ordering, role readiness, lifecycle availability — no
   cascading FKs into job-history tables; knowledge records survive
   job retention).  Qdrant REMAINS the vector system of record.  The
   durable jobs subsystem (ADR-030) REMAINS the authoritative
   asynchronous lifecycle store.  The JSON dedup catalog, the SQLite
   KB registry, and Qdrant-payload-derived identity are demoted to
   derived/compatibility/evidence surfaces at the authority cutover;
   they are frozen (never deleted) as rollback evidence and removed
   as authority only through the accepted implementation's explicit,
   evidenced cutover — never silently.
2. **Accepted cohort.**  Generic v2 document ingestion, v2 upload
   ingestion, and v2 MediaWiki ingestion — served by ONE common
   Core-owned knowledge-domain service with workflow-specific
   adapters (no per-workflow relational lifecycles), on both Celery
   and the local durable fallback.  Artifact generation is outside
   the knowledge-metadata domain (creates no knowledge documents and
   no Qdrant vectors).
3. **Identity.**  Logical document identity =
   `tenant + normalized namespaced source identity`
   (`upload:`, `mediawiki:`, `connector:`, `internal:`; legacy
   `path:` reserved for adopted identities).  Version identity =
   logical document + content fingerprint + processing contract
   (parser/extractor + embedding).  Content fingerprints drive
   deduplication evidence, idempotent same-source resubmission,
   caches, and integrity checks — never identity replacement (§7
   compliance).  NO compatibility mode continues content-hash
   document identity for new native ingestion; the current collapse
   is classified LEGACY and corrected.  Chunk point-id derivation is
   a persisted version property (`chunk_id_seed` + chunk-contract
   version); legacy adopted ids and Qdrant point IDs are preserved
   as evidence; adoption never rewrites point IDs.
4. **Version promotion.**  Immutable versions with verified
   promotion: the prior current version remains visible and serving
   until the replacement is fully indexed and verified; promotion is
   an atomic PostgreSQL transaction with independently reconciliable
   superseded-vector cleanup; failed replacements never invalidate
   the prior version; no public rollback API in the initial
   implementation.
5. **Vector visibility.**  Version-aware serving filter in Qdrant
   payloads: native and adopted points carry `document_id`,
   `version_id`, `serving`, `tenant_id`, top-level `kb_ids`; the
   shared retrieval filter excludes points explicitly marked
   `serving=false` while keeping unmarked legacy points visible; a
   static per-search filter only — NO PostgreSQL query per search or
   per result.  Qdrant payload indexes on the four fields.
6. **Consistency pattern.**  Relational state + inline durable
   per-operation evidence (`prepared/executing/applied_unverified/
   verified/failed/reconciliation_required`); crash-window protocol
   (never assume intent implies non-execution; verify via
   deterministic point ids/counts; replay only proven absence of
   idempotent operations; manual review when unprovable).  NO relay
   outbox and NO platform event bus in this phase (documented
   escalation paths only).
7. **KB registry migration.**  SQLite `registry.db` authority moves
   to `knowledge.knowledge_bases` (tenant_id + RLS; KB ids and
   collection mappings preserved; idempotent dry-run-first
   migration; runtime read→write cutover; SQLite frozen as rollback
   evidence, never deleted at cutover; no indefinite dual writes).
8. **Deletion.**  `active → delete_pending → (async evidenced Qdrant
   removal) → zero-vector verified → deleted tombstone → optional
   operator purge`; API deletion becomes asynchronous once
   PostgreSQL is authoritative; idempotent; adopted_uncertain
   records cannot be automatically deleted; purge is operator-only,
   dry-run default; job retention never deletes knowledge records.
9. **Adoption.**  Hybrid, dry-run-first, batched, resumable,
   idempotent adoption of existing Qdrant content is PART of this
   phase (catalog-first evidence priority: dedup_catalog.json →
   Qdrant scan → KB mapping → reliable job correlation → explicit
   uncertainty).  Provenance classes `native | adopted_verified |
   adopted_uncertain`; adopted_uncertain keeps retrieval available
   (never quarantined by hiding), cannot be automatically
   re-ingested, promoted, deleted, or replayed, and never claims
   verified provenance in API output.  The live sequence (backups,
   isolated dry runs, operator review, apply, reconcile, cutover)
   is mandatory and gated behind a later explicit deployment prompt.
10. **Authority gating.**  Explicit readiness/authority states
    (`schema_ready, adoption_pending, adoption_verified,
    authoritative, suspended`) stored in PostgreSQL, checked at
    startup, fail-closed: before `authoritative`, metadata-dependent
    native ingestion is rejected (explicit pre-cutover posture);
    after `authoritative`, PostgreSQL is the sole runtime authority
    and legacy JSON/SQLite authority is REFUSED.  There is NO
    boolean fallback switch restoring legacy authority (the
    revision-1 kill-switch is removed from the design).  Suspension
    stops new metadata-dependent ingestion while preserving
    retrieval and operator access.
11. **Data minimization (§29).**  The knowledge schema stores
    identities, fingerprints, bounded metadata, media type/size,
    safe references, contract versions, per-chunk manifest evidence
    (bounded reconciliation fields only — one row per expected
    point, NO text/embeddings/payloads/parser output), operation
    evidence, provenance/lifecycle.  Document bodies, chunk text,
    embeddings, and full Qdrant payloads are NEVER stored in
    PostgreSQL.
12. **Tenancy/security.**  `tenant_id NOT NULL` from the first
    migration on every tenant-owned table; forced RLS; fail-closed
    server-side tenant resolution (Spec 025 §3.12 posture); Spec 024
    roles (`retriva_migrator` owner; `retriva_core` minimal
    DML/sequence/function privileges; runtime no DDL; Pro roles
    denied; PUBLIC nothing); maintenance/adoption/purge paths
    explicitly privileged; bounded source references and metadata;
    no connector credentials; no content/secrets/high-cardinality
    labels in logs or metrics; backups classified sensitive.
    Multi-tenant vector isolation remains out of scope (fixed-tenant
    posture unchanged); the relational layer is the authoritative
    anchor for future enforcement.

## Consequences

- Backup/restore of PostgreSQL now carries document identity and
  lifecycle; Qdrant-only restores are detectable and reconcilable;
  PostgreSQL restore with newer Qdrant is classified, never silently
  trusted.
- The dual-write window between PostgreSQL and Qdrant is bounded by
  the state machine + operation evidence + reconciliation; perfect
  synchronous atomicity is NOT claimed (Qdrant has no cross-store
  transactions); operators get deterministic recovery instead.
- Existing deployments MUST run adoption (gated sequence) before
  deletion/reconcile semantics fully cover legacy content; adopted
  records explicitly represent uncertainty.
- The deduplication baseline (Spec 015-era collapse) changes per §7
  compliance; there is NO compatibility mode for new native
  ingestion; known baseline dedup failures remain documented and out
  of scope unless directly affected (new tests must not depend on
  the broken legacy behavior).
- GraphRAG: NO graph SQLite schema, write-ordering, or behavior
  changes; the GraphIndexer FK warning remains a separately governed
  deferred investigation; Spec 028 records identifiers useful for
  future graph reconciliation.
- CRM/Messaging: no schema or code changes; Pro roles receive no
  privileges on `knowledge`.
- Deferred (recorded): connector cursor/state migration;
  KB-registry SQLite deletion (later cleanup instruction);
  multi-tenant vector isolation; schedulers; production HA/DR;
  gateway store migration; embedding-model mass reindex automation.

## Final isolated status (sixth pass)

Automated Qdrant visible-point cutover gate implemented and validated
on real isolated Qdrant (incomplete visible point blocks cutover;
completed metadata allows it; authoritative retrieval disables the
missing-`serving` compatibility branch).  Messaging migration and
runtime validated with the repository-supported configuration
(`RETRIVA_MESSAGING_DATABASE_URL` + `RETRIVA_MESSAGING_DB_*`; the
earlier failure was an invalid isolated invocation using the ini
`driver://` placeholder).  Combined composition (Core + Pro +
Messaging, one isolated database) validated: core.knowledge=1,
pro.crm=9, messaging=0001_initial; Pro role denied on knowledge; core
ingestion works.  Durable-jobs lock-order defect: classification B
accepted for Spec 028 closure by explicit owner decision (source
provenance + consistency evidence); NOT fixed, deferred to a separately
governed durable-jobs concurrency follow-up.  Live adoption/cutover
remains pending separate authorization.

## Combined runtime composition (seventh pass)

Actual CRM Assistant/Pro runtime started as the Pro image's Core API
with the `retriva_crm_assistant` extension (`/api/v2/crm/health` ok;
`/api/v2/crm/pg/health` ok).  Gateway is part of the canonical default
Composition and validated (`/gateway/health` ok; `/api/v2/capabilities`
and `/gateway/system/jobs/{job}` routed to isolated Core; gateway v2
batch ingestion created).  Messaging healthy.  Combined ledgers:
core.knowledge=1, pro.crm=9, messaging=0001_initial.  Pro role denied
on `knowledge`; Core has no Messaging privileges.  Core upload/
document/MediaWiki all completed and indexed.  Durable-jobs lock-order
defect remains deferred (B by owner decision).
