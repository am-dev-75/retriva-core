-- Copyright (C) 2026 Retriva.
-- Licensed under the Apache License, Version 2.0 (the "License");
-- you may not use this file except in compliance with the License.
-- You may obtain a copy of the License at
--     http://www.apache.org/licenses/LICENSE-2.0
--
-- Retriva Core — Spec 028 (ADR-033): PostgreSQL-backed knowledge and
-- ingestion metadata.  Stream: core.knowledge | Migration V001.
--
-- PostgreSQL becomes the authoritative system of record for knowledge
-- and ingestion metadata (source identity, logical document identity,
-- immutable versions, KB registry, per-point manifests, Qdrant
-- operation evidence, provenance, deletion and authority state).
-- Qdrant remains the vector system of record; durable Core jobs
-- (core.jobs, ADR-030) remain the authoritative asynchronous
-- lifecycle store.  NO document bodies, chunk text, embeddings, or
-- full Qdrant payloads are stored here (Constitution §29/§19).
--
-- Dependencies: core.platform (ledger) and core.jobs (migration
-- ordering, role readiness, lifecycle availability).  There are NO
-- cascading foreign keys into job-history tables: knowledge rows are
-- correlated to jobs by value and survive job retention.
--
-- Tenancy: tenant_id NOT NULL on every tenant-owned table from the
-- first migration; forced RLS keyed on app.current_tenant;
-- fail-closed (an unset context evaluates to NULL, matching no rows)
-- — Constitution §32.  The privileged maintenance GUC
-- app.knowledge_privileged = 'granted' is used ONLY by operator
-- maintenance paths (migrator role), never by the API runtime role.

CREATE SCHEMA IF NOT EXISTS knowledge AUTHORIZATION retriva_migrator;
ALTER SCHEMA knowledge OWNER TO retriva_migrator;

-- ------------------------------------------------------------------
-- knowledge.knowledge_bases — KB registry authority (replaces the
-- SQLite registry.db after cutover)
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge.knowledge_bases (
    tenant_id        TEXT NOT NULL,
    kb_id            TEXT NOT NULL
        CHECK (kb_id ~ '^[a-z0-9][a-z0-9._-]{0,127}$'),
    collection_name  TEXT NOT NULL
        CHECK (collection_name ~ '^[A-Za-z0-9._-]{1,128}$'),
    name             TEXT
        CHECK (name IS NULL OR octet_length(name) <= 512),
    description      TEXT
        CHECK (description IS NULL OR octet_length(description) <= 4096),
    config           JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (octet_length(config::text) <= 4096),
    lifecycle_state  TEXT NOT NULL DEFAULT 'active'
        CHECK (lifecycle_state IN ('active', 'retired')),
    provenance       TEXT NOT NULL DEFAULT 'native'
        CHECK (provenance IN ('native', 'adopted')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, kb_id, collection_name)
);

-- A kb_id is unique per tenant among ACTIVE rows: a retired kb_id may
-- be re-created without collision (matches the SQLite PK semantics
-- for the (kb_id, collection_name) pair after cutover).
CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_bases_active_kb
    ON knowledge.knowledge_bases (tenant_id, kb_id)
    WHERE lifecycle_state = 'active';
CREATE INDEX IF NOT EXISTS idx_knowledge_bases_collection
    ON knowledge.knowledge_bases (tenant_id, collection_name);

-- ------------------------------------------------------------------
-- knowledge.sources — namespaced normalized source identity
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge.sources (
    source_id        TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    source_type      TEXT NOT NULL
        CHECK (source_type IN (
            'upload', 'mediawiki_export', 'connector', 'url',
            'internal', 'adopted')),
    namespace        TEXT NOT NULL
        CHECK (namespace IN (
            'upload', 'mediawiki', 'connector', 'internal', 'path')),
    normalized_ref   TEXT NOT NULL
        CHECK (octet_length(normalized_ref) <= 512
               AND normalized_ref <> ''),
    display_name     TEXT
        CHECK (display_name IS NULL OR octet_length(display_name) <= 512),
    external_ref     TEXT
        CHECK (external_ref IS NULL OR octet_length(external_ref) <= 1024),
    connector_provider TEXT
        CHECK (connector_provider IS NULL
               OR octet_length(connector_provider) <= 128),
    provenance       TEXT NOT NULL DEFAULT 'native'
        CHECK (provenance IN ('native', 'adopted_verified',
                              'adopted_uncertain')),
    lifecycle_state  TEXT NOT NULL DEFAULT 'active'
        CHECK (lifecycle_state IN ('active', 'disappeared', 'deleted')),
    safe_metadata    JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (octet_length(safe_metadata::text) <= 4096),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at       TIMESTAMPTZ,
    CONSTRAINT uq_sources_identity
        UNIQUE (tenant_id, namespace, normalized_ref)
);

