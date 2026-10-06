# Spec 028: PostgreSQL-backed knowledge and ingestion metadata

- **Status:** ACCEPTED — revision 2 (2026-10-05).  Revision 1 was
  presented and returned **CHANGES_REQUESTED**; revision 2 applies the
  owner's binding architectural decisions (cohort, schema/stream
  names, one-row-per-point manifest, KB registry PostgreSQL
  authority, namespaced source identity, corrected dedup semantics,
  version-serving visibility mechanism, inline operation evidence
  without a relay outbox, authority/readiness gating with NO legacy
  fallback switch, adoption in phase with a gated live sequence).
  Explicitly accepted for implementation by the owner on 2026-10-05.
  Implementation authorized per Constitution §42.
- **Order of authority:** Retriva constitution v1.2 (canonical,
  `retriva-core/.agent/rules/retriva-constitution.md`) → ADR-033
  (PROPOSED, revision 2) → this spec → architecture.md → plan.md /
  tasks.md / acceptance.md → code.
- **Registry:** specification 028 and ADR-033, allocated before first
  presentation; the CHANGES_REQUESTED → revision-2 cycle is recorded
  in the registry notes.  Statuses remain `proposed`.
- **Implements:** the knowledge/ingestion metadata persistence
  follow-up deferred by Spec 024 (spec §5, plan.md §9) and ADR-029
  (§Consequences), on the accepted durable Core jobs subsystem
  (Spec 025, ADR-030) and the post-Spec-027 v2 surface.
- **Baseline:** current accepted tree — retriva-core
  `4db115a82117cee3a549f4bb4716e70afaae45d7`, retriva-crm-assistant
  `8399063d1d01d4c22ed2a423ae9ee9ab42cea2c6`,
  retriva-messaging-extension `d12b38e94d4780ebee61539dc6a5d3665e3ddd39`,
  retriva-local-containerized-deployment
  `b7da2c020ea9545de482aed2ad18711abfc11637` (branch
  `centralized_relational_db`); live dev stack deploys core
  `4db115a…`.
- **Related:** ADR-029/030/031/032; Specs 014 (user metadata), 015
  (dedup collection awareness — its collection-scoped idempotency
  property is preserved; its content-hash identity collapse is
  classified LEGACY and corrected), 016 (GraphRAG — out of scope,
  §25), 025 (durable jobs), 026 (artifact jobs — OUT of this
  cohort), 027 (v1 decommissioning); Constitution §7, §19, §20, §22,
  §26, §28, §32, §36, §44.

---

## 1. Objective

Make PostgreSQL the authoritative system of record for the identity,
provenance, lifecycle, versioning, and relational state of ingested
knowledge content and its ingestion operations, while:

- Qdrant remains the vector database and the vector system of record;
- the durable Core jobs subsystem (`jobs` schema, ADR-030) remains
  the authoritative asynchronous lifecycle store;
- PostgreSQL does NOT become a vector database;
- after cutover, Qdrant is no longer the sole source of truth for
  document identity or lifecycle.

## 2. Scope (binding, per owner review)

### 2.1 Accepted first cohort

Exactly three workflows, all served by ONE common Core-owned
knowledge-domain service with workflow-specific adapters (no three
separate relational lifecycles):

1. generic v2 document ingestion (`POST /api/v2/documents`,
   job type `v2_document`);
2. v2 upload ingestion (`POST /api/v2/documents/upload`,
   job type `v2_upload`);
3. v2 MediaWiki ingestion (`POST /api/v2/documents/mediawiki`,
   job type `v2_mediawiki`).

Artifact generation (`v2_artifact`) is OUTSIDE the knowledge-metadata
domain (it creates no knowledge documents and no Qdrant vectors) and
is not part of this cohort.  The design supports BOTH Celery
execution and the existing local durable fallback through the
accepted jobs architecture (Spec 025 §3.10) — one implementation, one
state machine, `execution_transport` is the only difference.

### 2.2 Out of scope (binding)

Moving embeddings into PostgreSQL; replacing Qdrant; a platform event
bus; a relay outbox (inline durable per-operation evidence IS in
scope, §11); GraphRAG SQLite migration and ANY graph schema/write-
ordering change (§25); connector cursor/state migration; Messaging
provider-contract integration; CRM campaign/persistence changes;
gateway API redesign; API v1 restoration; UI redesign; automatic
schedulers; production HA/backup/DR/secrets redesign; unrelated
refactoring; CRM/Messaging schema changes; public
adoption/reconciliation endpoints; a broad public document-history
API; rollback-to-prior-version public API; boolean legacy-authority
fallback switches (§17).

## 3. Current state (verified)

Verified at commit `4db115a` (evidence pointers in architecture.md
§2).  Facts this revision builds on:

