# Spec 024: Centralized relational database — shared PostgreSQL platform for Core and Pro

- **Status:** ACCEPTED — 2026-10-04.  Accepted by explicit owner
  instruction: the task brief ordering this change ("Start Retriva's
  transition to a shared relational persistence platform by promoting
  PostgreSQL bootstrap, configuration, migration orchestration, and
  lifecycle management from the Pro CRM Assistant extension into
  Retriva Core", 2026-10-04) authorized implementation without a
  further acceptance round; the pack records that instruction as the
  acceptance event.  Revision 1, 2026-10-04.
- **Order of authority:** Retriva constitution v1.2 (canonical,
  `retriva-core/.agent/rules/retriva-constitution.md`) → ADR-029 →
  this spec → architecture.md → plan.md / tasks.md / acceptance.md →
  code.
- **Implements:** the shared relational persistence platform of
  ADR-029 (one instance, one database, module-owned schemas,
  provider-based migration streams, dedicated roles).
- **Related:** ADR-018 (CRM business-domain model — remains
  authoritative for CRM domain semantics), ADR-022/023/024/025 +
  Specs 019–023 (CRM persistence phases), Spec 018 (durable Core jobs,
  explicitly deferred), Constitution §20 (every store of record is
  declared), §17 (extensions extend, never fork), §45 (licensing
  boundary).

---

## 1. Objective

PostgreSQL becomes **mandatory for every Retriva deployment**,
including Core-only development deployments, as the shared
relational platform:

- one shared PostgreSQL instance;
- one database named `retriva`;
- multiple module-owned schemas;
- independently versioned migration streams registered through a
  Core-owned migration-provider contract;
- dedicated migration and runtime roles;
- Core schemas installed in every deployment;
- additional Pro schemas installed only when their Pro extensions are
  enabled.

Edition dependency rule (binding):

- Core may depend only on Core-owned database contracts.
- Pro may depend on stable, explicitly published Core contracts.
- Core must never depend on Pro schemas, Pro packages, or Pro
  migration providers.

This phase migrates **no other persistence**: GraphRAG SQLite state,
connector caches, Redis/Celery state, documents, attachments, model
artifacts, and Qdrant remain unchanged.

## 2. Scope

### 2.1 In scope

1. A Core-owned PostgreSQL platform package
   (`retriva.infrastructure.postgres`, Apache-2.0) providing generic
   capabilities only: configuration parsing, secure connection
   construction, readiness checks, role/bootstrap orchestration,
   migration-provider registration, migration discovery,
   deterministic ordering, migration-ledger access, checksum
   verification, transactional execution, advisory-lock concurrency
   protection, structured migration status/errors, and test support.
2. A Core-owned migration-provider contract (providers, streams,
   versions, names, checksums, dependencies, bodies, ownership
   metadata) with an extension-provided legacy-ledger adoption path.
3. Neutral Core-owned configuration (`RETRIVA_PG_*`) with a
   documented, deprecated `CRM_PG_*` compatibility path.
4. A Core-owned migration ledger (`platform.schema_migrations`)
   keyed by (provider, stream, version) preserving name, checksum,
   applied timestamp, and applying identity.
5. CRM Assistant adaptation: CRM schema definitions and migrations
   remain Pro-owned and register through the contract as the
   `pro.crm` stream (provider `retriva-crm-assistant`) without
   changing CRM table semantics, RLS, tenant isolation, grants,
   append-only rules, or migration history.
6. Mandatory PostgreSQL lifecycle in the development Compose
   deployment: PostgreSQL healthy → bootstrap → Core migrations →
   Core services; Core migrations → extension migrations →
   extension services.
7. Messaging preparation: the dedicated Messaging PostgreSQL service
   and the `retriva_messaging` database target are removed;
   Messaging targets the shared `retriva` database with a Pro-owned
   `messaging` schema and a dedicated runtime identity.  The provider
   framework supports registering `pro.messaging`.  Full Messaging
   provider-contract wiring and validation are deferred.
8. Automated tests: migration-framework tests, CRM compatibility
   tests, Compose validation tests, security tests.
9. Updated development documentation.

### 2.2 Out of scope (binding)

Durable Core job tables or repositories; Celery/Redis state
migration; GraphRAG SQLite migration; MediaWiki and Email Agent
connector-state migration; document or attachment migration;
model-file storage changes; Qdrant replacement; CRM schema redesign;
Messaging business-schema redesign; Messaging delivery
implementation; CRM/campaign-to-Messaging integration; transactional
outbox; production HA, backup/DR, and secrets-management redesign;
unrelated refactoring.

## 3. Requirements

### 3.1 Shared database

Core and Pro use the same PostgreSQL service and the same `retriva`
database.  Module isolation is implemented with separately owned
schemas and PostgreSQL privileges, not separate databases.

### 3.2 Mandatory PostgreSQL for Core

A Core-only development deployment starts PostgreSQL, waits for
readiness, bootstraps required roles and common infrastructure,
applies Core migrations, and starts Core services only after
successful Core migration.  PostgreSQL is never an optional,
CRM-only, or Pro-only dependency.

### 3.3 Additive Pro extensions

Core installation creates only Core-owned database objects.  A Pro
installation first establishes Core database state, then adds schemas
and objects supplied by its enabled migration providers.  Core
migrations must not import proprietary packages, create CRM- or
Messaging-specific tables, add Pro-specific columns to Core tables,
or require any Pro schema to exist.

### 3.4 Schema ownership

Every schema has exactly one owning module or bounded context.
Shared PostgreSQL infrastructure does not imply shared table
ownership.  Modules must not write directly to another module's
tables unless an explicit documented contract makes that access part
of the architecture.

### 3.5 Existing CRM schemas

`business`, `campaigns`, `imports`, `qualification`, `research` are
CRM-owned (verified: created and granted by CRM migration V001+,
migrator-owned, RLS-enforced).  `audit` is mixed: `audit.events` is
the CRM audit domain; `audit.schema_migrations` is the legacy CRM
migration ledger.  The generic migration concern moves to the
Core-owned `platform` ledger; `audit.events` semantics are unchanged
and the legacy ledger is preserved read-only after adoption.
`jobs` is a CRM-migration-created placeholder (no tables, no runtime
use); it is NOT transferred to Core in this phase and the future
Core-owned durable-jobs transition is documented as follow-up.

### 3.6 Roles

Administrative bootstrap identity, migration/owner identity
(`retriva_migrator`), Core runtime identity (`retriva_core`), CRM
runtime identity (`retriva_application` and peers), importer,
read-only, and pgAdmin operator identities remain separated.
Application runtime roles never own schemas and cannot execute
schema DDL.  Core runtime cannot write CRM-owned tables.  Role
creation and grants are idempotent.  The container bootstrap
superuser (`retriva` in the current development deployment) is never
the runtime identity of any service.

### 3.7 Tenant isolation

The transaction-scoped `app.current_tenant` setting and forced RLS
are preserved unchanged for CRM tables.  No new tenant-scoped Core
tables are introduced in this phase; the migration ledger is
deployment-global infrastructure and is not tenant-scoped.

### 3.8 Migration identity, ordering, and integrity

- Providers declare identity (provider id), stream identity,
  stream dependencies, required roles, optional legacy ledger, and
  migrations (version, name, up/down SQL bodies, sha256 checksum).
- Discovery is deterministic; stream ordering is a deterministic
  topological order with stable tie-breaks; versions order
  monotonically within a stream.
- Duplicate provider/stream/version identities fail clearly; an
  extension cannot overwrite or impersonate a `core.*` stream
  (namespace guard); missing stream dependencies fail clearly.
- Checksum drift on an applied migration fails safely; migration
  execution is one transaction per migration (DDL + ledger insert)
  where PostgreSQL permits; a session-level advisory lock serializes
  concurrent runners.
- Re-running all providers on an up-to-date database is a no-op.
- Applied legacy ledger records are adopted with preserved identity
  (version, name, checksum, applied timestamp/identity) after
  explicit validation; legacy rows are never edited in place or
  deleted; the mapping is documented.

### 3.9 Configuration

Canonical Core configuration is `RETRIVA_PG_HOST`, `RETRIVA_PG_PORT`,
`RETRIVA_PG_DATABASE`, `RETRIVA_PG_SSLMODE`, `RETRIVA_PG_ADMIN_USER`,
`RETRIVA_PG_ADMIN_PASSWORD(_FILE)`,
`RETRIVA_PG_MIGRATOR_PASSWORD(_FILE)` (role name configurable via
`RETRIVA_PG_MIGRATOR_USER`), `RETRIVA_PG_CORE_USER`,
`RETRIVA_PG_CORE_PASSWORD(_FILE)`.  Extension-specific runtime
credentials remain extension-specific (`CRM_PG_APPLICATION_*` and
peers; Messaging runtime identity).  Generic Core code never treats
`CRM_PG_*` as canonical.  The deprecated `CRM_PG_*` platform names
(host/port/database/sslmode/admin/migrator/pool bounds) remain
accepted as a documented compatibility path.  Passwords never appear
in logs, errors, or health output; secret-file indirection follows
the existing convention; missing required settings fail clearly.

### 3.10 Compose lifecycle

The sole development Compose deployment makes PostgreSQL mandatory:
`retriva-postgres`, `retriva-pg-bootstrap`, `retriva-pg-migrate` run
without a profile; Core services depend on Core migration success;
Pro extension migration one-shots (CRM) run in the Pro profile after
Core migrations; no Core service depends on optional Pro migrations;
one-shots are idempotent; no arbitrary sleeps; no circular
dependencies; repeated `docker compose up` neither damages valid
database objects nor recreates the volume; health checks never
expose credentials; a Core-only path exists without CRM installed or
imported; the established Pro development workflow is preserved.

## 4. Acceptance

See acceptance.md.  Required gates: framework tests, CRM
compatibility tests, Compose configuration validation, clean-database
bootstrap+migration+rerun, existing-database upgrade validation
against a copy or safe representation of the live development
database, Core-only and Pro startup validation, security checks, and
documentation.

## 5. Deferred follow-ups (recorded, not implemented)

Durable Core jobs (`jobs` schema transition); knowledge/ingestion
metadata persistence; connector state; GraphRAG migration; full
Messaging provider-contract wiring and functional validation;
production readiness.  See plan.md §9.
