# Spec 024 acceptance

Status: ACCEPTED (with the pack, 2026-10-04).  Records the executed
validation; commands and outcomes are copied into the task report.

## A. Migration framework (Constitution §39 deterministic tests)

1. Deterministic provider discovery — green.
2. Deterministic stream ordering (topological, stable ties) — green.
3. Duplicate provider/stream/version rejection — green.
4. Checksum mismatch rejection on applied migrations — green.
5. Successful first application — green.
6. Idempotent second run (no-op) — green.
7. Rollback of the failed migration's transaction (bad SQL leaves no
   ledger row / no partial DDL) — green.
8. Concurrent-run protection (advisory lock; two concurrent
   upgrades serialize without duplicates) — green.
9. Missing-dependency failure (unregistered dependency stream) —
   green.
10. Core-only operation with no Pro package installed (default
    providers = core.platform only; core repo has no Pro package) —
    green.
11. Inability of a Pro provider to impersonate a `core.*` stream —
    green.
12. Legacy-ledger adoption (validated, identity-preserving, no
    legacy mutation, partial-state failure) — green.
13. Licensing boundary: core platform modules import no
    `retriva_crm_assistant`/`retriva_messaging` symbol — green.

## B. CRM compatibility

1. CRM migrations register through the provider mechanism
   (`re.crm` stream via `MIGRATION_PROVIDERS`) — green.
2. A clean database receives the complete CRM schema through the
   provider runner — green.
3. An existing development-style database (legacy ledger rows,
   business data) upgrades without data loss via adoption — green
   (restored-copy validation of the live dump, then the live
   database itself).
4. Existing constraints, indexes, functions, triggers, RLS policies
   remain present (`verify` oracle + catalog probes) — green.
5. CRM runtime can access its required schemas (application role
   probe unchanged) — green.
6. Core runtime cannot write (nor read) CRM-owned tables (no
   grants; privilege probe) — green.
7. Migration rerun is a no-op — green.
8. Existing CRM PG suites (organizations, campaigns, ACP schema/
   history/tenant isolation, ERP identity/txt import, audit, pool,
   readiness, roles security) — green.

## C. Compose

1. `docker compose config` (base), `--profile pro`,
   `--profile db`, `--profile messaging`,
   `--profile connectors` — green.
2. Core-only path: default resolution includes
   `retriva-postgres`, `retriva-pg-bootstrap`,
   `retriva-pg-migrate` and no Pro service — green.
3. Core services depend on `retriva-pg-migrate:
   service_completed_successfully` — green.
4. CRM migrate one-shot depends on Core migrate + CRM bootstrap —
   green; no Core service depends on the CRM one-shots — green.
5. Messaging draft requires no separate database service (uses
   shared `retriva-postgres`/`retriva`) — green; dedicated service
   removed.
6. Health checks expose no credentials; images pinned; pgAdmin
   localhost bind — green.

## D. Security

1. Runtime identities cannot perform prohibited DDL (role privilege
   probes) — green.
2. Core runtime cannot write CRM tables — green.
3. CRM runtime does not gain schema ownership or unnecessary
   privileges (unchanged grants; `verify`) — green.
4. RLS and tenant protections unchanged (forced RLS probe, fail
   closed) — green.
5. Credentials/connection secrets redacted from logs and errors
   (repr/dump probes, no-DSN error contract) — green.

## E. Operational acceptance (Constitution §37)

1. Live-run through the deployed interface: the Compose one-shot
   lifecycle ran against the live development database after
   restored-copy validation — green.
2. Persistence across container recreation: volume
   `retriva_pg_data` and CRM data preserved (row counts and ledger
   identity preserved; volume never recreated) — green.
3. Repeated execution proving semantic idempotency (rerun no-op on
   the live database) — green.
4. Failure-path validation (checksum drift, failed migration
   rollback, missing dependency, impersonation, adoption mismatch)
   — green in scratch tests.
5. Reconciliation: `status`/`verify` outputs inspected; legacy
   ledger rows preserved read-only; new ledger rows equal the
   shipped CRM migrations — green.

## F. Documentation

Development documentation updated (mandatory PostgreSQL, shared
model, ownership, startup paths, ordering, provider registration,
streams, configuration/deprecations, status inspection, psql,
backup, reset-dev-state, volume preservation, not-migrated list,
messaging target, dev/test phase) — done.

## V. Executed validation commands

Recorded verbatim in the task report (§12).  Any command that could
not run in this environment is listed there with the limitation and
the outstanding validation.

## W. Container-level validation (2026-10-04 continuation phase)

Recorded evidence (full logs under the session's validation log set;
isolated Compose projects spec024core/spec024pro/spec024upgrade/
spec024conc, all torn down with volumes after the runs; the live
development stack ran untouched and was verified intact after every
phase):

1. **Pre-upgrade backup preserved** at
   `/mnt/devel/retriva-backups/retriva-pg-2026-10-04-preupgrade.dump`
   (sha256 `937bb162…`; `pg_restore --list` valid; contains NO
   `platform` objects — genuinely pre-migration; never committed).
