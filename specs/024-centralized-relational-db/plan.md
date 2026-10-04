# Spec 024 plan

Status: ACCEPTED (with the pack, 2026-10-04)

## 1. Current-state findings (verified 2026-10-04)

1. **Generic, must move to Core:** CRM `postgres/config.py`
   connection/password/pool plumbing; `bootstrap.py` role
   provisioning; `migrate.py` discovery/checksum/ledger/lock/
   transaction/status machinery; `errors.py`; `readiness.py` shape.
2. **CRM-specific, stays in Pro:** all SQL migrations V001–V008; the
   four CRM runtime roles and their grants; `audit.events` and its
   triggers; `tenant.py`; repositories; qualification/campaign/
   imports/erp modules; `CRM_PG_ENABLED`; the CRM readiness route.
3. **Extension registration today:** Core
   `CapabilityRegistry.load_extensions()` imports each
   `RETRIVA_EXTENSIONS` module and calls `register(registry)`;
   routers register as `*_api_router` capabilities and are mounted
   by the host FastAPI apps.
4. **Provider registration without Core→Pro imports:** the migration
   runner loads provider modules from
   `RETRIVA_PG_MIGRATION_PROVIDERS` (same mechanism shape as
   `RETRIVA_EXTENSIONS`), each exposing `MIGRATION_PROVIDERS`; the
   Core provider is implicit.  Core contains no proprietary module
   name.
5. **Ledger today:** `audit.schema_migrations`
   (version PK, name, checksum, applied_at, applied_by), created by
   `ensure_ledger` as migrator; mixed with the CRM audit domain
   (`audit.events`).
6. **Ledger contents (live dev DB, project cust_0007):** 8 applied
   rows, V001–V008, applied_by `retriva_migrator`,
   2026-09-23…2026-09-28.  `business.organizations` holds 10 dev
   rows.  The volume `retriva_pg_data` must survive.
7. **Ordering/checksums today:** versions are integers from
   `V<NNN>__<name>.up.sql` paired with `.down.sql`; checksum =
   sha256(up_sql + NUL + down_sql); discovery validates pairing and
   uniqueness; application order is ascending version.
8. **Transactional execution:** yes — autocommit off, one
   transaction per migration (DDL + ledger insert), commit per
   migration, rollback on failure.
9. **Concurrency:** session advisory lock
   `pg_advisory_lock(hashtext('retriva_pg_migrations'))` around the
   applying run.
10. **Data/compat preservation:** no CRM SQL file is edited; ledger
    rows are preserved via explicit adoption; role grants unchanged;
    CRM runtime roles keep their identities and passwords.
11. **Messaging DB service usage:** `retriva-messaging-db` never
    started; no container, no volume (`messaging_db_data` never
    created); `RETRIVA_MESSAGING_ENABLED=off` in the live `.env`.
    Removal is low-risk.
12. **Files expected to change:** listed in tasks.md.

Discrepancies vs. the task brief (repository facts preferred): the
live deployment's admin bootstrap identity is the container superuser
`retriva` (compose `RETRIVA_PG_ADMIN_USER`), not
`retriva_admin` (the .env overrides the default); the dev `.env`
sets `CRM_PG_ENABLED=true` (the store is active, not disabled);
`retriva-core/AGENTS.md` carries an unrelated mission block
(Spec 014) whose order-of-authority chain does not cover this change
(reported in the task report; the canonical constitution chain
governs).

## 2. Implementation phases

### Phase A — Governance
Registry backfill + allocation (done first per Constitution §43),
Spec 024 pack, ADR-029.

### Phase B — Core platform package
`retriva/infrastructure/postgres/` per architecture.md §1.1;
`psycopg2-binary` added to core requirements (lazy import; the
existing psycopg2-based approach, no new framework); Core
`core.platform` SQL files; CLI module.

### Phase C — Core tests
Unit (contract/registry/ordering/impersonation/checksum/config
hygiene, no DB) + integration (scratch cluster via `initdb` or
`RETRIVA_PG_TEST_ADMIN_URL`: bootstrap → upgrade → rerun no-op →
checksum drift → rollback on bad SQL → concurrency → adoption →
core-only defaults).

### Phase D — CRM adaptation
Provider module; `migrate.py` delegation; settings aliases; conftest
stack update; PG tests updated (config, migrations, roles security)
+ new provider/adoption tests; `verify` reads the new ledger.

