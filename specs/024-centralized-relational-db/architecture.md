# Spec 024 architecture — shared PostgreSQL platform

Status: ACCEPTED (with the pack, 2026-10-04)

Related: ADR-029 (decision), spec.md (requirements).  This document
records the implemented architecture.

## 1. Components

### 1.1 Core platform package — `retriva.infrastructure.postgres`

Apache-2.0, zero CRM/Messaging domain knowledge.  Modules:

- `errors.py` — `PostgresPlatformError`,
  `PostgresNotConfiguredError`, `PostgresConnectionError`,
  `MigrationError`.
- `config.py` — `PostgresPlatformSettings` (env prefix
  `RETRIVA_PG_`): endpoint (host/port/database/sslmode), admin
  identity, migrator identity, Core runtime identity, pool bounds
  and timeouts.  `connection_kwargs(role)`, `resolved_password(role)`,
  `has_password(role)`, `*_FILE` secret indirection (4 KiB cap),
  fail-closed missing-credential errors.  Credentials are `SecretStr`
  and never rendered.
- `bootstrap.py` — generic, idempotent role provisioning:
  `provision_roles(admin_kwargs, specs)` creates or rotates
  non-elevated LOGIN roles (`NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOREPLICATION`), refuses to manage pre-existing elevated roles, and
  (via `restrict_database_create`) revokes `CREATE` on the database
  from `PUBLIC`, granting it to the migrator only.  Used by the Core
  CLI (migrator + Core runtime roles) and reused by the CRM bootstrap
  for its four extension roles.
- `migrations.py` — the provider contract and runner:
  - `Migration` (provider, stream, version, name, up_sql, down_sql,
    checksum = sha256(up + NUL + down), optional description),
    `LegacyLedger` descriptor, `MigrationProvider` protocol
    (provider_id, stream_id, discover(), stream_dependencies(),
    required_roles(), legacy_ledger(), optional downgrade_guard()),
    `SqlMigrationProvider` (file-based provider used by Core and
    CRM), `ProviderRegistry` (registration + validation + ordering),
    `upgrade()/downgrade()/status()/verify()` driven by a settings
    object.
  - Ledger: `platform` schema, `platform.schema_migrations` table,
    PK (provider, stream, version), columns name, checksum,
    applied_at, applied_by.  Created idempotently by `ensure_ledger`
    (migrator-owned) outside the versioned streams.
  - Ordering: streams are ordered by deterministic topological sort
    of stream dependencies (Kahn's algorithm with sorted ready set;
    ties broken by provider id then stream id; cycles fail).
  - Concurrency: session-level advisory lock
    `pg_advisory_lock(hashtext('retriva_pg_migrations'))` held across
    the run; lock acquisition before any ledger read for applying.
  - Transactions: each migration's DDL plus its ledger insert runs in
    one transaction; failure rolls back that migration only, then
    aborts the run with a structured error naming provider, stream,
    version and cause class.
  - Legacy adoption: a provider declaring `legacy_ledger` triggers
    adoption of legacy rows (validated against shipped migrations:
    unknown versions and checksum drift fail; partial-adoption states
    fail) into the new ledger preserving version, name, checksum,
    applied_at, applied_by.  Legacy rows are read-only thereafter and
    never modified or deleted.
  - Impersonation guard: `core.*` streams are reserved to provider
    `retriva-core`; registration of a `core.*` stream by any other
    provider fails clearly.  Duplicate (provider, stream) and
    duplicate (provider, stream, version) registrations fail clearly.
- `migrate_cli.py` / `__main__` — deployment-time CLI
  (`python -m retriva.infrastructure.postgres.migrate`): `bootstrap`,
  `upgrade`, `status`, `downgrade --stream --to
  --confirm-destructive`, `verify`, `readiness`.  Providers load from
  `RETRIVA_PG_MIGRATION_PROVIDERS` (comma-separated dotted module
  paths exposing `MIGRATION_PROVIDERS`); the Core `core.platform`
  provider is always registered implicitly.  No proprietary module
  names are hard-coded.
- `sql/` — the Core `core.platform` stream: `V001__platform_ledger`
  (platform schema ownership + Core-runtime usage/select grants on
  the ledger).

### 1.2 CRM Assistant adaptation (Pro, unchanged semantics)

