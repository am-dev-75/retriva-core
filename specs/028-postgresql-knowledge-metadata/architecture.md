# Spec 028 — Architecture: PostgreSQL-backed knowledge and ingestion metadata

- **Status:** ACCEPTED — revision 2 (2026-10-05; revision 1 returned
  CHANGES_REQUESTED, all owner decisions applied here).
- **Order of authority:** constitution → ADR-033 rev 2 → spec.md
  rev 2 → this document → plan/tasks/acceptance → code.

## 1. Target architecture

```
                 ┌───────────────────────────────────────────────┐
                 │            PostgreSQL (database `retriva`)    │
                 │  platform.schema_migrations      (core.platform)
                 │  jobs.jobs / job_attempts / job_events
                 │      (core.jobs — authoritative async lifecycle,
                 │       ADR-030)
                 │  knowledge.knowledge_bases   ← KB registry authority
                 │  knowledge.sources                            │
                 │  knowledge.documents          AUTHORITATIVE   │
                 │  knowledge.kb_memberships     document/source │
                 │  knowledge.document_versions  /version/       │
                 │  knowledge.ingestions         ingestion/      │
                 │  knowledge.version_chunks     manifest/       │
                 │  knowledge.qdrant_operations  op evidence     │
                 │      (core.knowledge — NEW, Core-owned)       │
                 └──────────────┬────────────────────────────────┘
                                │ idempotent, evidenced mutations
                 ┌──────────────▼───────────────────────────────┐
                 │ Qdrant collection(s): vectors + payloads +    │
                 │ payload indexes (vector system of record)     │
                 └───────────────────────────────────────────────┘
```

- One common knowledge-domain service (`src/retriva/knowledge/`,
  with `adapters.py` for the three cohort workflows); Celery and the
  local durable fallback share it unchanged (Spec 025 §3.10).
- SQLite `graph.db`, session store, artifact store, gateway JSON
  stores, and connector SQLite state keep their current roles.
- The KB authority moves from SQLite `registry.db` to
  `knowledge.knowledge_bases` (§7 of this document); SQLite is frozen
  as rollback evidence, never deleted.

## 2. Verified current state (evidence pointers, commit `4db115a`)

| # | Fact | Evidence |
|---|------|----------|
| 1 | Content-hash identity + collapse (LEGACY, §7 conflict) | `dedup.py:43-57,109-121`; `v2_documents.py:1160-1271` |
| 2 | source_uri path identity | `v2_documents.py:234-236,630`; `chunker.py:256,338` |
| 3 | JSON dedup catalog | `dedup.py:64-105`; `DocRecord` `domain/models.py:83-103` |
| 4 | SQLite KB registry schema | `registry_db.py:54-63` |
| 5 | Point ids / full payload keys | `chunker.py:254,336`; `qdrant_store.py:111-143`; `domain/models.py:19-41` |
| 6 | kb_ids list membership + filtering | `metadata_validation.py:64-76`; `qdrant_store.py:209-221,643-656` |
| 7 | Upsert config (batch 100, retry 3, no wait) | `qdrant_store.py:56-100,111-117`; `config.py:217` |
| 8 | Deletion behavior + residue | `v2_documents.py:1360-1431`; `v2_kbs.py:227-315`; `qdrant_store.py:338-402,461-472` |
| 9 | Stale tails / no delete-before-upsert | full read `process_document_v2` (`v2_documents.py:367-828`) |
| 10 | MediaWiki identity/dedup/orphans | `mediawiki_v2_parser.py:84-138,140-176,221-244` |
| 11 | Durable jobs + correlation gaps | `jobs/sql/V001__jobs_foundation.up.sql`; `durable_jobs.py:618-746`; `v2_jobs.py:85-88` |
| 12 | Tenant resolution (jobs only) | `jobs/tenant.py:118-152`; `durable_jobs.py:607-615` |
| 13 | Chunk sizing (scale inputs) | `config.py:218-219` (`max_chunk_chars=2000`, `chunk_overlap=200`) |
| 14 | Gateway/connector identity maps | gateway `core/json_source_repository.py`, `source_models.py:197-206`; connectors' `state_store.py` |

## 3. Domain model (schema `knowledge`; all tables forced RLS, tenant_id NOT NULL)

