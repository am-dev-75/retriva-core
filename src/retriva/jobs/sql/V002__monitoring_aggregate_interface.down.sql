-- Spec 036 / ADR-041 — rollback of the monitoring aggregate
-- interface.  Removes ONLY the interface and its grants: the
-- dedicated function, the dedicated schema (its sole object is the
-- function by design), and the owner's two application-schema
-- grants.  Application tables, owners, policies, FORCE RLS, indexes,
-- privileges, roles, and data are untouched.  The non-login
-- `retriva_monitor_owner` role itself is bootstrap-managed and is
-- left inert (it holds nothing after this rollback).

SET ROLE retriva_monitor_owner;
DROP FUNCTION IF EXISTS monitoring.nonterminal_job_count();
DROP SCHEMA IF EXISTS monitoring;
RESET ROLE;

REVOKE SELECT ON jobs.jobs FROM retriva_monitor_owner;
REVOKE USAGE ON SCHEMA jobs FROM retriva_monitor_owner;