- `retriva_crm_assistant/postgres/migrations.py` — provider module:
  `CrmMigrationProvider` (provider `retriva-crm-assistant`, stream
  `pro.crm`, file discovery over the existing `postgres/sql/` files,
  legacy checksum algorithm preserved, required roles = migrator +
  the four CRM roles, stream dependency `core.platform`, legacy
  ledger `audit.schema_migrations`, downgrade guard preserving the
  ACP-history refusal).
- `migrate.py` — CLI kept (deployment compatibility); `upgrade`,
  `status`, `downgrade`, `verify` delegate to the Core runner with
  the CRM provider registered; `bootstrap-roles` provisions the four
  CRM extension roles through the Core generic `provision_roles`.
- `config.py` — endpoint/admin/migrator/pool fields accept the
  canonical `RETRIVA_PG_*` names with documented `CRM_PG_*`
  compatibility aliases; CRM extension role credentials stay
  `CRM_PG_*`; `CRM_PG_ENABLED` remains the CRM runtime activation
  switch.
- CRM tables, columns, constraints, indexes, functions, triggers,
  RLS policies, tenant context (`app.current_tenant`,
  transaction-local, fail-closed), grants, append-only rules, and
  migration file contents are unchanged.

### 1.3 Messaging preparation (Pro, draft)

- Dedicated `retriva-messaging-db` service, its
  `retriva_messaging` database target, the `messaging_db_data`
  volume, and `MESSAGING_DB_*` variables are removed (verified never
  started; no data).
- `retriva_messaging` models target schema `messaging`
  (`MetaData(schema=...)`); Alembic `env.py` pins
  `version_table_schema="messaging"`; the default
  `RETRIVA_MESSAGING_DATABASE_URL` points at the shared `retriva`
  database; the Compose service uses the shared
  `retriva-postgres` service with the dedicated
  `retriva_messaging` runtime role provisioned by a Pro-owned
  one-shot bootstrap, and Alembic runs as the migrator through a
  one-shot migration service before the Messaging API starts.
- The provider framework supports registering `pro.messaging`
  (tested with a contract-level provider); full provider-contract
  wiring of Messaging's Alembic stream and functional validation are
  deferred.

### 1.4 Deployment topology

```
retriva-postgres (no profile, retriva_pg_data volume)
  → retriva-pg-bootstrap   (core roles; admin conn; idempotent;
      Core-only `base` build stage with its own repo-relative
      context RETRIVA_PG_TOOLS_CONTEXT — the Dockerfile `base`
      stage copies repo-relative paths while the `pro` stage needs
      the workspace-parent context)
  → retriva-pg-migrate     (core.platform; migrator conn only;
      same Core-only image)
      → retriva-ingestion / retriva-core / retriva-worker
        (service_completed_successfully)
retriva-pg-crm-bootstrap   (pro: four CRM roles)
  depends: retriva-pg-bootstrap completed
retriva-pg-crm-migrate     (pro: pro.crm stream incl. adoption)
  depends: retriva-pg-migrate completed, crm-bootstrap completed
retriva-pg-messaging-bootstrap / retriva-pg-messaging-migrate
  (messaging profile; after core migrate)
retriva-pgadmin            (db/pgadmin profiles, operator tool)
```

## 2. Security posture

- Bootstrap (admin/superuser) is deployment-time only; the container
  superuser is never a service runtime identity.
- Migrator owns schemas and schema changes; application roles own
  nothing, cannot DDL.
- Core runtime role has only USAGE on `platform` + SELECT on the
  ledger; it holds no CRM grants (cannot read or write CRM tables).
- CRM runtime keeps exactly its previous grants; importer staging
  restrictions and RLS semantics unchanged.
- No secrets in logs, errors, readiness output, health checks, or
  committed files; `SecretStr` + `*_FILE` indirection.
- Ledger and platform infrastructure are not tenant-scoped
  (deployment-global); CRM tenant isolation unchanged.

## 3. Compatibility

- `CRM_PG_HOST/PORT/DATABASE/SSLMODE/ADMIN_USER/ADMIN_PASSWORD(_FILE)/
  MIGRATOR_PASSWORD(_FILE)/POOL_*/timeouts` remain accepted (compat
  aliases of the `RETRIVA_PG_*` canonicals) — deprecated, documented
  in the deployment `.env.example` and docs.
- Legacy ledger `audit.schema_migrations` is preserved read-only
  after adoption; CRM `verify` remains authoritative for CRM domain
  invariants and reads stream status from the new ledger.
- Existing development volume and CRM data survive in place; the
  upgrade path is validated against a restored copy before the live
  database is migrated.
