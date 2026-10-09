# ADR-041 (PROPOSED) — PostgreSQL monitoring aggregate interface under FORCE RLS

Status: **PROPOSED** (owner decision required; companion to Spec 036).
Date: 2026-10-09. Core baseline `90f7369e930bd0155836c2106f853a0e2a562d71`;
deployment baseline `aa37eb4f1bfcd2eface88d77eb7bf5f556ca19f3`.

## Context

Spec 035 / ADR-040 defined the canonical PostgreSQL monitoring gauge
`retriva_pg_nonterminal_jobs` as a direct read-only `SELECT count(*)` on
`jobs.jobs`, executed by a dedicated login role with `CONNECT` + `USAGE` +
`SELECT` (Spec 035 §6, §7 B.3, architecture §3).

The 2026-10-09 live deployment proved this source defective in the hardened
installation: `jobs.jobs` carries `FORCE ROW LEVEL SECURITY` with the
`tenant_isolation` policy (Spec 025 / ADR-030; Constitution §32), so any
session without `app.current_tenant` — or the controlled
`app.jobs_privileged_cleanup = 'granted'` transaction flag — matches zero
rows. The monitoring role observed `0` of `38` rows and would keep reporting
`0` durable non-terminal jobs, leaving the validated `RedisQueueDisappearance`
correlation structurally blind. No accepted aggregate interface existed.

The required metric is a global operational aggregate; monitoring must not
receive row visibility, tenant enumeration, `BYPASSRLS`, superuser,
ownership, write, DDL, or role-management capability (Constitution §§29–34).

## Decision

Once this ADR and Spec 036 are accepted, the PostgreSQL monitoring gauge is
sourced exclusively through a **migration-managed, aggregate-only,
`SECURITY DEFINER` interface**:

1. `monitoring.nonterminal_job_count() RETURNS bigint` — no parameters,
   fixed literal SQL, `SET search_path = pg_catalog`, every reference
   schema-qualified, returning one count of non-terminal durable jobs across
   all tenants; it sets the application's own transaction-local
   `app.jobs_privileged_cleanup = 'granted'` visibility flag internally.
2. Owned by a dedicated non-login role `retriva_monitor_owner`
   (`NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
   NOBYPASSRLS`), provisioned idempotently by the Core bootstrap, which also
   owns the dedicated `monitoring` schema created by the migration (so the
   function is born owned by its definer role and no `CREATE` privilege is
   ever granted on any application schema); in the application schema it
   holds only `USAGE` on `jobs` and `SELECT` on `jobs.jobs`.
3. `REVOKE ALL ... FROM PUBLIC`; `EXECUTE` granted only to the deployment
   login role `retriva_monitor`, which has `CONNECT` + `USAGE` on schema
   `monitoring` + `EXECUTE` and **no access to application schemas or
   tables**.
4. Created by a versioned `core.jobs` migration (V002) with a rollback that
   removes only the interface and its grants; `FORCE RLS` and the
   `tenant_isolation` policy remain unchanged and enforced for every other
   path.
5. Collector, grant template, metric contract, runbook, and live prompt
   updated accordingly; no live change until a separate authorized
   deployment.

This decision amends the PostgreSQL-access clauses of Spec 035 /
ADR-040 exactly as recorded in Spec 036 §"amendment effect"; those accepted
documents are not edited in place (Constitution §43).

## Alternatives considered

- **Grant the monitoring role `BYPASSRLS` (or superuser/ownership).**
  Rejected: broad privilege; violates the least-privilege and no-bypass
  constraints and Constitution §32's isolation expectations.
- **Widen the `tenant_isolation` policy** (for example a role-scoped
  `USING (true)` clause). Rejected: modifies an isolation control for one
  consumer; the policy surface is shared by the application and a single
  reviewer-visible mechanism is safer.
- **Own the function as `retriva_migrator` (existing object owner).**
  Rejected in favor of the dedicated non-login owner: the definer role holds
  the minimum application-schema grants plus its own dedicated schema and cannot be logged into, cannot be assumed, and
  cannot administer anything else.
- **Collector-side session flag** (`PGOPTIONS`/role-level
  `app.jobs_privileged_cleanup=granted`). Rejected: session-level privilege
  assertions on the monitoring identity are broader than a count-only
  interface and would let table `SELECT` become cross-tenant; a role-level
  setting is a standing privilege, not a scoped call.
- **Accept the limitation** (report the metric as best-effort/tenant-scoped).
  Rejected: the metric is a correlation input for a validated availability
  rule; a structurally zero signal is misleading.
- **Tenant enumeration in the collector** (loop tenants and sum). Rejected:
  requires tenant identifiers in monitoring and scales with tenant count;
  Constitution §§29–33.

## Consequences

- The Postgres metric becomes accurate across tenants with no row visibility
  and no elevated privilege anywhere.
- One additional non-login role and one schema function exist; both are
  bootstrap/migration-managed, reversible, and audited.
- Live activation requires the migration to run before the collector switch;
  the superseding live deployment prompt must enforce that ordering and
  verify forced RLS and denial of direct access.
- Until acceptance and implementation, the live monitoring deployment stays
  rolled back and the Spec 034 monitoring gap remains `OPEN_MONITORING_GAP`.