2. **Images rebuilt from source** (never cached pre-Spec-024 images):
   core base + core services + gateway (Core-only `base` target),
   Pro image (target `pro`), Messaging image.  In-image proof:
   platform package imports + both V001 SQL files present + CLI runs
   in the Core image; `retriva_crm_assistant`/`retriva_messaging`
   NOT importable in Core images (asserted in-container); CRM
   provider imports with 8 migrations + 16 SQL files in the Pro
   image; Messaging models/schema/alembic env/entrypoints verified
   in-image.
3. **Core-only lifecycle (spec024core, fresh volumes, no Pro
   credential values, default `base` build targets):** PostgreSQL
   healthy → bootstrap exit 0 (`created: [retriva_migrator,
   retriva_core]`) → `core.platform` V1 applied by `retriva_migrator`
   → ingestion/core/worker/gateway all healthy with `/health` 200
   on all three published ports; ledger contains ONLY `core.platform`;
   only the `platform` schema exists; `retriva_core` non-superuser,
   reads the ledger, denied DDL and denied ledger writes; stop
   (volumes kept) → restart → bootstrap/migrate reruns exit 0 as
   no-ops → all services healthy again.
4. **CRM clean-database lifecycle (spec024pro):** Core services
   started healthy BEFORE any CRM migration with zero CRM schemas
   (Core did not depend on CRM migration success); CRM bootstrap +
   migrations then applied `pro.crm` V1–V8; ledger = core.platform 1
   + pro.crm 8; CRM oracle 26/26 green in-container; every one-shot
   rerun exit 0 no-op; all services healthy; all endpoints 200.
5. **CRM existing-database upgrade (spec024upgrade):** the preserved
   pre-upgrade dump restored into an isolated fresh cluster (roles
   pre-created; zero-error restore): 8 legacy ledger rows, 10
   organizations, no `platform` schema (verified pre-upgrade state);
   Core bootstrap+migrations; CRM bootstrap; adoption of exactly 8
   records ONCE each with original checksums, original applied_at
   timestamps (2026-09-23…28) and applied_by `retriva_migrator`;
   organizations=10 preserved; legacy ledger 8 rows intact; rerun
   no-op; oracle 26/26; Pro services healthy against the upgraded
   database (endpoints 200).
6. **Failure isolation:** invalid provider module → `MigrationError`
   naming the module and `RETRIVA_PG_MIGRATION_PROVIDERS`, one-shot
   exit 1, core services unaffected; bad migrator password → clear
   `PostgresConnectionError`; ledger checksum tamper (V008) → safe
   refusal (`checksum drift`), restoration, no-op recovery; two
   concurrent migration one-shots against one fresh database → both
   exit 0, exactly one `core.platform` ledger row (advisory-lock
   serialization); one-shot logs scanned for every validation
   credential value — clean (the only occurrences in any log are
   two server-side FATAL lines produced by the validation script's
   own typo passing a password as a role name — not a platform
   component).
7. **Messaging shared-database lifecycle (spec024pro + messaging):**
   bootstrap exit 0 (`schema messaging owned by retriva_migrator`,
   `retriva_messaging` runtime role, USAGE grant); Alembic migration
   exit 0; `messaging.alembic_version` = `0001_initial`; all 8
   tables in `messaging`; zero Messaging objects in `public`; no
   `retriva_messaging` database; no dedicated service/volume;
   idempotent reruns; runtime identity DML-verified (INSERT worked;
   the one constraint rejection proved the probe reached the table)
   and denied DDL/CRM/platform access; CRM runtime denied messaging
   access; core runtime reads only the ledger (9 rows) and is
   denied messaging; the draft Messaging service started and
   reports `{"status":"healthy",...}` against the shared database
   with its own log redacting the connection credential.  Messaging
   still uses Alembic and is NOT yet wired as an executed
   `pro.messaging` provider — unchanged, documented.
8. **Role model from catalogs:** `platform`/`messaging`/`business`/
   `audit` schemas and all their tables owned by `retriva_migrator`
   (no `retriva_messaging_owner` role exists); database CREATE held
   only by the admin superuser and `retriva_migrator`; DDL denied for
   `retriva_core`, `retriva_messaging`, and `retriva_application` on
   every schema probed.
9. **Regression suites after the fixes:** Core 737 passed (failure
   set byte-identical to the proven baseline), CRM 1326 passed (3
   proven pre-existing failures), Messaging 57 passed, deployment 78
   passed; all Compose profile resolutions valid; `manage.sh` shell
   syntax OK; compile/lint clean for every changed file.
10. **Defects found and fixed by this validation:** (a) the compose
    platform one-shots lacked a build `target`, which would have
    built the Pro image for Core bootstrap/migration — fixed to the
    Core-only `base` stage (with the core Dockerfile's first stage
    named `base`, matching the gateway Dockerfile convention);
    (b) the base stage requires the repo build context while the
    pro stage requires the workspace-parent context — the platform
    one-shots now pin their own repo-relative context variables
    (`RETRIVA_PG_TOOLS_CONTEXT`/`RETRIVA_PG_TOOLS_DOCKERFILE`);
    (c) an unimportable provider module raised a bare ImportError —
    now a clear, actionable `MigrationError`; (d) validation
    isolation flaw (shared `retriva-local-net` network across
    Compose projects causing DNS cross-talk to the live stack) —
    fixed in the validation overrides with per-project networks;
    the live deployment was verified undamaged after the incident
    (ledger 9+8 rows, data intact, role passwords unchanged, all
    services healthy).
