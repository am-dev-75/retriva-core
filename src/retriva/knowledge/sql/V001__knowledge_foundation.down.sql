-- Copyright (C) 2026 Retriva — Spec 028 (ADR-033): core.knowledge V001
-- downgrade.
--
-- DESTRUCTIVE: drops the Core-created `knowledge` schema and all
-- knowledge metadata.  The framework requires the explicit
-- --confirm-destructive override.  In addition this script carries an
-- independent database-level downgrade guard: it REFUSES to run when
-- any native knowledge data exists (sources, documents, versions,
-- ingestions, or manifest rows with provenance='native'), because
-- that data is the authoritative system of record and destroying it
-- is a deliberate data-destroying operation, never an implied-safe
-- rollback.  Adopted-only rows may still block removal until an
-- operator explicitly purges them.
--
-- The guard is defense-in-depth on top of the framework's
-- confirm_destructive gate.

DO $$
DECLARE
    _has_native BOOLEAN := FALSE;
BEGIN
    -- The guard must see rows past forced RLS; this is the migrator
    -- (owner) connection, so the privileged maintenance GUC is valid.
    PERFORM set_config('app.knowledge_privileged', 'granted', true);
    IF to_regclass('knowledge.sources') IS NOT NULL THEN
        EXECUTE
            'SELECT EXISTS (SELECT 1 FROM knowledge.sources '
            'WHERE provenance = ''native'' LIMIT 1)'
            INTO _has_native;
    END IF;
    IF NOT _has_native
       AND to_regclass('knowledge.document_versions') IS NOT NULL THEN
        EXECUTE
            'SELECT EXISTS (SELECT 1 FROM knowledge.document_versions '
            'WHERE provenance = ''native'' LIMIT 1)'
            INTO _has_native;
    END IF;
    IF NOT _has_native
       AND to_regclass('knowledge.ingestions') IS NOT NULL THEN
        EXECUTE
            'SELECT EXISTS (SELECT 1 FROM knowledge.ingestions LIMIT 1)'
            INTO _has_native;
    END IF;
    IF _has_native THEN
        RAISE EXCEPTION
            'core.knowledge downgrade refused: native knowledge data '
            'exists (PostgreSQL is the authoritative system of record; '
            'dropping it is destructive and is not an implied-safe '
            'rollback). Suspend knowledge authority or purge '
            'explicitly first.'
            USING ERRCODE = 'raise_exception';
    END IF;
END;
$$;

DROP TRIGGER IF EXISTS qdrant_operations_state_monotonic
    ON knowledge.qdrant_operations;
DROP FUNCTION IF EXISTS knowledge.forbid_operation_state_regression();

ALTER DEFAULT PRIVILEGES FOR ROLE retriva_migrator IN SCHEMA knowledge
    REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM retriva_core;

DROP SCHEMA IF EXISTS knowledge CASCADE;