CREATE INDEX IF NOT EXISTS idx_sources_tenant_type
    ON knowledge.sources (tenant_id, source_type);
CREATE INDEX IF NOT EXISTS idx_sources_provenance
    ON knowledge.sources (tenant_id, provenance)
    WHERE provenance <> 'native';

-- ------------------------------------------------------------------
-- knowledge.documents — logical document identity (tenant + source)
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge.documents (
    document_id        TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL,
    source_id          TEXT NOT NULL
        REFERENCES knowledge.sources (source_id) ON DELETE RESTRICT,
    title              TEXT
        CHECK (title IS NULL OR octet_length(title) <= 1024),
    lifecycle_state    TEXT NOT NULL DEFAULT 'active'
        CHECK (lifecycle_state IN (
            'active', 'delete_pending', 'deleted', 'retention_hold')),
    current_version_id TEXT,
    serving_generation INTEGER NOT NULL DEFAULT 1
        CHECK (serving_generation >= 1),
    user_metadata      JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (octet_length(user_metadata::text) <= 4096),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at         TIMESTAMPTZ,
    purge_after        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_documents_source
    ON knowledge.documents (tenant_id, source_id);
CREATE INDEX IF NOT EXISTS idx_documents_lifecycle
    ON knowledge.documents (tenant_id, lifecycle_state)
    WHERE lifecycle_state <> 'active';
CREATE INDEX IF NOT EXISTS idx_documents_purge_after
    ON knowledge.documents (tenant_id, purge_after)
    WHERE purge_after IS NOT NULL;

-- ------------------------------------------------------------------
-- knowledge.kb_memberships — many-to-many document↔KB membership
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge.kb_memberships (
    tenant_id       TEXT NOT NULL,
    document_id     TEXT NOT NULL
        REFERENCES knowledge.documents (document_id) ON DELETE CASCADE,
    kb_id           TEXT NOT NULL,
    collection_name TEXT NOT NULL,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, kb_id, collection_name)
);

CREATE INDEX IF NOT EXISTS idx_kb_memberships_kb
    ON knowledge.kb_memberships (tenant_id, kb_id, collection_name);