1. Identity today: content-hash-derived for uploads/MediaWiki
   (`doc_`+sha256(kb:hash)[:32], `dedup.py:49-57`) with identical-
   content collapse; absolute source path as `doc_id` for
   source_uri ingestion (`chunker.py:256,338`).  The collapse is
   LEGACY behavior conflicting with Constitution §7 — corrected by
   this spec (§8), with NO compatibility mode that continues
   content-hash identity for new native ingestion.
2. De-facto catalog: JSON file `dedup_catalog.json`
   (`dedup.py:64-105`) — lock-protected whole-file rewrite, no
   transactions, no RLS, not covered by DB backup, never reconciled.
3. KB authority: SQLite `registry.db` table `knowledge_bases`
   (PK `(kb_id, collection_name)`; columns kb_id, collection_name,
   name, description, created_at, updated_at, settings_json;
   `registry_db.py:54-63`).  KB membership is a LIST
   (`user_metadata.kb_ids`) — many-to-many.
4. Qdrant: one ContextVar collection per request (default
   `retriva_chunks`; the live deployment uses a tenant-specific
   collection name); point id =
   md5(`<canonical>_<idx>`); payload carries text, doc_id,
   source_path(s), page_title, section_path, chunk_id/index/type,
   language, image_path, ingestion_timestamp/status, created_at,
   user_metadata (kb_ids + user keys), content_hash(+algorithm);
   NO tenant field, NO payload indexes, NO aliases, NO
   embedding-model stamp, NO `wait` on upserts; batch 100, 3
   bounded retries; delete-by-filter (doc_id / kb / metadata).
5. Deletion residue: document DELETE leaks the catalog record;
   metadata-filter delete skips graph cleanup; KB delete cascades
   vectors+catalog+registry; errors swallowed to 204; MediaWiki
   changed-page re-sync accumulates orphan identities; source_uri
   re-ingestion leaves stale tails (no delete-before-upsert).
6. Durable jobs: payloads in `jobs.jobs.input_metadata`;
   v2_document/v2_mediawiki have no `subject_id`; ingestion handlers
   write no `result_metadata`; no document→job correlation surface
   (only v2_upload sets subject_id).
7. Tenant: server-resolved fixed tenant for jobs
   (`RETRIVA_JOBS_DEFAULT_TENANT`, Spec 025 §3.12); NO tenant field
   in vector payloads/filters/retrieval (verified absence).
8. Live Qdrant content is legacy, catalog-less or catalog-JSON-only;
   adoption (§14) is required in this phase, gated by the §22
   sequence.

## 4. Requirements

### 4.1 Authoritative relational domain
PostgreSQL schema `knowledge`, stream `core.knowledge`, provider
`retriva-core`, is the system of record for: knowledge-base
registry (§5.1, replacing SQLite after cutover), sources, documents,
KB memberships, document versions, ingestions, per-chunk manifests,
and Qdrant operation evidence.  The stream depends explicitly on
`core.platform` (ledger/bootstrap) and `core.jobs` (migration
ordering, role readiness, and lifecycle availability — NOT a data
dependency): NO cascading relational FKs from knowledge records into
job-history tables; knowledge records survive job retention.  No
domain tables in `platform` or `jobs`; no `pro.*` dependency or
import.

### 4.2 Identity rules (§7/§8 — corrected)
- Logical document identity = `tenant + normalized namespaced source
  identity` (NOT content).  Version identity = logical document +
  content fingerprint + processing contract (parser/extractor +
  embedding contract).  Server-generated opaque uuid-hex ids are the
  public identifiers; source identity is the stable logical key.
- Distinct sources with identical content remain distinct documents
  (Constitution §7).  Identical resubmission of the same source with
  the same processing contract resolves to the same version
  idempotently.  Changed content, or a changed parser/embedding
  contract that can change vector output, creates a NEW version.
  Metadata-only changes do NOT create a content version unless the
  retrieval payload or processing identity changes.
- Chunk point-id derivation is a persisted version property
  (`chunk_id_seed` + chunk-contract version); identifiers stay
  opaque across stores (§19).  Legacy adopted ids and point IDs are
  preserved as legacy evidence; adoption never rewrites Qdrant
  point IDs.

### 4.3 Qdrant visibility (§10)
Version-aware serving filter in Qdrant payloads: every point carries
tenant, kb membership, document_id, version_id, and a serving
marker; ordinary retrieval excludes non-serving versions via a
static payload filter; promotion never creates a window in which
neither the prior nor replacement version is searchable; no
per-search or per-result PostgreSQL query.

### 4.4 PostgreSQL↔Qdrant operation evidence (§11)
Relational state + inline durable per-operation evidence
(prepared/executing/applied_unverified/verified/failed/
reconciliation_required).  Crash-window rule: never assume an intent
row means Qdrant did NOT execute; verify by deterministic point
ids/counts; replay only proven absence of idempotent operations;
manual review when unprovable.  Relay outbox remains an escalation
path only.

