-- Spec 036 / ADR-041 — RLS-safe PostgreSQL monitoring aggregate
-- interface (ACCEPTED 2026-10-09).
--
-- The monitoring gauge `retriva_pg_nonterminal_jobs` is sourced ONLY
-- through `monitoring.nonterminal_job_count()`: an aggregate-only,
-- no-argument SECURITY DEFINER function owned by the dedicated
-- non-login role `retriva_monitor_owner` (provisioned by the Core
-- bootstrap one-shot, which also grants the migrator membership so
-- this migration can SET ROLE into it).
--
-- FORCE ROW LEVEL SECURITY on `jobs.jobs` remains enabled and is
-- never modified.  The function uses the application's own
-- transaction-local `app.jobs_privileged_cleanup = 'granted'`
-- visibility convention internally and returns one count; it exposes
-- no rows, tenants, identifiers, payloads, or free text, accepts no
-- arguments, fixes `search_path = pg_catalog`, and schema-qualifies
-- every reference.
--
-- Created by : retriva_migrator (SET ROLE retriva_monitor_owner for
--              the owner-created objects).
-- Upstream   : src/retriva/jobs/migrations.py (core.jobs stream).

GRANT USAGE ON SCHEMA jobs TO retriva_monitor_owner;
GRANT SELECT ON jobs.jobs TO retriva_monitor_owner;

CREATE SCHEMA IF NOT EXISTS monitoring AUTHORIZATION retriva_monitor_owner;

SET ROLE retriva_monitor_owner;
CREATE FUNCTION monitoring.nonterminal_job_count()
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
VOLATILE
SET search_path = pg_catalog
AS $$
DECLARE
    counted bigint;
BEGIN
    PERFORM pg_catalog.set_config(
        'app.jobs_privileged_cleanup', 'granted', true);
    SELECT pg_catalog.count(*) INTO counted
      FROM jobs.jobs
     WHERE jobs.jobs.status NOT IN ('succeeded', 'failed', 'cancelled');
    RETURN counted;
END;
$$;
REVOKE ALL ON FUNCTION monitoring.nonterminal_job_count() FROM PUBLIC;
RESET ROLE;