-- ------------------------------------------------------------------
-- knowledge.document_versions — immutable versions (controlled
-- lifecycle/reconciliation fields only)
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge.document_versions (
    version_id               TEXT PRIMARY KEY,
    tenant_id                TEXT NOT NULL,
    document_id              TEXT NOT NULL
        REFERENCES knowledge.documents (document_id) ON DELETE RESTRICT,
    content_fingerprint      TEXT
        CHECK (content_fingerprint IS NULL
               OR content_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    fingerprint_algorithm    TEXT NOT NULL DEFAULT 'sha256'
        CHECK (octet_length(fingerprint_algorithm) <= 32),
    source_revision          TEXT
        CHECK (source_revision IS NULL
               OR octet_length(source_revision) <= 256),
    parser_contract_version  TEXT NOT NULL
        CHECK (octet_length(parser_contract_version) <= 128),
    embedding_contract_version TEXT NOT NULL
        CHECK (octet_length(embedding_contract_version) <= 256),
    media_type               TEXT
        CHECK (media_type IS NULL OR octet_length(media_type) <= 256),
    content_size             BIGINT
        CHECK (content_size IS NULL OR content_size >= 0),
    storage_ref              TEXT
        CHECK (storage_ref IS NULL OR octet_length(storage_ref) <= 2048),
    chunk_id_seed            TEXT NOT NULL
        CHECK (octet_length(chunk_id_seed) <= 512),
    chunk_contract_version   TEXT NOT NULL
        CHECK (octet_length(chunk_contract_version) <= 128),
    status                   TEXT NOT NULL DEFAULT 'staging'
        CHECK (status IN (
            'staging', 'parsing', 'embedding', 'indexing', 'indexed',
            'index_partial', 'failed', 'superseded', 'retired')),
    chunk_count_expected     INTEGER
        CHECK (chunk_count_expected IS NULL
               OR chunk_count_expected >= 0),
    provenance               TEXT NOT NULL DEFAULT 'native'
        CHECK (provenance IN ('native', 'adopted_verified',
                              'adopted_uncertain')),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    promoted_at              TIMESTAMPTZ,
    superseded_at            TIMESTAMPTZ
);

-- Identical resubmission with identical processing contract resolves
-- to the SAME version (idempotent).  NULL fingerprints (adopted
-- uncertain) are exempt from the uniqueness constraint.
CREATE UNIQUE INDEX IF NOT EXISTS uq_version_identity
    ON knowledge.document_versions (
        document_id, content_fingerprint, parser_contract_version,
        embedding_contract_version)
    WHERE content_fingerprint IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_versions_document
    ON knowledge.document_versions (tenant_id, document_id);
-- Single active replacement per document (concurrent-ingestion
-- serialization aid).  Scoped to versions ACTIVELY being processed
-- (parsing/embedding/indexing), so a sequential resubmission whose
-- predecessor is still unclaimed (staging) can create a new version,
-- while two workers racing to claim the same document are serialized
-- at the processing boundary (the loser observes the winner).
CREATE UNIQUE INDEX IF NOT EXISTS uq_version_active_replacement
    ON knowledge.document_versions (document_id)
    WHERE status IN ('parsing', 'embedding', 'indexing')
      AND provenance = 'native';
CREATE INDEX IF NOT EXISTS idx_versions_status
    ON knowledge.document_versions (tenant_id, status)
    WHERE status NOT IN ('indexed', 'superseded', 'retired');

-- ------------------------------------------------------------------
-- knowledge.ingestions — ingestion/domain evidence (correlated to
-- jobs by value; NOT a cascading FK to job history)
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge.ingestions (
    ingestion_id         TEXT PRIMARY KEY,
    tenant_id            TEXT NOT NULL,
    job_id               TEXT NOT NULL,
    attempt_id           TEXT,
    job_type             TEXT NOT NULL
        CHECK (job_type IN ('v2_document', 'v2_upload', 'v2_mediawiki')),
    document_id          TEXT NOT NULL
        REFERENCES knowledge.documents (document_id) ON DELETE RESTRICT,
    target_version_id    TEXT,
    collection_name      TEXT NOT NULL
        CHECK (collection_name ~ '^[A-Za-z0-9._-]{1,128}$'),
    kb_ids               JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(kb_ids) = 'array'
               AND octet_length(kb_ids::text) <= 4096),
    ingestion_mode       TEXT NOT NULL DEFAULT 'create'
        CHECK (ingestion_mode IN (
            'create', 'reingest', 'metadata_update', 'adopt', 'repair')),
    sync_state           TEXT NOT NULL DEFAULT 'registered'
        CHECK (sync_state IN (
            'registered', 'parsing', 'embedding', 'indexing',
            'indexed', 'index_partial', 'reconciliation_required',
            'delete_pending', 'deleted', 'failed')),
    expected_chunk_count INTEGER
        CHECK (expected_chunk_count IS NULL
               OR expected_chunk_count >= 0),
    observed_chunk_count INTEGER
        CHECK (observed_chunk_count IS NULL
               OR observed_chunk_count >= 0),
    error_code           TEXT
        CHECK (error_code IS NULL OR octet_length(error_code) <= 128),
    error_summary        TEXT
        CHECK (error_summary IS NULL
               OR octet_length(error_summary) <= 512),
    reconcile_after      TIMESTAMPTZ,
    started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_ingestions_job
    ON knowledge.ingestions (tenant_id, job_id);
CREATE INDEX IF NOT EXISTS idx_ingestions_document
    ON knowledge.ingestions (tenant_id, document_id);
CREATE INDEX IF NOT EXISTS idx_ingestions_reconcile
    ON knowledge.ingestions (tenant_id, reconcile_after)
    WHERE reconcile_after IS NOT NULL;

-- ------------------------------------------------------------------
-- knowledge.version_chunks — one bounded row per expected point
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge.version_chunks (
    tenant_id       TEXT NOT NULL,
    version_id      TEXT NOT NULL
        REFERENCES knowledge.document_versions (version_id)
        ON DELETE CASCADE,
    chunk_ordinal   INTEGER NOT NULL CHECK (chunk_ordinal >= 0),
    point_id        TEXT NOT NULL
        CHECK (point_id ~ '^[0-9a-zA-Z-]{8,64}$'),
    chunk_fingerprint TEXT
        CHECK (chunk_fingerprint IS NULL
               OR chunk_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    byte_count      INTEGER
        CHECK (byte_count IS NULL OR byte_count >= 0),
    chunk_contract_version TEXT NOT NULL
        CHECK (octet_length(chunk_contract_version) <= 128),
    sync_state      TEXT NOT NULL DEFAULT 'expected'
        CHECK (sync_state IN (
            'expected', 'applied_unverified', 'verified', 'failed',
            'removed')),
    op_id           TEXT,
    PRIMARY KEY (version_id, chunk_ordinal)
);

-- Point ids are globally unique per collection; tenant-scoped unique
-- key prevents cross-tenant collision.
CREATE UNIQUE INDEX IF NOT EXISTS uq_version_chunks_point
    ON knowledge.version_chunks (tenant_id, point_id);
CREATE INDEX IF NOT EXISTS idx_version_chunks_sync_pending
    ON knowledge.version_chunks (version_id)
    WHERE sync_state <> 'verified';

-- ------------------------------------------------------------------
-- knowledge.qdrant_operations — inline durable per-operation evidence
-- (NOT a relay outbox)
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge.qdrant_operations (
    op_id           TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    ingestion_id    TEXT
        REFERENCES knowledge.ingestions (ingestion_id)
        ON DELETE SET NULL,
    document_id     TEXT,
    version_id      TEXT,
    op_type         TEXT NOT NULL
        CHECK (op_type IN (
            'upsert_batch', 'delete_points', 'delete_document',
            'delete_kb', 'payload_patch', 'adopt_verify')),
    collection_name TEXT NOT NULL
        CHECK (collection_name ~ '^[A-Za-z0-9._-]{1,128}$'),
    batch_no        INTEGER NOT NULL DEFAULT 0 CHECK (batch_no >= 0),
    batch_count     INTEGER NOT NULL DEFAULT 1 CHECK (batch_count >= 1),
    target_summary  TEXT
        CHECK (target_summary IS NULL
               OR octet_length(target_summary) <= 1024),
    expected_count  INTEGER
        CHECK (expected_count IS NULL OR expected_count >= 0),
    op_state        TEXT NOT NULL DEFAULT 'prepared'
        CHECK (op_state IN (
            'prepared', 'executing', 'applied_unverified', 'verified',
            'failed', 'reconciliation_required')),
    attempt_no      INTEGER NOT NULL DEFAULT 1 CHECK (attempt_no >= 1),
    error_code      TEXT
        CHECK (error_code IS NULL OR octet_length(error_code) <= 128),
    error_summary   TEXT
        CHECK (error_summary IS NULL
               OR octet_length(error_summary) <= 512),
    prepared_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    executed_at     TIMESTAMPTZ,
    verified_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_qdrant_ops_pending
    ON knowledge.qdrant_operations (tenant_id, op_state, prepared_at)
    WHERE op_state IN ('prepared', 'executing', 'applied_unverified',
                       'failed');
CREATE INDEX IF NOT EXISTS idx_qdrant_ops_version
    ON knowledge.qdrant_operations (tenant_id, version_id)
    WHERE version_id IS NOT NULL;

-- op_state is monotonic: a verified operation can never regress to an
-- earlier state (evidence must not be rewritten).  Deletion is
-- restricted to the privileged purge path.
CREATE FUNCTION knowledge.forbid_operation_state_regression()
RETURNS trigger AS $$
DECLARE
    _rank CONSTANT jsonb := '{
        "prepared": 0, "executing": 1, "applied_unverified": 2,
        "failed": 3, "reconciliation_required": 4, "verified": 5
    }'::jsonb;
    _old INTEGER;
    _new INTEGER;
BEGIN
    IF OLD.op_state = 'verified' AND NEW.op_state <> 'verified' THEN
        RAISE EXCEPTION
            'knowledge.qdrant_operations.op_state is monotonic: '
            'verified operations cannot change state'
            USING ERRCODE = 'raise_exception';
    END IF;
    _old := (_rank ->> OLD.op_state)::int;
    _new := (_rank ->> NEW.op_state)::int;
    IF _new < _old AND NOT (NEW.op_state = 'reconciliation_required') THEN
        RAISE EXCEPTION
            'knowledge.qdrant_operations.op_state cannot regress '
            '(% -> %)', OLD.op_state, NEW.op_state
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER qdrant_operations_state_monotonic
    BEFORE UPDATE ON knowledge.qdrant_operations
    FOR EACH ROW
    EXECUTE FUNCTION knowledge.forbid_operation_state_regression();

-- ------------------------------------------------------------------
-- knowledge.authority — durable readiness/authority state (single row)
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge.authority (
    singleton          BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    state              TEXT NOT NULL DEFAULT 'schema_ready'
        CHECK (state IN (
            'schema_ready', 'adoption_pending', 'adoption_verified',
            'authoritative', 'suspended', 'reconciliation_required')),
    authoritative      BOOLEAN NOT NULL DEFAULT FALSE,
    native_ingestion_available BOOLEAN NOT NULL DEFAULT FALSE,
    adoption_run_ref   TEXT
        CHECK (adoption_run_ref IS NULL
               OR octet_length(adoption_run_ref) <= 256),
    operator_note      TEXT
        CHECK (operator_note IS NULL
               OR octet_length(operator_note) <= 1024),
    catalog_frozen_at  TIMESTAMPTZ,
    sqlite_frozen_at   TIMESTAMPTZ,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by         TEXT NOT NULL DEFAULT current_user,
    -- Invalid combinations fail at the database boundary:
    -- authoritative=true iff state='authoritative'; native ingestion
    -- available only in authoritative or suspended states, never in
    -- suspended (suspended stops native ingestion).
    CHECK ((state = 'authoritative') = authoritative),
    CHECK (NOT native_ingestion_available
           OR state IN ('schema_ready', 'adoption_pending',
                        'adoption_verified', 'authoritative')),
    CHECK (state <> 'authoritative' OR native_ingestion_available)
);

-- Seed exactly one authority row if absent.
INSERT INTO knowledge.authority (singleton) VALUES (TRUE)
    ON CONFLICT (singleton) DO NOTHING;

-- ------------------------------------------------------------------
-- Row-Level Security: tenant isolation + controlled privileged path
-- ------------------------------------------------------------------
ALTER TABLE knowledge.knowledge_bases    ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge.sources            ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge.documents          ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge.kb_memberships     ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge.document_versions  ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge.ingestions         ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge.version_chunks     ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge.qdrant_operations  ENABLE ROW LEVEL SECURITY;

ALTER TABLE knowledge.knowledge_bases    FORCE ROW LEVEL SECURITY;
ALTER TABLE knowledge.sources            FORCE ROW LEVEL SECURITY;
ALTER TABLE knowledge.documents          FORCE ROW LEVEL SECURITY;
ALTER TABLE knowledge.kb_memberships     FORCE ROW LEVEL SECURITY;
ALTER TABLE knowledge.document_versions  FORCE ROW LEVEL SECURITY;
ALTER TABLE knowledge.ingestions         FORCE ROW LEVEL SECURITY;
ALTER TABLE knowledge.version_chunks     FORCE ROW LEVEL SECURITY;
ALTER TABLE knowledge.qdrant_operations  FORCE ROW LEVEL SECURITY;

-- authority is deployment-global (not tenant-owned); it is readable
-- by the runtime role and writable only by the operator/migrator
-- (privileged GUC set on the maintenance connection).
ALTER TABLE knowledge.authority ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge.authority FORCE ROW LEVEL SECURITY;
CREATE POLICY authority_read ON knowledge.authority
    FOR SELECT USING (TRUE);
CREATE POLICY authority_privileged ON knowledge.authority
    FOR ALL
    USING (current_setting('app.knowledge_privileged', true)
           = 'granted')
    WITH CHECK (current_setting('app.knowledge_privileged', true)
                = 'granted');

CREATE POLICY tenant_isolation ON knowledge.knowledge_bases
    FOR ALL
    USING (tenant_id = current_setting('app.current_tenant', true)
           OR current_setting('app.knowledge_privileged', true)
              = 'granted')
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)
                OR current_setting('app.knowledge_privileged', true)
                   = 'granted');

