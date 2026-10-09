# Spec 036 (PROPOSED) — RLS-safe PostgreSQL monitoring aggregate interface

Status: **PROPOSED** (awaiting owner decision; implementation is NOT authorized
until this pack and ADR-041 are `ACCEPTED`).
Date: 2026-10-09.
Bases: Core `90f7369e930bd0155836c2106f853a0e2a562d71`; deployment
`aa37eb4f1bfcd2eface88d77eb7bf5f556ca19f3`.
Governing: `retriva-core/.agent/rules/retriva-constitution.md` (v1.2, sections
20, 29–34, 37–44), Spec 025 / ADR-030 (durable jobs, RLS, roles), Spec 034 /
ADR-039, Spec 035 / ADR-040, ADR-041 (companion, PROPOSED).

On acceptance this pack amends, by supersession of the affected clauses only:

- Spec 035 §6 (PostgreSQL read-only access: direct `SELECT ON jobs.jobs`);
- Spec 035 §7 B.3 (grant template);
- Spec 035 architecture.md §3 (read-only access model), and §4 for the
  `retriva_pg_nonterminal_jobs` source row;
- ADR-040 Decision 3, only in its PostgreSQL-access sentence.

Nothing else in Spec 034, Spec 035, ADR-039, or ADR-040 changes. The accepted
packs are not edited in place; this pack and ADR-041 carry the amendment
(Constitution §§42–43).

## 1. Problem

Spec 035 defined the canonical PostgreSQL monitoring gauge
`retriva_pg_nonterminal_jobs` as a direct read-only query executed by a
dedicated login role:

```sql
SELECT count(*) FROM jobs.jobs
 WHERE status NOT IN ('succeeded', 'failed', 'cancelled');
```

The rolled-back live deployment (2026-10-09) proved that this source cannot
work as specified in the hardened installation:

- `jobs.jobs` carries `ENABLE ROW LEVEL SECURITY` **and** `FORCE ROW LEVEL
  SECURITY` with the `tenant_isolation` policy (`src/retriva/jobs/sql/
  V001__jobs_foundation.up.sql`): a session without `app.current_tenant` (or
  the controlled `app.jobs_privileged_cleanup = 'granted'` transaction flag)
  matches no rows — including for the table owner;
- the monitoring login role therefore observed `0` of `38` rows live, and
  would keep reporting `0` non-terminal jobs even while durable non-terminal
  work exists;
- consequently the validated correlation rule `RedisQueueDisappearance`
  (`redis_key_exists{key="ingestion"} == 0 and on()
  (retriva_pg_nonterminal_jobs > 0)`) is structurally blind in production.

The metric is a **global operational aggregate** (durable non-terminal work
counts, all tenants), but monitoring must not gain row visibility, tenant
enumeration, `BYPASSRLS`, superuser, ownership, write, or DDL capability
(Constitution §§29–34, Spec 035 §5, task constraints).

## 2. Scope

In scope (after acceptance only):

- a narrowly scoped, version-controlled PostgreSQL aggregate interface
  (a single `SECURITY DEFINER` function returning an aggregate count);
- a dedicated non-login owner role for that function;
- migration-managed creation (forward and rollback migration) in the Core
  `core.jobs` stream, plus idempotent bootstrap provisioning of the owner role;
- the PostgreSQL collector switch to the interface, with a reduced role
  privilege set (`CONNECT`/`USAGE`/`EXECUTE` only) and no direct table
  `SELECT`;
- the corresponding deployment grant template, runbook, metric-contract, and
  deployment-prompt updates;
- deterministic security and fidelity tests and isolated end-to-end
  validation.

Out of scope:

- any change to application tables, policies, RLS posture, or application
  roles;
- any change to Redis, Qdrant, connectors, or application behavior;
- any live production change (deployment is governed by the separate live
  prompt; this pack does not close `OPEN_MONITORING_GAP`);
- tenant-scoped monitoring, dashboards, or additional PostgreSQL metrics
  beyond those already in the metric contract (any future aggregate must
  follow this same interface pattern through a governed change).

## 3. Owner decision requested