### Phase E — Messaging preparation
Schema/database/role retarget; alembic env schema; config/tests;
Compose retarget (Phase F covers the deployment file).

### Phase F — Deployment
Compose topology (mandatory PG, profiles, dependency graph,
messaging-db removal); `manage.sh` commands and credential checks;
`.env.example` (new names, deprecations, messaging block); docs;
deployment tests rewritten.

### Phase G — Validation
All suites; `docker compose config` for base and profiles;
scratch-DB lifecycle; live-DB backup + restored-copy upgrade
validation; then live adoption through the Compose lifecycle;
existing-database upgrade validation; report.

## 3. Data-safety procedure

1. `docker exec retriva-postgres pg_dump -U retriva -d retriva >
   backup.sql` (documented in docs) before any runner touches the
   live DB.
2. Create scratch DB `retriva_cedb_test` from the dump on the same
   instance; run bootstrap (idempotent) + core upgrade + CRM upgrade
   (adoption) + rerun (no-op) + verify there.
3. Only after the scratch validation is green, run the same
   lifecycle against the live `retriva` database via the Compose
   one-shots.
4. Never drop/recreate the database, drop CRM schemas, truncate,
   touch the volume, or use CASCADE against application schemas.

## 4. Rollback

Code: revert branch `centralized_relational_db`.  Database: the
adoption only adds rows to `platform.schema_migrations` and creates
the `platform` schema; the legacy ledger and all CRM objects are
untouched; a pre-change `pg_dump` restore path is documented.
Compose: the previous compose file remains in git history; volume
data is never modified by either version of the lifecycle.

## 5. Test matrix (maps to acceptance.md)

- B/C: contract unit tests; integration lifecycle tests.
- D: CRM provider registration; clean-DB CRM schema; existing-style
  DB upgrade; idempotent rerun; invariants (RLS, triggers, grants).
- E: messaging schema/config tests.
- F: compose resolution tests (base, pro, messaging, db profiles);
  core-only exclusion of Pro; env contract; manage.sh contract.
- G: executed commands recorded in acceptance.md §V.

## 6. Scope guards

No durable job tables; no module persistence migration beyond the
migration-ledger concern; no CRM/Messaging redesign; no outbox; no
production hardening.

## 7. Constitution checkpoints

§42 pack before code; §43 registry before PROPOSED; §44 scope
binding; §45 no proprietary leak into Core (tests assert
core-platform code never imports `retriva_crm_assistant` /
`retriva_messaging`); §20 store-of-record declared (platform ledger
is Core-owned infrastructure); §34 secrets referenced only; §33/§41
no content in logs; §8 deterministic semantics (discovery/ordering);
§18 optional capabilities safe by default (CRM store activation
unchanged; messaging remains disabled by default).

## 8. Owner-authorization note

The task brief (2026-10-04) explicitly authorizes implementation
without a further acceptance round and fixes the architectural
decisions this pack implements.  That instruction is recorded as the
acceptance event in spec.md §Status.

## 9. Deferred follow-up plan

1. **Durable Core jobs** (`jobs` schema transition): Core-owned
   `core.jobs` stream with its own schema; requires ADR + tenant
   decision per Constitution §32 (tenant_id from first migration);
   the CRM-created empty `jobs` placeholder is either adopted with an
   explicit ownership-transfer migration or dropped in the same
   governed change that introduces the Core tables.
2. **Knowledge/ingestion metadata persistence**: new
   `core.<module>` streams; runtime Core role gains scoped grants
   per stream; Core runtime credential wiring lands with the first
   Core runtime consumer.
3. **Connector state**: per-connector decisions (MediaWiki, Email
   Agent) to move SQLite caches into owned schemas.
4. **GraphRAG SQLite → PostgreSQL**: own stream and ADR.
5. **Messaging full validation**: wire `pro.messaging` provider
   execution (Alembic bridge or SQL stream), end-to-end Compose
   validation, delivery workflow implementation (its own spec).
6. **Legacy ledger retirement**: after all deployments are migrated
   and verified, a CRM-owned governed change may drop
   `audit.schema_migrations` (CRM-owned object, CRM's decision).
7. **CI**: wire the registry check and full suites into continuous
   integration when CI infrastructure exists.