CREATE POLICY tenant_isolation ON knowledge.sources
    FOR ALL
    USING (tenant_id = current_setting('app.current_tenant', true)
           OR current_setting('app.knowledge_privileged', true)
              = 'granted')
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)
                OR current_setting('app.knowledge_privileged', true)
                   = 'granted');

CREATE POLICY tenant_isolation ON knowledge.documents
    FOR ALL
    USING (tenant_id = current_setting('app.current_tenant', true)
           OR current_setting('app.knowledge_privileged', true)
              = 'granted')
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)
                OR current_setting('app.knowledge_privileged', true)
                   = 'granted');

CREATE POLICY tenant_isolation ON knowledge.kb_memberships
    FOR ALL
    USING (tenant_id = current_setting('app.current_tenant', true)
           OR current_setting('app.knowledge_privileged', true)
              = 'granted')
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)
                OR current_setting('app.knowledge_privileged', true)
                   = 'granted');

CREATE POLICY tenant_isolation ON knowledge.document_versions
    FOR ALL
    USING (tenant_id = current_setting('app.current_tenant', true)
           OR current_setting('app.knowledge_privileged', true)
              = 'granted')
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)
                OR current_setting('app.knowledge_privileged', true)
                   = 'granted');

CREATE POLICY tenant_isolation ON knowledge.ingestions
    FOR ALL
    USING (tenant_id = current_setting('app.current_tenant', true)
           OR current_setting('app.knowledge_privileged', true)
              = 'granted')
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)
                OR current_setting('app.knowledge_privileged', true)
                   = 'granted');

