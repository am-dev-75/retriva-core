# Spec 024 tasks

Status: ACCEPTED (with the pack, 2026-10-04)

## A. Governance

- [x] Create `retriva-core/docs/governance/spec-adr-registry.yaml`
      (backfill 001–023 specs, 001–028 + gateway-local ADRs; allocate
      Spec 024 + ADR-029).
- [x] Spec 024 pack (`spec.md`, `architecture.md`, `plan.md`,
      `tasks.md`, `acceptance.md`).
- [x] ADR-029 (`retriva-crm-assistant/docs/adr/
      adr-029-centralized-relational-db.md`).
- [x] Deterministic registry integrity check test in core tests.

## B. Core platform package (`retriva.infrastructure.postgres`)

- [x] `errors.py` (platform error taxonomy, no secrets in messages).
- [x] `config.py` (`PostgresPlatformSettings`, `RETRIVA_PG_*`,
      `*_FILE` indirection, `connection_kwargs`, fail-closed
      credential errors, no-secret rendering).
- [x] `bootstrap.py` (`provision_roles`, `restrict_database_create`,
      idempotent, refuses elevated roles).
- [x] `migrations.py` (`Migration`, `LegacyLedger`,
      `MigrationProvider`, `SqlMigrationProvider`,
      `ProviderRegistry`, ledger, ordering, adoption, upgrade,
      downgrade, status, verify, advisory lock, transactions).
- [x] `migrate_cli.py` + `__main__` (bootstrap/upgrade/status/
      downgrade/verify/readiness; providers via
      `RETRIVA_PG_MIGRATION_PROVIDERS`).
- [x] `sql/V001__platform_ledger.{up,down}.sql` (platform schema,
      Core-runtime grants).
- [x] `psycopg2-binary` in core requirements (lazy import).

## C. Core tests

- [x] Unit: contract (registry ordering, duplicates, impersonation,
      dependencies, checksum determinism, discovery pairing).
- [x] Unit: config hygiene (no secret leaks, file indirection,
      alias precedence, missing-credential failures).
- [x] Governance: registry integrity check (duplicates, dangling
      paths, PROPOSED/ACCEPTED without entry).
- [x] Licensing boundary test: core platform code imports no
      Pro package.
- [x] Integration (scratch cluster / admin URL): bootstrap →
      upgrade → rerun no-op → checksum drift → failed-migration
      rollback → concurrent-run protection → legacy adoption →
      core-only defaults → messaging-style provider registration.

## D. CRM Assistant adaptation

- [x] `postgres/migrations.py` provider module (`pro.crm`,
      legacy ledger, downgrade guard, roles).
- [x] `postgres/config.py` aliases to `RETRIVA_PG_*` canonicals;
      CRM roles unchanged.
- [x] `postgres/migrate.py` delegates to Core runner; CLI compat;
      `bootstrap-roles` via Core `provision_roles`.
- [x] `readiness.py` ledger reads via read-only migrator probe
      (documented); application-role business probe unchanged.
- [x] `verify` reads stream status from `platform.schema_migrations`
      (CRM domain invariants unchanged).
- [x] conftest `pg_stack`: Core bootstrap + CRM bootstrap + provider
      upgrade (incl. core.platform).
- [x] Tests: updated `test_pg_config`, `test_pg_migrations`,
      `test_pg_roles_security`; new provider/adoption tests; all
      PG suites green.

## E. Messaging preparation

- [x] `db_models.py`: `MetaData(schema="messaging")`.
- [x] Alembic `env.py`/`alembic.ini`: version table in `messaging`
      schema.
- [x] Config default URL → shared `retriva` database; dedicated
      runtime role env; tests updated.
- [x] README/`.env.example` updated (dedicated DB removed).

## F. Deployment

- [x] Compose: mandatory `retriva-postgres`/`retriva-pg-bootstrap`/
      `retriva-pg-migrate` (no profile; core build target);
      dependency graph per architecture.md §1.4;
      `retriva-pg-crm-bootstrap`/`retriva-pg-crm-migrate` (pro
      profile); `retriva-messaging-db` + `messaging_db_data` +
      `MESSAGING_DB_*` removed; messaging one-shots +
      shared-DB wiring; pgAdmin profiles kept.
- [x] `manage.sh`: up/up-pro include the PG lifecycle;
      `_require_db_env` core + Pro credential sets; db-* commands
      updated; help updated.
- [x] `.env.example`: new `RETRIVA_PG_*` block; deprecated
      `CRM_PG_*` compatibility notes; messaging block retargeted.