### 3.1 knowledge.knowledge_bases (KB registry authority; replaces SQLite after cutover)
`kb_id` bounded slug; `tenant_id`; `collection_name` bounded (Qdrant
collection identity); `name`, `description` bounded; `config` JSONB
bounded ≤4 KB (safe configuration only — no secrets); `lifecycle_state`
CHECK (`active | retired`); `provenance` CHECK (`native | adopted`);
`created_at/updated_at`.  UNIQUE `(tenant_id, kb_id,
collection_name)` — preserves the SQLite PK semantics; `kb_id` alone
is UNIQUE per tenant among `active` rows (enforced via partial unique
index `WHERE lifecycle_state='active'`) so a retired KB id can be
re-created without collision.  Migration/rollback: §7 below.

### 3.2 knowledge.sources
`source_id` uuid-hex PK; `tenant_id`; `source_type` CHECK
(`upload | mediawiki_export | connector | url | internal | adopted`);
`namespace` bounded (the §4 namespace token); `normalized_ref` TEXT
NOT NULL bounded (≤512; normalized namespaced identity, §4);
`display_name` bounded (mutable, never identity); `external_ref`
bounded nullable (raw external reference as evidence, e.g. MediaWiki
site+page ids); `connector_provider` bounded nullable;
`provenance` CHECK (`native | adopted_verified | adopted_uncertain`);
`lifecycle_state` CHECK (`active | disappeared | deleted`);
`safe_metadata` JSONB ≤4 KB; `created_at/updated_at/deleted_at`.
UNIQUE `(tenant_id, namespace, normalized_ref)`.

### 3.3 knowledge.documents
`document_id` uuid-hex PK (public id); `tenant_id`; `source_id` FK →
sources (RESTRICT); `title` bounded; `lifecycle_state` CHECK
(`active | delete_pending | deleted | retention_hold`);
`current_version_id` uuid-hex nullable (by value — no hard FK, §3.5);
`serving_generation` INT NOT NULL DEFAULT 1 (monotonic, §6
visibility); `user_metadata` JSONB ≤4 KB (Spec 014 semantics);
`created_at/updated_at/deleted_at`; `purge_after` timestamptz
nullable (snapshotted at `deleted`, mirrors Spec 025 §3.9
philosophy).  Forced RLS.

### 3.4 knowledge.kb_memberships
`document_id` FK; `kb_id` bounded; `tenant_id`; `collection_name`;
`added_at`.  PK `(document_id, kb_id, collection_name)`.
Many-to-many (verified: payloads carry `kb_ids` lists).  Membership
changes propagate to Qdrant payload `kb_ids` via evidenced payload
patches (bounded); KB deletion (§9) removes memberships in the same
relational transaction as tombstones.

### 3.5 knowledge.document_versions
`version_id` uuid-hex PK; `document_id` FK; `tenant_id`;
`content_fingerprint` TEXT NOT NULL (`sha256:<hex>`; NULL permitted
ONLY for `adopted_uncertain`); `fingerprint_algorithm` bounded;
`source_revision` bounded nullable (MediaWiki revision id, connector
revision, mtime/etag-class evidence); `parser_contract_version`
bounded NOT NULL; `embedding_contract_version` bounded NOT NULL
(model id + dimension + normalization contract, stamped from
config at ingestion time — fixes the current absence);
`media_type` bounded; `content_size` BIGINT nullable;
`storage_ref` bounded nullable (safe reference only; NOT required —
upload source bytes are transient; §10);
`chunk_id_seed` bounded NOT NULL (deterministic seed from which
Qdrant point ids derive; legacy values adopted verbatim);
`chunk_contract_version` bounded NOT NULL (chunker contract that
defines ordinal→id derivation);
`status` CHECK (`staging | parsing | embedding | indexing | indexed |
index_partial | failed | superseded | retired`);
`chunk_count_expected` INT nullable; `created_at`; `promoted_at`
nullable; `superseded_at` nullable.  UNIQUE `(document_id,
content_fingerprint, parser_contract_version,
embedding_contract_version)` on non-NULL fingerprints (partial
unique index) — identical resubmission with identical processing
contract resolves to the SAME version (idempotent); changed content
OR changed processing contract ⇒ new version.  `documents.
current_version_id` is maintained by the promotion transaction
(value reference + transition-service invariant checks).

### 3.6 knowledge.ingestions
`ingestion_id` uuid-hex PK; `tenant_id`; `job_id` uuid-hex NOT NULL
(by value, indexed — NOT a cascading FK; job retention purges
independently); `attempt_id` uuid-hex nullable; `job_type` CHECK
(`v2_document | v2_upload | v2_mediawiki`); `document_id` FK;
`target_version_id` uuid-hex nullable; `collection_name` bounded;
`kb_ids` bounded list; `ingestion_mode` CHECK (`create | reingest |
metadata_update | adopt | repair`); `sync_state` CHECK (§5 state
machine); `expected_chunk_count` INT nullable;
`observed_chunk_count` INT nullable; `error_code`/`error_summary`
bounded sanitized; `reconcile_after` timestamptz nullable;
`started_at/completed_at`.  Forced RLS.