CREATE POLICY tenant_isolation ON knowledge.version_chunks
    FOR ALL
    USING (tenant_id = current_setting('app.current_tenant', true)
           OR current_setting('app.knowledge_privileged', true)
              = 'granted')
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)
                OR current_setting('app.knowledge_privileged', true)
                   = 'granted');

CREATE POLICY tenant_isolation ON knowledge.qdrant_operations
    FOR ALL
    USING (tenant_id = current_setting('app.current_tenant', true)
           OR current_setting('app.knowledge_privileged', true)
              = 'granted')
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)
                OR current_setting('app.knowledge_privileged', true)
                   = 'granted');

-- ------------------------------------------------------------------
-- Grants: retriva_migrator owns; retriva_core minimal runtime DML;
-- no runtime DDL; no PUBLIC privileges; Pro roles receive nothing.
-- ------------------------------------------------------------------
GRANT USAGE ON SCHEMA knowledge TO retriva_core;

GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge.knowledge_bases
    TO retriva_core;
GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge.sources
    TO retriva_core;
GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge.documents
    TO retriva_core;
GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge.kb_memberships
    TO retriva_core;
GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge.document_versions
    TO retriva_core;
GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge.ingestions
    TO retriva_core;
GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge.version_chunks
    TO retriva_core;
GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge.qdrant_operations
    TO retriva_core;
-- The authority row is runtime-readable; transitions are operator
-- (migrator) actions only.
GRANT SELECT ON knowledge.authority TO retriva_core;

-- Runtime role may not create objects in the knowledge schema.
REVOKE CREATE ON SCHEMA knowledge FROM PUBLIC;
REVOKE ALL ON SCHEMA knowledge FROM PUBLIC;

-- Future Core knowledge tables follow the same DML posture.
ALTER DEFAULT PRIVILEGES FOR ROLE retriva_migrator IN SCHEMA knowledge
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO retriva_core;

COMMENT ON SCHEMA knowledge IS
    'Core-owned authoritative knowledge and ingestion metadata (core.knowledge stream; Spec 028; ADR-033). Qdrant remains the vector system of record; core.jobs remains the async lifecycle authority.';
COMMENT ON TABLE knowledge.knowledge_bases IS
    'KB registry authority (migrated from SQLite registry.db at cutover; SQLite frozen as rollback evidence)';
COMMENT ON TABLE knowledge.sources IS
    'Namespaced normalized source identity (upload/mediawiki/connector/internal; path legacy-only)';
COMMENT ON TABLE knowledge.documents IS
    'Logical document identity = tenant + normalized source identity (never content-hash)';
COMMENT ON TABLE knowledge.document_versions IS
    'Immutable versions = document + content fingerprint + processing contract';
COMMENT ON TABLE knowledge.version_chunks IS
    'One bounded manifest row per expected Qdrant point (no text/embeddings/payloads)';
COMMENT ON TABLE knowledge.qdrant_operations IS
    'Inline durable per-operation Qdrant evidence (not a relay outbox)';
COMMENT ON TABLE knowledge.authority IS
    'Durable knowledge authority/readiness state (single row; fail-closed)';