### 4.5 Deletion, tombstones, purge (§18)
`active → delete_pending → (async Qdrant removal) → zero-vector
verified → deleted tombstone → optional operator purge`.  Normal API
deletion becomes asynchronous once PostgreSQL is authoritative;
idempotent; failed/partial deletion is reconciliation work; no
hard-delete before zero-vector verification; metadata-filter and KB
deletion are bounded batches; adopted_uncertain records cannot be
automatically deleted; purge is operator-only, dry-run default;
job retention never deletes knowledge records; source files and
artifacts remain independently owned.

### 4.6 Adoption (§14/§15) and catalog/registry retirement (§13/§16)
Hybrid adoption (catalog-first evidence priority) is PART of this
phase; live application is gated by the §22 sequence and a later
deployment prompt.  Provenance classes: native / adopted_verified /
adopted_uncertain, with §15 restrictions.  `dedup_catalog.json` and
SQLite `registry.db` are frozen (never deleted) as rollback
evidence; authority cutover is explicit, evidenced, fail-closed
(§17); silent JSON fallback is prohibited; indefinite dual writes
are prohibited.

### 4.7 Tenant isolation and security (§24)
`tenant_id NOT NULL` on every tenant-owned table; forced RLS;
fail-closed context; Spec 024 roles (`retriva_migrator` owner,
`retriva_core` minimal DML/sequence/function privileges, runtime no
DDL, Pro roles denied, PUBLIC nothing); explicitly privileged
maintenance/adoption paths; database-enforced immutability where
used; bounded source references and user metadata; connector
credentials never enter metadata; logs/metrics never contain
content, secrets, or high-cardinality tenant/document labels;
backups classified sensitive.

### 4.8 API compatibility (§23)
Existing v2 request/response contracts preserved; approved additive
optional fields ONLY: `document_id`, `version_id`, `ingestion_id`,
`sync_state`; no raw source references, storage paths, operation
records, manifests, Qdrant point IDs, or uncertainty internals
exposed; provenance claims honest; no public adoption/reconcile
endpoint; no broad document-history API; no API v1; OpenAPI synced.

### 4.9 Content policy (§19)
PostgreSQL stores identities, fingerprints, bounded metadata, media
type/size, safe source/storage references, contract versions,
manifest evidence, operation evidence, provenance/lifecycle — never
document bodies, chunk text, embeddings, or full Qdrant payloads.
Reindex/restore limitations for non-durable source bytes are stated
explicitly (§19).

### 4.10 Observability and operator tooling (§21)
Bounded metrics/logs (no content/secrets/high-cardinality labels);
operator commands: `knowledge status | adopt [--dry-run|--apply] |
reconcile [--dry-run|--apply] | verify <id> | purge [--dry-run|
--apply]` — tenant-explicit, batch-bounded, resumable, idempotent,
safe-by-default, structured output/exit codes.  No schedulers.

## 5. Acceptance summary

Full gates in acceptance.md §A–L: governance/migration (clean +
existing-DB + order matrix + idempotent rerun + checksum drift +
concurrent runner + downgrade guard + ownership/grants); domain and
concurrency (uniqueness, immutable versions, promotion, failed
replacement preserving prior, concurrent same-source ingestion,
duplicate callbacks, state monotonicity, provenance classes); RLS/
security probes; KB registry migration (dry run, apply, idempotency,
conflicts, cutover, freeze, no indefinite dual write); adoption
(catalog-first, Qdrant validation, verified/uncertain, no vector
mutation, interruption/resume, conflict report, mixed retrieval,
authority gate); Qdrant visibility (prior version visible during
replacement, partial replacement invisible, promotion correctness,
no empty window, failed-replacement cleanup, adopted visibility,
tenant/KB filtering, no relational N+1); operation evidence
(crash windows, absence-proven replay, duplicates, partial batch,
stale tails, restore mismatch); deletion/purge; durable jobs/API;
real integration (real PostgreSQL/Qdrant/Celery, local fallback,
Redis loss, API restart, clean and restored DBs, isolated adoption,
retrieval equivalence, Pro/Messaging compatibility, no Core→Pro
import).

## 6. Owner decisions status

Revision-1 open decisions D1–D15 are RESOLVED by this owner review
(cohort; names; one-row-per-point manifest; hybrid adoption;
corrected dedup semantics without compat mode; promotion semantics;
inline op evidence; deletion model; no content storage; operator
tooling now; dedup baseline disposition; GraphIndexer deferred;
adoption in phase with gated live sequence; additive API fields; KB
registry migration this phase).  Remaining (non-blocking, §30 of the
re-presentation): operational identity naming for CLI privilege
boundaries, readiness-surface field naming, purge retention default
— recorded as recommendations; the only hard gate is explicit
acceptance of this revision and the later authorized live
adoption/cutover deployment instruction (§22).