### 3.7 knowledge.version_chunks (one row per EXPECTED Qdrant point)
`tenant_id`; `version_id` FK; `chunk_ordinal` INT NOT NULL;
`point_id` bounded NOT NULL (the Qdrant point id — md5 hex from the
seed today; adopted values verbatim); `chunk_fingerprint` bounded
nullable (chunk-level sha256 where computed); `byte_count` INT
nullable (bounded char/byte count); `sync_state` CHECK
(`expected | applied_unverified | verified | failed | removed`);
`op_id` uuid-hex nullable (correlation to the applying operation).
PK `(version_id, chunk_ordinal)`; UNIQUE `(tenant_id, collection-
scoped point id)` → UNIQUE `(tenant_id, point_id)` (point ids are
globally unique per collection; tenant prefix makes the unique key
safe across tenants).  NO text, NO embeddings, NO full payloads, NO
arbitrary parser output.

### 3.8 knowledge.qdrant_operations
`op_id` uuid-hex PK; `tenant_id`; `ingestion_id` FK nullable;
`document_id`/`version_id` bounded nullable correlation;
`op_type` CHECK (`upsert_batch | delete_points | delete_document |
delete_kb | payload_patch | adopt_verify`);
`collection_name` bounded; `batch_no`/`batch_count` INT;
`target_summary` bounded (point-id range or bounded filter digest —
never a full payload); `expected_count` INT nullable;
`op_state` CHECK (`prepared | executing | applied_unverified |
verified | failed | reconciliation_required`); `attempt_no` INT
(reconciliation tries); `error_code`/`error_summary` sanitized;
`prepared_at/executed_at/verified_at`.  NOT append-only: outcome
states must be writable by `retriva_core`; instead a trigger blocks
`op_state` regression (verified → earlier states forbidden) and
deletion is restricted to the privileged purge path.  NOT a relay
outbox: no separate relay process; rows are written inline by the
worker as each operation executes.

## 4. Source identity and namespaces (§7 of the phase brief)

Normalized identity = `namespace ":" normalized_external_ref`,
tenant-scoped by column (never embedded in the string):

| Namespace | Normalized ref rules | Examples |
|---|---|---|
| `upload:` | `upload:<bounded-sha256-of-uploader-context>:<bounded-original-filename-normalized>` — stable per uploader-context+filename; the raw client path is NOT identity | `upload:a1b2…:report-q3.pdf` |
| `mediawiki:` | `mediawiki:<site-identity>:page:<page_id>` (page identity, NOT revision) | `mediawiki:rdwiki:page:12345` |
| `connector:` | `connector:<connector-type>:<external-item-id>` — connector-owned, opaque | `connector:email-agent:<msg-id>` |
| `internal:` | `internal:<stable-logical-reference>` — server-chosen for programmatic sources | `internal:crat-corpus-0007` |
| `path:` (LEGACY ONLY) | adopted legacy source_uri identities recorded verbatim, normalized to forward slashes, length-bounded | `path:/data/wiki/export.xml` |

Rules: lowercase + percent-encoding for unsafe characters; length
bounds; collision prevention by namespace + tenant scoping + the
UNIQUE constraint; immutable identity vs mutable `display_name`;
moves/renames create a NEW source identity by default (the old
source is tombstoned; aliasing old→new is recorded in
`sources.safe_metadata` as evidence, NOT as identity rewrite);
MediaWiki page id is identity, revision id is version evidence;
connector ids are opaque and connector-owned; existing absolute
paths are recorded as bounded provenance evidence only; legacy
adopted identities keep their `path:` namespace; source
disappearance (deleted remote/file) is a source tombstone
(`lifecycle_state='disappeared'`), never an automatic document
deletion.

## 5. Domain state machine (owned by document_versions.status joined
with ingestions.sync_state; the durable jobs machine, Spec 025 §3.2,
remains the ONLY asynchronous lifecycle authority)

