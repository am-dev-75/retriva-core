# Spec 036 — Architecture (PROPOSED)

Companion to `spec.md` and ADR-041. Status: PROPOSED.

## 1. Where the interface sits

```text
  +---------------------------+         scrape /metrics.txt
  | retriva-pg-monitor-       | <----------------------------- prometheus
  | exporter (postgres:16.15- |                                (v3.15.0)
  | alpine + psql + busybox)  |
  +-------------+-------------+
                |  psql, session: PGOPTIONS=-c statement_timeout=5000
                |  identity: retriva_monitor (CONNECT, USAGE, EXECUTE)
                v
  +---------------------------+       EXECUTE monitoring.nonterminal_job_count()
  | retriva-postgres 16.15    |-------+-------------------------------------+
  +---------------------------+       |  SECURITY DEFINER (owner:           |
                                      v  retriva_monitor_owner, NOLOGIN)    |
                        +--------------------------------+                |
                        | jobs.monitoring_nonterminal_    |                |
                        | job_count() -> bigint          |                |
                        |  set_config(priv_cleanup,      |                |
                        |             'granted', true)   |                |
                        |  SELECT count(*) FROM          |                |
                        |    jobs.jobs WHERE status ...  |                |
                        +---------------+----------------+                |
                                        v                                 |
                        +--------------------------------+   FORCE RLS     |
                        | jobs.jobs (tenant_isolation     |   stays on     |
                        | policy, forced)                 |<---------------+
                        +--------------------------------+
```

The monitoring login role never reads `jobs.jobs` directly; the only path to
the aggregate is the function. The function is the only object in the design
with definer rights, and it can return nothing but one number.

## 2. Catalog objects and exact DDL

### 2.1 Bootstrap provisioning (Core one-shot, admin connection, idempotent)

```sql
-- provisioned idempotently; refuses to manage an existing elevated role
CREATE ROLE retriva_monitor_owner
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT retriva_monitor_owner TO retriva_migrator;   -- SET ROLE for its objects
```

No password exists for this role; it can never log in and cannot be assumed
by any application identity. Membership in `retriva_migrator` exists only so
the migration can `SET ROLE` into the owner to create the owner's own objects
(PostgreSQL requires objects to be created in a schema the creating role can
use).

### 2.2 Versioned migration `core.jobs` V002 (up)

```sql
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
```

The function is born owned by its definer role; no ownership transfer is
performed (PostgreSQL requires the new owner to hold `CREATE` on the
containing schema, which would mean granting the owner `CREATE` on the
application `jobs` schema — deliberately avoided).

### 2.3 Versioned migration V002 (down)

```sql
DROP FUNCTION IF EXISTS monitoring.nonterminal_job_count();
SET ROLE retriva_monitor_owner;
DROP SCHEMA IF EXISTS monitoring;
RESET ROLE;
REVOKE SELECT ON jobs.jobs FROM retriva_monitor_owner;
REVOKE USAGE ON SCHEMA jobs FROM retriva_monitor_owner;
```

Dropping the function removes its privilege rows; any `EXECUTE` grant to the
deployment login role disappears with the object. The dedicated schema is
removed only when empty (the function is its sole object by design).
Application tables, roles, policies, and data are untouched in both
directions.

### 2.4 Deployment grant template (live, after accepted implementation)

```sql
CREATE ROLE retriva_monitor LOGIN PASSWORD :'monitor_password';
GRANT CONNECT ON DATABASE retriva TO retriva_monitor;
GRANT USAGE ON SCHEMA monitoring TO retriva_monitor;
GRANT EXECUTE ON FUNCTION monitoring.nonterminal_job_count()
    TO retriva_monitor;
```

No access to application schemas and no `SELECT` on any table. Rollback:
`REVOKE EXECUTE ...`, then `DROP ROLE retriva_monitor`.

### 2.5 Collector query

```sql
SELECT monitoring.nonterminal_job_count();
```

Executed by `psql -tA` with the session statement timeout; the collector
parses a single integer, fails closed on any error, and emits the accepted
metric names unchanged.

## 3. Security analysis

- **RLS preservation.** `FORCE ROW LEVEL SECURITY` and the
  `tenant_isolation` policy are not modified. Every direct query by every
  role (including the table owner) is still filtered exactly as before. The
  function uses the application's own documented privileged-cleanup
  transaction flag internally; that flag was already the accepted
  cross-tenant visibility mechanism for controlled operations
  (`V001__jobs_foundation.up.sql`), and it remains transaction-local.
- **No privilege escalation surface.** A caller gains only `EXECUTE`. The
  definer owner is non-login, owns only its dedicated schema, and holds two application-schema grants; no role in the
  design has `BYPASSRLS`, superuser, write, DDL, or role-management rights.
- **No enumeration surface.** The function has no parameters; a caller
  cannot ask for a tenant, a subset, or a predicate. The only result is the
  global count.
- **No leakage surface.** The result is one bigint; errors are standard
  PostgreSQL errors; the collector never logs SQL text, credentials, or
  identifiers and emits only bounded numeric metrics.
- **Shadowing resistance.** `SET search_path = pg_catalog` is fixed on the
  function; `public`/`pg_temp` cannot shadow `jobs.jobs` or the used
  `pg_catalog` functions; all references are schema-qualified.
- **Injection resistance.** The body is fixed literal SQL with no dynamic
  SQL, no format strings, and no identifiers from input.
- **Fail-closed.** Missing grants, missing function, or a database outage
  produce permission/connection errors → `retriva_pg_monitor_up=0` + error
  counter + stale timestamp → `MonitoringPostgresGaugeStale`; never a wrong
  `0` presented as healthy data.

## 4. Migration mechanics

- Stream `core.jobs` (provider `retriva.jobs.migrations.jobs_provider`,
  dependency `core.platform`); the provider's required roles gain
  `retriva_monitor_owner` so readiness fails when bootstrap has not run.
- Versioned file pair with sha256 checksums, transactional apply, ledger rows
  in `platform.schema_migrations`, deterministic order, idempotent re-runs.
- Fresh install: bootstrap → V001 (tables/RLS/grants) → V002 (interface).
- Upgrade: existing installation applies V002 only.
- Downgrade guard remains in force; down removes only the interface.

## 5. Validation architecture

- Committed deterministic tests (Core): migration contract tests for V002
  (checksums, up/down shapes), and interface security tests on a disposable
  PostgreSQL 16 instance applying the real migrations:
  - cross-tenant count equals ground truth (multiple synthetic tenants);
  - forced RLS still enabled/active (catalog + behavior);
  - direct monitoring-role `SELECT` denied;
  - `PUBLIC` and wrong roles cannot `EXECUTE`;
  - monitoring cannot write/DDL/`SET ROLE`/bypass RLS;
  - function `proconfig` fixes `search_path`; references qualified;
  - result shape is a single integer;
  - rollback removes the function and grants;
  - existing RLS/application tests unchanged.
- Committed deterministic tests (deployment): canonical topology parity and
  the collector/interface alignment.
- Isolated end-to-end validation (outside repositories): canonical Compose
  topology with synthetic Redis (Spec 034 posture) and synthetic PostgreSQL
  carrying the real jobs schema + forced RLS; the canonical collector proves
  the failure first, then the prototype interface (disposable, outside
  repositories) proves the corrected metric and the A3 correlation firing
  and resolution end to end.