- [x] Docs: `docs/postgres-business-intelligence.md` updated for the
      mandatory shared-platform model + new operator runbook
      entries (backup, status, psql, reset-dev-state, volume
      preservation).
- [x] Deployment tests rewritten (profiles, dependencies, env
      contract, manage.sh contract, no-messaging-db).

## G. Validation

- [x] Core/CRM/messaging/deployment test suites executed.
- [x] `docker compose config` for base and profiles.
- [x] Scratch-DB lifecycle (bootstrap, core upgrade, CRM upgrade,
      adoption, rerun no-op, verify).
- [x] Live-DB backup → restored-copy upgrade validation → live
      adoption through the Compose one-shots.
- [x] Final diff review (Pro→Core leakage, data risk, security,
      scope).

## H. Container-level validation (2026-10-04 continuation phase)

- [x] Pre-upgrade backup preserved at
      `/mnt/devel/retriva-backups/retriva-pg-2026-10-04-preupgrade.dump`
      (sha256 937bb162…; verified pre-migration state via
      `pg_restore --list`: no `platform` objects; never committed).
- [x] Repository state frozen and recorded (branch/commit/status per
      repo; no unrelated work; nothing staged).
- [x] §3.1 true-Core-only fix: core Dockerfile first stage named
      `base` (gateway convention); platform one-shots build target
      `base` (previous implementation built the LAST stage — the Pro
      image — for the Core one-shots: a real defect found and fixed);
      CRM one-shots always build `pro` (`RETRIVA_PG_CRM_BUILD_TARGET`);
      deployment tests assert the target split.
- [x] §3.2 provider/extension drift: unimportable provider module now
      raises `MigrationError` (clear, actionable); drift scenarios 1–7
      documented and tested (missing module, duplicate provider,
      pending-stream verify failure, core-only, CRM, Alembic
      exception).
- [x] Core-only images rebuilt from source (base + core services +
      gateway), NOT cached pre-Spec-024 images; in-image proof: no
      `retriva_crm_assistant`/`retriva_messaging` importable, platform
      package + SQL present, CLI runs.
- [x] Pro image rebuilt (target pro); in-image proof: CRM provider
      imports, Core platform package imports, CRM SQL present.
- [x] Messaging image rebuilt; in-image proof: models use schema
      `messaging`, alembic env targets the schema, migrate/bootstrap
      commands present.
- [x] Core-only lifecycle: isolated project (`spec024core`), fresh
      volumes, Core-only build/credentials: PostgreSQL healthy →
      bootstrap → `core.platform` migration → core services healthy;
      only Core streams recorded; core runtime DDL denial + ledger
      read; stop (volumes kept) → restart → no-op reruns → healthy.
- [x] CRM clean-database lifecycle (`spec024pro`): Core services
      healthy BEFORE CRM migrations; CRM one-shots apply `pro.crm`;
      oracle green; full idempotent reruns.
- [x] CRM existing-database upgrade (`spec024upgrade`): pre-upgrade
      dump restored into the isolated cluster (roles pre-created,
      faithful owner/privilege restore); Core bootstrap+migrations;
      CRM bootstrap; adoption of exactly 8 legacy records once, with
      original checksums/timestamps; data counts preserved; no-op
      rerun; oracle green; Pro services healthy.
- [x] Failure isolation: invalid provider module → clear one-shot
      failure, core services unaffected; CRM one-shot auth failure →
      clear error without credentials; ledger checksum tamper → safe
      refusal; two concurrent migration runners → advisory-lock
      serialization, exactly one ledger row; failure logs scanned for
      credentials.
- [x] Messaging shared-DB lifecycle (`spec024pro`): bootstrap creates
      `messaging` schema + `retriva_messaging` role; Alembic version
      table in `messaging`; all tables in `messaging`; nothing in
      `public`; no `retriva_messaging` database; no dedicated
      service/volume; idempotent reruns; service startup attempted at
      the draft boundary (result reported truthfully).
- [x] Role model verified from catalogs (owners, grants, CREATE
      privilege, DDL denials, Core↔CRM↔Messaging isolation).
- [x] Regression suites rerun and failure sets compared with the
      proven baselines.

Explicitly NOT done (out of scope): durable job tables; Celery/Redis
state migration; GraphRAG SQLite migration; connector-state migration;
document/attachment migration; model storage; Qdrant; CRM/Messaging
schema redesign; Messaging delivery; CRM→Messaging integration;
outbox; production HA/backup/secrets; Messaging provider-contract
execution wiring (full `pro.messaging` provider integration).