```
registered     (ingestions.sync_state) actor: api at submission
               evidence: source/document/version rows + job created
               Qdrant: nothing visible yet; job status: pending/queued
parsing        actor: worker (stage DETECTING→PARSING)
embedding      actor: worker
indexing       actor: worker; evidence: qdrant_operations rows per batch;
               version_chunks rows flip expected→applied_unverified
indexed        TERMINAL success for the version; actor: worker at
               verified completion; REQUIRED evidence: observed count
               == expected count AND all ops verified; triggers the
               §6 promotion transaction
index_partial  actor: worker/reconciler on failed/cancelled indexing;
               evidence: op rows + chunk rows record the boundary;
               job status reflects the durable failure/cancellation;
               replacement vectors stay INVISIBLE (serving=false)
failed         TERMINAL failure (deterministic, before/outside
               indexing); prior current version untouched
delete_pending actor: api/operator; relational tombstone in the SAME
               transaction as the delete-intent op row
deleted        TERMINAL deletion; actor: worker/reconciler AFTER
               verified zero matching vectors
reconciliation_required  evidence insufficient; operator-only exit;
               late/duplicate callbacks never move states backward;
               guarded rowcount updates make delivery idempotent
```

The version's `status` and the ingestion's `sync_state` are joined
only at defined evidence points (claim, batch outcomes, verified
completion); neither is derived from the other, and neither
duplicates the durable job status.  Retry (durable `retry_wait`,
Spec 025 §3.6) resumes the SAME ingestion/version at the recorded
boundary; a new operator retry attempt appends a new attempt
correlation without erasing prior evidence.

## 6. Qdrant vector-visibility mechanism (version-aware serving filter)

Concrete design (owner-preferred option):

- **Payload fields added to every native point** at upsert:
  `document_id`, `version_id`, `serving` (bool), `tenant_id`,
  `kb_ids` (moved to top-level alongside the existing
  `user_metadata.kb_ids` for filter efficiency), plus the existing
  payload keys unchanged.
- **Serving rule in retrieval:** the shared filter builder
  (`qdrant_store.py` `_build_filter` used by `search_chunks`,
  `search_documents`, discovery) adds ONE static clause:
  `should[ MatchValue(serving=true), IsEmptyCondition(serving) ]` —
  points explicitly marked `serving=false` (replacement versions
  under construction, superseded versions pending cleanup) are
  excluded; points with NO serving field (pre-adoption legacy,
  adopted_uncertain before patching) remain visible.  This is a
  static per-search filter — **no PostgreSQL query per search and
  none per result**; promotion changes visibility purely through
  Qdrant payload state, and PostgreSQL records the authoritative
  bookkeeping (`documents.current_version_id`, `serving_generation`)
  for evidence and reconciliation.
- **Qdrant payload indexes** (created at collection init AND during
  adoption for the live collection): `version_id` (keyword),
  `serving` (bool), `document_id` (keyword), `kb_ids` (keyword
  array).  These make the retrieval filter, the
  set_payload-by-filter promotion, and bounded scans efficient.
- **Promotion sequence** (atomically decided in PostgreSQL AFTER
  Qdrant visibility flips):
  1. replacement version fully verified (expected==observed, ops
     verified) while still `serving=false`;
  2. Qdrant: `set_payload(version_id=new, serving=true)` — window
     opens in which BOTH versions are visible (bounded, harmless:
     retrieval may briefly return both; retriever's per-doc
     diversity cap mitigates; documented);
  3. PostgreSQL: single transaction promotes (`documents.
     current_version_id=new`, `serving_generation+1`, version status
     `indexed`→current, prior version `superseded_at`) + supersedes
     prior ingestions evidence;
  4. Qdrant: `set_payload(version_id=old, serving=false)`;
  5. async evidenced cleanup deletes superseded points (bounded
     batches; failure = reconciliation work, NEVER undoing the new
     current version).
- **Crash windows:** crash before step 2 → new version invisible,
  reconcile re-drives (safe re-verify); crash between 2 and 3 → both
  visible, reconcile completes promotion by evidence (new is
  verified ⇒ promote in PG, then flip old); crash between 3 and 4 →
  PG says new, Qdrant shows both → reconcile flips old; crash during
  5 → orphaned old points → reconcile detects by manifest/point-id
  set-difference and deletes.  NO window exists in which NEITHER
  version is visible: the old version keeps `serving=true` until the
  new version is fully visible (steps 2 precede 4).
- **Adopted legacy points:** payload-patched (metadata-only; vector
  values and point IDs untouched) with
  `document_id/version_id/serving=true/tenant_id/kb_ids` during
  adoption; points that cannot be attributed keep NO serving field
  and stay visible via the IsEmpty clause.
- **Tenant/KB consistency:** `kb_ids` top-level + membership table
  kept in sync at metadata updates; tenant_id is recorded but NOT
  used as a retrieval filter in this phase (fixed-tenant posture
  unchanged) — it exists as the authoritative anchor and for future
  enforcement.

## 7. KB registry migration (SQLite → PostgreSQL)

