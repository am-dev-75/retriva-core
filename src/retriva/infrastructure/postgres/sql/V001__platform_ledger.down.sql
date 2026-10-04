-- Retriva shared PostgreSQL platform (Spec 024; ADR-029).
-- Down migration V001: revoke the Core platform foundation.
--
-- Destructive and gated behind --confirm-destructive.  The CASCADE
-- is scoped strictly to the Core-owned `platform` schema (never to
-- application schemas): reverting the platform stream removes the
-- shared ledger, which is only meaningful when the platform itself
-- is being dismantled.  The legacy CRM ledger
-- (audit.schema_migrations) and all CRM objects are untouched.

REVOKE SELECT ON platform.schema_migrations FROM retriva_core;
REVOKE USAGE ON SCHEMA platform FROM retriva_core;

ALTER DEFAULT PRIVILEGES FOR ROLE retriva_migrator IN SCHEMA platform
    REVOKE SELECT ON TABLES FROM retriva_core;

DROP SCHEMA IF EXISTS platform CASCADE;