Acceptance of this pack and ADR-041 authorizes the bounded implementation of
the aggregate-only `SECURITY DEFINER` interface described in §5 under all the
constraints of §6, with no broadening of monitoring privileges beyond
`CONNECT` + `USAGE` + `EXECUTE`.

## 4. Root cause summary

1. Spec 035 assumed a plain-table read; the live installation enforces
   forced RLS on `jobs.jobs` (Spec 025 / ADR-030, Constitution §32).
2. A non-superuser, non-`BYPASSRLS` role cannot obtain cross-tenant rows
   through table access; the only in-schema cross-tenant visibility
   mechanisms are the tenant GUC or the controlled privileged-cleanup GUC.
3. No accepted aggregate interface existed; direct `SELECT` was the only
   authorized path, and it is functionally insufficient.

## 5. Proposed design (normative once accepted)

### 5.1 Interface

The interface lives in its own dedicated schema, created by the migration and
owned by the definer role. The function is created by that role (no ownership
transfer is possible or needed, and no `CREATE` privilege is granted on the
`jobs` schema):

```sql
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

Properties, all normative:

- returns one `bigint` aggregate only; no rows, identifiers, tenants,
  payloads, or free text;
- takes **no** parameters (no tenant, filter, SQL, identifier, table, or
  predicate inputs) — no injection surface and no enumeration surface;
- `SECURITY DEFINER`, created and owned by the dedicated non-login role
  `retriva_monitor_owner` (bootstrap-provisioned,
  `NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`);
  the owner also owns the dedicated `monitoring` schema; it holds `USAGE` on
  schema `jobs` and `SELECT` on `jobs.jobs` for the definer body and nothing
  else (no `CREATE` on any application schema);
- `SET search_path = pg_catalog` (immutable, shadowing-safe) and every
  relation/function reference schema-qualified;
- internally uses the application's own accepted cross-tenant visibility
  convention (`app.jobs_privileged_cleanup = 'granted'`, transaction-local):
  forced RLS remains enabled and enforced for every role and every other
  query path; the function itself returns only the count;
- `REVOKE ALL ... FROM PUBLIC`; `EXECUTE` granted only to the dedicated
  monitoring login role `retriva_monitor` created by the live deployment
  through the accepted secret interface;
- the interface exposes no SQL text, no error detail, and no identifiers on
  failure (callers receive a standard PostgreSQL permission/SQL error; the
  collector translates it into a health/error metric without echoing it).

### 5.2 Roles

- `retriva_monitor_owner` — dedicated non-login definer role; owns the
  dedicated `monitoring` schema and the interface function; holds only
  `USAGE` on schema `jobs` and `SELECT` on `jobs.jobs` (required for the
  definer body); membership is granted to `retriva_migrator` so migrations
  can `SET ROLE` into it to create its objects; nothing else.
- `retriva_monitor` — deployment-created login role for the collector:
  `CONNECT` on the database, `USAGE` on schema `monitoring`, `EXECUTE` on the
  function. **No** access to application schemas, no table `SELECT`, no
  write/DDL, no `BYPASSRLS`, no ownership, no role-management.
- `retriva_core`, `retriva_migrator`, application schemas, and RLS posture
  are unchanged.

### 5.3 Migration strategy (Core-owned)

- bootstrap (idempotent, admin connection, Core one-shot): provision
  `retriva_monitor_owner` if absent; refuse to manage an existing elevated
  role; grant membership to `retriva_migrator`.
- `core.jobs` stream, versioned migration `V002__monitoring_aggregate_
  interface`:
  - up: grant `USAGE`/`SELECT` on `jobs` to the owner; create the dedicated
    `monitoring` schema owned by the owner; `SET ROLE
    retriva_monitor_owner` and create the function (born owned by the
    definer role); `REVOKE ALL FROM PUBLIC`; `RESET ROLE`;
  - down: `DROP FUNCTION monitoring.nonterminal_job_count(); DROP SCHEMA
    monitoring;` and revoke the owner's `jobs` grants; no application table,
    role, policy, or data change;
  - registered with the existing ledger (`platform.schema_migrations`),
    checksums, transactional application, and downgrade guard; `SET ROLE`
    targets a bootstrap-provisioned, non-login, non-elevated role only;
  - the provider's required roles include `retriva_monitor_owner` (readiness
    check fails closed when the bootstrap has not run).
- no runtime DDL anywhere in application code.

### 5.4 Collector and deployment changes (deployment repository)

- collector query becomes `SELECT monitoring.nonterminal_job_count();`
  (single aggregate; statement timeout unchanged);
- grant template becomes `CREATE ROLE retriva_monitor LOGIN PASSWORD ...;
  GRANT CONNECT ...; GRANT USAGE ON SCHEMA monitoring ...; GRANT EXECUTE ON
  FUNCTION monitoring.nonterminal_job_count() ...;`
- metric contract: same metric name, labels, freshness/error metrics; source
  column updated to the interface; direct-SELECT grant removed;
- runbook: RLS verification and interface verification steps; rollback only
  revokes `EXECUTE` and drops the login role (Core migration down removes
  the function, its schema, and owner grants).

## 6. Security requirements (normative)

1. Forced RLS on `jobs.jobs`, `jobs.job_attempts`, and `jobs.job_events`
   remains enabled and enforced.
2. The monitoring login role has no direct table access; `SELECT` on
   protected tables is denied.
3. No `BYPASSRLS`, superuser, ownership, write, DDL, replication, or
   role-management privilege exists anywhere in this design; the definer
   owner is non-login and holds only the `jobs`-schema `USAGE`/`SELECT`
   grants plus ownership of its own dedicated `monitoring` schema.
4. `PUBLIC` cannot execute the interface; only `retriva_monitor` is granted
   `EXECUTE`.
5. The interface accepts no inputs and returns only a bounded numeric
   aggregate; no row, identifier, tenant, payload, error, or free-text data.
6. `search_path` is fixed inside the function and all references are
   schema-qualified; object-shadowing attempts cannot change the definition.
7. The collector fails closed (health/error metric, never silent absence) on
   permission or query failure and never logs SQL text or identifiers.
8. Statement timeout is enforced at the collector/session boundary
   (`PGOPTIONS=-c statement_timeout=5000`).
9. Rollback removes only the interface and its grants.

## 7. Acceptance criteria

A. Governance: ADR-041 `ACCEPTED`; registry entries for Spec 036 / ADR-041
consistent; no conflicting interface in another repository.

B. Migration: fresh-install and upgrade paths both reach the defined catalog
state; idempotent re-runs are no-ops; down migration removes only the
interface/grants; checksums and ledger rows are correct; no table ownership
or data change.

C. Fidelity: with multiple synthetic tenants and non-terminal jobs, the
interface returns the exact cross-tenant count; `retriva_pg_nonterminal_jobs`
reconciles with the authoritative count through the collector.

D. Security: all clauses of §6 proven by committed tests (direct access
denied, forced RLS active, `PUBLIC`/wrong roles denied, no write/DDL, safe
`search_path`, aggregate-only result shape, timeout, clean rollback);
application RLS and behavior tests unchanged.

E. Deployment: canonical-topology fixes from the defect correction retained;
the collector uses only the interface; runbooks and metric contract updated;
deterministic and isolated end-to-end validation pass; a superseding live
deployment prompt is produced; production remains unchanged until live
activation under that prompt.

## 8. Failure semantics

Permission/query failure: collector emits `retriva_pg_monitor_up=0`, an error
counter increment, and keeps the last successful value with a stale
timestamp; `MonitoringPostgresGaugeStale` fires. Missing interface (migration
not applied): the same fail-closed path (permission denied), never a false
`0`.

## 9. Compatibility and migration

- Additive: no application API, table, policy, or role change; no data
  rewrite; app behavior and RLS tests must remain green.
- Specs 025–035 semantics are preserved; only the monitoring PostgreSQL
  source and its grants change, as recorded above.
- Baseline failures must be identified explicitly; no test may be weakened
  to gain green status.

## 10. Production boundary

This pack authorizes no live change. Until `ACCEPTED` and implemented, the
live monitoring deployment remains rolled back and the Spec 034 monitoring
gap remains `OPEN_MONITORING_GAP`. The v2 live deployment prompt is
executable only after owner acceptance and after a bounded implementation
task commits the migration and the collector switch.