- Model: §3.1.  Migration process (dry-run first, all operator/CLI,
  idempotent):
  1. `knowledge adopt --kb-registry --dry-run`: read
     `registry.db.knowledge_bases`; validate kb_id slug rules
     (`domain/kb.py` regex), collection mappings, duplicates,
     conflicts (same kb_id → different collections across tenants,
     invalid settings JSON);
  2. `--apply`: idempotent INSERT ... ON CONFLICT DO NOTHING +
     conflict report; preserves ids/mappings verbatim
     (`provenance='adopted'`);
  3. verify counts/mappings (report);
  4. runtime reads switch (registry accessor reads PostgreSQL,
     falls back to SQLite ONLY while authority state ≠ authoritative,
     reporting the mode);
  5. runtime writes switch (KB create/delete write PostgreSQL);
  6. SQLite file FROZEN as rollback evidence (never deleted or
     overwritten during initial cutover);
  7. dual writes end at cutover; post-cutover SQLite writes are
     refused and logged as anomalies.
- Uniqueness: `(tenant_id, kb_id, collection_name)`; active-kb-id
  partial unique per tenant.  RLS + grants per §10.

## 8. `dedup_catalog.json` retirement

1. Adoption reads the catalog as EVIDENCE priority 1 (§9).
2. Catalog claims are cross-checked against Qdrant scroll (point
   counts, doc_ids) and KB mappings; recoverable evidence imported.
3. Runtime knowledge writes move to PostgreSQL (submission-time
   rows; no more catalog record creation).
4. Runtime knowledge reads (dedup idempotency, document
   GET/list/status) move to PostgreSQL at authority cutover.
5. Catalog frozen (rename-in-place prohibited; freeze = no more
   reads for authority + recorded freeze timestamp in the authority
   state); files retained until a later explicit cleanup
   instruction.
6. NO indefinite dual writes; post-cutover, the runtime REFUSES
   silent JSON fallback: the authority state row (§11) is
   authoritative, startup checks assert it, and any legacy-catalog
   authority use after `authoritative` raises a fail-closed
   configuration error (tested).
7. Dedup idempotency survives: same-source same-contract
   resubmission resolves through `sources`/`document_versions`
   uniqueness (NOT the JSON catalog).

## 9. Adoption model (hybrid; §14 of the phase brief)

Evidence priority: (1) `dedup_catalog.json` DocRecords; (2) Qdrant
payload scan (scroll, batched, resumable checkpoint); (3) KB
registry mapping; (4) durable job records where correlation is
reliable (v2_upload subject_id); (5) explicit uncertainty.
Guarantees: existing point IDs preserved; vectors never rewritten
(no re-chunk/re-embed; metadata-only payload patch adds the §6
visibility fields); vectors never deleted during adoption; explicit
adopted records; dry-run default; batch-bounded; resumable;
idempotent (UNIQUE constraints); conflict + uncertainty reports;
survives interruption; records progress without text/secrets;
rollback/suspension supported before authority cutover.

## 10. Provenance classes

`native | adopted_verified | adopted_uncertain` (definitions and
gates in ADR-033 §Decision 9).  adopted_uncertain: retrieval stays
available (serving untouched); fingerprint/source-revision/claims
nullable; NO automatic reingestion, promotion, deletion, or replay;
reconciliation reports uncertainty; API output never claims verified
provenance falsely; destructive transitions require operator
evidence.  Quarantine-by-hiding is prohibited (§15 of the phase
brief).

## 11. Authority/readiness states and fail-closed gating

Single-row PostgreSQL state `knowledge.authority` (migrator-created,
runtime-readable, operator-writable via CLI only):
`state ∈ {schema_ready, adoption_pending, adoption_verified,
authoritative, suspended}` + evidence columns (timestamps, adoption
run refs, operator note) + `catalog_frozen_at`/`sqlite_frozen_at`.

- Startup check: read the authority row; unknown/missing ⇒
  fail-closed for metadata-dependent operations (report mode).
- `schema_ready` (fresh DB, pre-adoption): native metadata-dependent
  ingestion is REJECTED (HTTP 409-class sanitized error) — the
  explicit pre-cutover posture; retrieval of legacy content
  unaffected; KB registry may run in read-legacy mode (reported).
- `adoption_pending/verified`: as above; adoption/reconcile CLIs
  advance the state with evidence.
- `authoritative`: PostgreSQL knowledge metadata is the SOLE runtime
  authority; JSON/SQLite authority reads/writes REFUSED (fail-closed
  anomaly); no silent fallback exists — there is NO configuration
  switch that restores legacy authority (the revision-1
  `RETRIVA_KNOWLEDGE_METADATA_ENABLED` fallback boolean is REMOVED
  from the design).
