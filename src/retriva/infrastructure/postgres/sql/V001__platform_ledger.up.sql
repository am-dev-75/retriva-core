-- Retriva shared PostgreSQL platform (Spec 024; ADR-029).
-- Migration V001: Core platform ledger foundation.
--
-- The `platform` schema is owned by the `core.platform` stream and
-- houses the Core migration ledger (deployment-global
-- infrastructure; NOT tenant-scoped by design).  The ledger table
-- itself is created idempotently by the runner before any
-- migration; this migration records the schema's ownership and the
-- Core runtime role's read posture in versioned, reproducible form.

CREATE SCHEMA IF NOT EXISTS platform AUTHORIZATION retriva_migrator;

-- Core runtime identity: may read migration status, nothing else.
-- It holds no grants on any extension-owned schema.
GRANT USAGE ON SCHEMA platform TO retriva_core;
GRANT SELECT ON platform.schema_migrations TO retriva_core;

ALTER DEFAULT PRIVILEGES FOR ROLE retriva_migrator IN SCHEMA platform
    GRANT SELECT ON TABLES TO retriva_core;

COMMENT ON SCHEMA platform IS
    'Core-owned deployment-global infrastructure (core.platform stream): shared migration ledger';
