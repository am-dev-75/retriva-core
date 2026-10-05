-- Retriva Core — Spec 025 (ADR-030): core.jobs V001 downgrade.
--
-- Drops ONLY Core-created objects (the three job tables, the
-- append-only trigger function, Core-side default privileges, and the
-- schema USAGE grant).  The `jobs` schema itself is NOT dropped: it
-- pre-exists as the CRM V001 reservation ("Durable job tracking
-- (later phases)"); removing the empty reservation is CRM V001's down
-- migration, refused by the CRM destructive-downgrade guard while any
-- Core-owned job object or job data exists (Spec 025 §3.13).
--
-- This downgrade is destructive (drops job history) and therefore
-- requires the framework's explicit --confirm-destructive override.

DROP TABLE IF EXISTS jobs.job_events CASCADE;
DROP TABLE IF EXISTS jobs.job_attempts CASCADE;
DROP TABLE IF EXISTS jobs.jobs CASCADE;

DROP FUNCTION IF EXISTS jobs.forbid_job_event_mutation();

ALTER DEFAULT PRIVILEGES FOR ROLE retriva_migrator IN SCHEMA jobs
    REVOKE SELECT, INSERT, UPDATE ON TABLES FROM retriva_core;
ALTER DEFAULT PRIVILEGES FOR ROLE retriva_migrator IN SCHEMA jobs
    REVOKE USAGE, SELECT ON SEQUENCES FROM retriva_core;

REVOKE USAGE ON SCHEMA jobs FROM retriva_core;