- `suspended`: stops new metadata-dependent ingestion (409),
  preserves existing retrieval and operator access (resume is an
  operator action) — the rollback posture after cutover.
- Readiness surface: `GET /api/v2/capabilities` gains additive
  bounded fields (metadata authority mode + readiness state name —
  no internals); `/health` unchanged.

**Readiness object field naming (non-blocking decision D2):** the
additive object is named `knowledge_metadata` with fields:
`state` (one of the six legal states),
`authoritative` (boolean, true only when `state == 'authoritative'`),
`native_ingestion_available` (boolean, true only when authority and
runtime checks permit native ingestion).  Invalid combinations fail
startup or readiness validation.
- Cutover command: `knowledge status --set-authoritative` style
  operator action gated on required evidence (adoption verified,
  reconcile clean) — implementation detail in tasks.md; dry-run
  first.

## 12. PostgreSQL↔Qdrant operation evidence lifecycle

`prepared → executing → applied_unverified → verified | failed |
reconciliation_required` (§3.8).  Crash-window protocol (owner
§11): NEVER assume an intent row means Qdrant did not execute;
verify via deterministic point ids (`version_chunks.point_id`
retrieve/counts) or bounded filters; mark `verified` without replay
when Qdrant evidence proves success; replay ONLY proven absence of
an idempotent operation (bounded retries); `reconciliation_required`
+ manual review when outcome is unprovable; duplicate delivery safe
(guarded rowcount + idempotent Qdrant ops); sanitized failures.
Relay outbox remains a documented escalation path only.

## 13. Deletion, tombstones, purge

`active → delete_pending` (transactional intent) → async evidenced
Qdrant removal → verified zero matching vectors (count check) →
`deleted` tombstone → optional operator `knowledge purge --dry-run|
--apply` (privileged).  Requirements per §18 of the phase brief:
API deletion becomes ASYNC (202 + correlated durable job of type
`v2_document` with deletion mode) once authoritative; idempotent
repeats; failed/partial deletion = reconciliation work; no
hard-delete before verification; metadata-filter and KB deletion
are bounded batch tombstone selection + the same sequence;
adopted_uncertain protected from automatic deletion; job retention
never deletes knowledge rows; source files/artifacts independently
owned; rollback limits documented (a tombstone can be reversed
pre-purge by operator action; post-purge is irreversible — stated).

**Purge retention default (non-blocking decision D3):** `purge_after`
is snapshotted at the `deleted` transition as `now() + interval '90
days'`.  Configuration changes do not rewrite existing
`purge_after` values silently.  Never age-purge: active records,
pending records, uncertain records, reconciliation-required records,
or `adopted_uncertain` records.  Purge is privileged, manual,
batch-bounded, and dry-run first.  Add no scheduler.

## 14. Per-chunk manifest scale analysis (owner §6)

Inputs: `max_chunk_chars=2000`/`chunk_overlap=200`
(`config.py:218-219`) ⇒ a 100 KB document ≈ 55–60 chunks; a 1 MB
document ≈ 550–600 chunks.

| Quantity | Planning assumption (development scale) | Rationale |
|---|---|---|
| documents per tenant | 10^4–10^5 (100 k) | dev corpus + headroom |
| chunks per document | median ~20; p95 ~600; p99 ~5 000 | 2000-char chunks |
| rows per tenant | ~2×10^6 at 100 k docs | median-weighted; p99 burst bounded by version lifecycle |
| row width | ~110 bytes heap estimate: tenant_id(16) + version_id(16) + ordinal(4) + point_id(32) + fingerprint(64 nullable) + byte_count(4) + sync_state(1) + op_id(16 nullable) + alignment/toast-free padding | no text/JSON in rows |
| table size per tenant | ~220 MB heap + ~250 MB total with indexes | 2 M rows |
| index size | PK btree (version_id, ordinal) ≈ 60 MB; UNIQUE (tenant_id, point_id) ≈ 90 MB; partial sync_state index ≈ 10–30 MB (only non-verified rows) | verified paths only (§14 index list) |
| batch insert | ONE multi-row INSERT … ON CONFLICT (version_id, chunk_ordinal) DO NOTHING per Qdrant batch (≤100 rows/batch ⇒ ≤100 rows/statement), same transaction as the op-evidence row | no per-chunk transactions |
| write volume | steady-state ≈ rows_in == rows_out (supersession deletes manifest rows of retired versions via version FK cascade AFTER verified vector cleanup) | bounded table growth |
| supersession volume | equals replaced chunk count; deletes batched with the same purge batching | |
| adoption volume | one row per existing live point (bounded by collection size; live dev collection is small) | adoption writes are batched + resumable |
| autovacuum | append-mostly with batched deletes ⇒ conventional bloat profile; fillfactor default; no per-chunk transaction churn; monitor `pg_stat_user_tables` | documented operator note |
| retention | manifest rows cascade with their version when a version is purged; purge is operator-gated (§13) | |
| query patterns | (a) missing: `WHERE version_id=? AND sync_state!='verified'` (partial index); (b) orphan: Qdrant point ids of a version set-diffed against manifest point ids (manifest read by PK range); (c) stale-tail: ordinal > expected via PK scan; (d) unique point-id collision probe by UNIQUE index | all PK/index-served |

**Rejected alternatives (recorded):** count-only version manifest
(cannot do set-difference reconciliation without re-deriving the id
generator — fragile against legacy/scheme changes); full payload
mirror in PostgreSQL (violates the content policy and unbounded
growth).  One-row-per-point is the verified choice because deletion,
orphan/stale detection, and integrity verification all need the
exact point-id set.

**Indexes (verified paths only):** PK (version_id, chunk_ordinal);
UNIQUE (tenant_id, point_id); partial btree on
`(version_id) WHERE sync_state != 'verified'`; on versions:
`(tenant_id, document_id)`, partial unique current-version
enforcement helper `(document_id) WHERE status IN
('staging','parsing','embedding','indexing')` (single active
replacement per document — concurrent-ingestion serialization aid);
on sources: UNIQUE `(tenant_id, namespace, normalized_ref)`; on
ingestions: `(tenant_id, job_id)`, `(document_id)`; on
qdrant_operations: `(tenant_id, op_state, prepared_at)` partial
WHERE op_state IN ('prepared','executing','applied_unverified',
'failed'); on kb_memberships: PK + `(tenant_id, kb_id)`.  NO JSONB
GIN indexes; NO speculative indexes.

## 15. Durable-jobs correlation (§20 of the phase brief)

Submission (ONE transaction): resolve/create source + document
(+ memberships); create version (`staging`) or resolve existing
matching version; create ingestion row (`registered`) + manifest
seeds where derivable; submit durable job with `subject_type=
'document'`, `subject_id=document_id` (ALL cohort job types — fixes
today's null subjects) — two-way lookup via the existing
`get_job_by_subject`.  Execution: claim evidence appends
attempt correlation; batch ops flip chunk rows + op rows;
completion requires domain verification gate (§5 `indexed`); job
terminal success without domain evidence is impossible by
construction (the handler completes the job only after the verified
promotion transaction); failures preserve the prior current
version; operator retry = new attempt + new ingestion-mode row
(`repair`), history preserved; late/duplicate callbacks never move
domain state backward; NO job-event copying.  Transaction/error
boundaries: jobs schema writes stay inside the Spec 025
repository/service; knowledge writes are separate transactions
correlated by ids; Qdrant calls are ALWAYS outside PostgreSQL
transactions (evidence rows before/after; §12 protocol).

## 16. Content policy (§19 of the phase brief)

No bodies, no chunk text, no embeddings, no full Qdrant payloads in
PostgreSQL.  Stored: identities, fingerprints, bounded metadata,
media type/size, safe source/storage references, contract versions,
manifest evidence, op evidence, provenance/lifecycle.  Limitations
stated: upload/source_uri/MediaWiki source bytes are transient
(uploads: temp file removed on success; source paths: external
files may vanish) ⇒ reindex/restore of a version is possible only
while its source bytes remain obtainable; adopted versions of
non-durable sources are likewise non-reindexable; the reconciliation
report must classify such versions explicitly instead of silently
replaying.

## 17. API compatibility (§23 of the phase brief)

Existing v2 contracts preserved; additive optional fields ONLY:
`document_id`, `version_id`, `ingestion_id`, `sync_state` (on
ingestion-acceptance responses and job/document projections where a
value exists).  Job responses (`JobResponseV2`) unchanged except
additive fields; document list/GET/delete operations keep shapes
(delete becomes async-202 once authoritative with the job id
additive — the documented compatibility change, gated to the
authoritative state); no raw source refs/storage paths/op
records/manifests/point IDs/uncertainty internals exposed;
provenance honest (`adopted_uncertain` never labeled verified); no
public adoption/reconcile endpoints; no broad history API; no API
v1; OpenAPI synced.

## 18. Observability

Counters (labels: job_type, state/phase, error_code class — NEVER
tenant/document ids): documents by lifecycle/sync state; ingestion
stage latency; Qdrant sync lag (verified_at − executed_at);
index_partial count; reconciliation_required backlog; orphan/missing
vector counts (aggregate); deletion backlog; adoption progress;
failed ops by safe code; promotion failures.  Structured logs with
opaque ids only.

**Operational identity naming (non-blocking decision D1):** privileged
CLI paths (`adopt --apply`, `reconcile --apply`, `purge --apply`,
`status --set-authoritative`) run under the `retriva_migrator`
database role (explicit administrative context, not the API runtime
role `retriva_core`).  Ordinary tenant-scoped read paths (`status`,
`verify`, `adopt --dry-run`, `reconcile --dry-run`, `purge --dry-run`)
run under `retriva_core`.  Administrative commands MUST NOT silently
fall back to runtime credentials.  Global operations require explicit
administrative context.  Tenant-scoped commands require an explicit
tenant.  Report operational mode without printing credentials.  A
dedicated production operator role remains deferred.

## 19. Migration stream and roles

`core.knowledge` provider `retriva-core` (registered after
`core.jobs` in `CORE_STREAM_PROVIDER_MODULES`); V001 creates schema,
tables (§3), indexes (§14), forced RLS + policies, grants, the
`op_state` regression trigger, and the downgrade guard; depends
explicitly on `core.platform` (ledger) + `core.jobs` (migration
ORDERING, role readiness, lifecycle availability — no cascading FKs
into job-history tables); NO `pro.*` dependency/import.  Roles per
Spec 024: `retriva_migrator` owns; `retriva_core` USAGE + SELECT/
INSERT/UPDATE/DELETE on domain tables + sequences (+ the op-evidence
state transitions); NO runtime DDL; Pro roles denied; PUBLIC
nothing; adoption/reconcile/purge run under the explicitly
privileged operational identity (maintenance paths are NOT exposed
to the API runtime role).  Appendix A of acceptance.md pins actual
catalog/role probes.

## 20. GraphIndexer disposition (§25)

OUT of scope: no graph SQLite schema changes, no write-ordering
changes, no GraphRAG behavior changes.  Spec 028 records
source/document/version identifiers that future graph reconciliation
can use (provenance rows already carry them); the pre-existing
GraphIndexer FK failure remains a separately governed deferred
investigation (recorded; not fixed here).

## 21. Database/deployment impact (§27)

New `knowledge` schema + `core.knowledge` stream + provider
registration + ordering dependency; PostgreSQL backups now include
knowledge data (classified sensitive); Core runtime gains the
authority/readiness behavior (§11); one-shot picks up the stream
automatically; operator commands (§ of plan.md); Qdrant changes:
4 payload fields on native/adopted points + 4 payload indexes
(creation is part of implementation, bounded); NO new database
instance, service, or queue; NO CRM/Messaging schema changes;
deployment-repo changes only if accepted one-shots/commands need
them (e.g. a `knowledge-adoption` helper in manage.sh — decided at
implementation).

## 22. Rollback and risks (§29)

- Code before authority cutover: suspension state + code rollback;
  PostgreSQL schema may remain (empty/backfilled only) — no data
  loss.
- Code after cutover: `suspended` state stops metadata-dependent
  ingestion while preserving retrieval + operator access; code
  rollback requires the documented decommissioning sequence
  (`core.knowledge` downgrade is migrator-only, explicitly
  confirmed, destructive-guarded — and unsafe after native writes
  exist EXCEPT as a deliberate data-destroying operation; stated,
  never implied safe).
- PostgreSQL migration: forward-only; downgrade guard refuses when
  native knowledge rows exist (pattern: Spec 025 §3.13).
- KB registry adoption: SQLite remains frozen rollback evidence;
  rollback = re-point runtime to SQLite (allowed ONLY pre-cutover).
- Qdrant payload additions: additive fields; removal unnecessary
  (harmless residue); index removal is a bounded operator action.
- Failed/partial live adoption: adoption is idempotent + resumable;
  suspend → reconcile dry-run → resume/re-adopt; no vector loss
  (adoption never deletes).
- Partial authority cutover: authority state machine requires
  evidence gates; every state is explicit and reported.
- Frozen JSON/SQLite: retained as rollback evidence until a later
  explicit cleanup instruction.
- Restored PostgreSQL with newer Qdrant / restored Qdrant with
  newer PostgreSQL: reconciliation classifies (§ of plan.md);
  uncertain ⇒ reconciliation_required + manual review; automatic
  destructive replay prohibited.

## 23. Explicitly deferred

As spec.md §2.2 (binding list) plus: KB-registry SQLite deletion
(later cleanup instruction); manifest purge automation; multi-tenant
vector isolation enforcement; embedding-model mass reindex
automation; gateway store migration; connector cursor migration.
