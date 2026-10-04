# Copyright (C) 2026 Retriva.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.  See the License for the specific language governing
# permissions and limitations under the License.

"""Integration tests for the shared-PostgreSQL platform (Spec 024).

Runs against a real PostgreSQL server (scratch cluster or
``RETRIVA_PG_TEST_ADMIN_URL``); skips with an explicit reason when
no server is available.  Every test works on its OWN fresh scratch
database (roles are cluster-level and bootstrapped once by the
session fixture).  Covers the full migration lifecycle: bootstrap,
first application, idempotent rerun, checksum drift, failed-
migration rollback, concurrent-run protection, legacy-ledger
adoption, downgrade gating, role privileges, and the readiness
probe.
"""

from __future__ import annotations

import threading

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from retriva.infrastructure.postgres.errors import (  # noqa: E402
    MigrationError,
)
from retriva.infrastructure.postgres.migrations import (  # noqa: E402
    CORE_PLATFORM_STREAM,
    LegacyLedger,
    ProviderRegistry,
    SqlMigrationProvider,
    applied_revisions,
    checksum_for,
    downgrade,
    load_provider_registry,
    platform_provider,
    status,
    upgrade,
    verify,
)

# --- helpers ---------------------------------------------------------------


def write_stream(tmp_path, prefix, migrations):
    """Create a SQL-file stream directory; returns its path."""
    directory = tmp_path / prefix.replace(".", "_")
    directory.mkdir(exist_ok=True)
    for version, up, down in migrations:
        (directory / f"V{version:03d}__step{version:03d}.up.sql"
         ).write_text(up, encoding="utf-8")
        (directory / f"V{version:03d}__step{version:03d}.down.sql"
         ).write_text(down, encoding="utf-8")
    return directory


def ext_provider(tmp_path, name="retriva-ext-test", stream="pro.ext",
                 migrations=None, dependencies=(), required_roles=(),
                 legacy=None, bootstrap_sql=""):
    migrations = migrations or [
        (1,
         "CREATE SCHEMA IF NOT EXISTS ext_test AUTHORIZATION "
         "retriva_migrator;\n"
         "CREATE TABLE ext_test.items (id INT PRIMARY KEY);\n",
         "DROP SCHEMA IF EXISTS ext_test CASCADE;\n"),
    ]
    return SqlMigrationProvider(
        provider_id=name, stream_id=stream,
        sql_dir=write_stream(tmp_path, stream, migrations),
        dependencies=tuple(dependencies),
        required_roles=tuple(required_roles),
        legacy_ledger=legacy,
        bootstrap_sql=bootstrap_sql,
    )


def registry_with(*providers) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register_core_platform(platform_provider())
    for provider in providers:
        registry.register(provider)
    return registry


def _connect(settings, role):
    conn = psycopg2.connect(**settings.connection_kwargs(role))
    conn.autocommit = True
    return conn


def _scalar(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


# --- core lifecycle --------------------------------------------------------


def test_core_only_upgrade_applies_platform_and_is_idempotent(
        pg_platform_stack):
    settings = pg_platform_stack.fresh_database("retriva_pg_test_core")
    registry = load_provider_registry("")  # Core-only: no Pro package
    assert registry.stream_ids() == [CORE_PLATFORM_STREAM]

    result = upgrade(registry, settings)
    stream_result = result[CORE_PLATFORM_STREAM][0]
    assert [a["version"] for a in stream_result["applied"]] == [1]

    # The ledger exists, is migrator-owned, and is readable by the
    # Core runtime role.
    admin = pg_platform_stack.admin(settings.database)
    try:
        assert _scalar(
            admin,
            "SELECT count(*) FROM platform.schema_migrations") == 1
        owner = _scalar(
            admin,
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace "
            "WHERE nspname = 'platform'")
        assert owner == "retriva_migrator"
    finally:
        admin.close()
    core = _connect(settings, "core")
    try:
        assert _scalar(
            core,
            "SELECT count(*) FROM platform.schema_migrations") == 1
    finally:
        core.close()

    # Idempotent second run: nothing applied, no error.
    result = upgrade(registry, settings)
    assert result[CORE_PLATFORM_STREAM][0]["applied"] == []
    assert result[CORE_PLATFORM_STREAM][0]["adopted"] == []

    # Framework verify passes.
    report = verify(registry, settings)
    assert report["ok"] is True


def test_extension_stream_runs_after_core_and_creates_its_schema(
        pg_platform_stack, tmp_path):
    settings = pg_platform_stack.fresh_database(
        "retriva_pg_test_ext")
    provider = ext_provider(tmp_path)
    registry = registry_with(provider)
    assert registry.stream_ids() == [
        CORE_PLATFORM_STREAM, provider.stream_id]

    result = upgrade(registry, settings)
    assert [a["version"] for a in
            result[CORE_PLATFORM_STREAM][0]["applied"]] == [1]
    assert [a["version"] for a in
            result[provider.stream_id][0]["applied"]] == [1]

    admin = pg_platform_stack.admin(settings.database)
    try:
        assert _scalar(
            admin, "SELECT count(*) FROM ext_test.items") == 0
        # Core runtime role cannot read or write extension tables.
        core = _connect(settings, "core")
        try:
            with pytest.raises(psycopg2.Error):
                core.cursor().execute(
                    "SELECT count(*) FROM ext_test.items")
            with pytest.raises(psycopg2.Error):
                core.cursor().execute(
                    "INSERT INTO ext_test.items VALUES (1)")
        finally:
            core.close()
    finally:
        admin.close()

    assert verify(registry, settings)["ok"] is True


def test_checksum_drift_on_applied_migration_fails_safely(
        pg_platform_stack, tmp_path):
    settings = pg_platform_stack.fresh_database(
        "retriva_pg_test_drift")
    provider = ext_provider(tmp_path)
    registry = registry_with(provider)
    upgrade(registry, settings)

    # Tamper with the applied migration on disk.
    (provider._sql_dir / "V001__step001.up.sql").write_text(  # noqa: SLF001
        "CREATE SCHEMA IF NOT EXISTS ext_test AUTHORIZATION "
        "retriva_migrator;\n"
        "CREATE TABLE ext_test.items (id INT PRIMARY KEY, extra INT);\n",
        encoding="utf-8")

    with pytest.raises(MigrationError) as excinfo:
        upgrade(registry, settings)
    assert "checksum drift" in str(excinfo.value)
    # The applied state is untouched.
    conn = _connect(settings, "migrator")
    try:
        rows = applied_revisions(conn, provider.provider_id,
                                 provider.stream_id)
        assert sorted(rows) == [1]
    finally:
        conn.close()


def test_failed_migration_rolls_back_completely(
        pg_platform_stack, tmp_path):
    settings = pg_platform_stack.fresh_database(
        "retriva_pg_test_rollback")
    provider = ext_provider(tmp_path, migrations=[
        (1, "CREATE SCHEMA IF NOT EXISTS ext_test AUTHORIZATION "
            "retriva_migrator;\n"
            "CREATE TABLE ext_test.items (id INT PRIMARY KEY);\n",
         "DROP SCHEMA IF EXISTS ext_test CASCADE;\n"),
        # Valid DDL followed by an invalid statement: the whole
        # migration must roll back (no half-applied table).
        (2, "CREATE TABLE ext_test.second (id INT PRIMARY KEY);\n"
            "THIS IS NOT VALID SQL;\n",
         "DROP TABLE ext_test.second;\n"),
    ])
    registry = registry_with(provider)

    with pytest.raises(MigrationError):
        upgrade(registry, settings)

    conn = _connect(settings, "migrator")
    try:
        # V002 left no table and no ledger row.
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('ext_test.second') IS NULL")
            assert cur.fetchone()[0] is True
        rows = applied_revisions(conn, provider.provider_id,
                                 provider.stream_id)
        assert sorted(rows) == [1]
    finally:
        conn.close()

    # The framework reports the stream as not fully applied.
    report = verify(registry, settings)
    assert report["ok"] is False


def test_verify_fails_when_extension_stream_not_applied(
        pg_platform_stack, tmp_path):
    """Drift scenario: an extension provider is registered but its
    stream is not applied (for example the extension is enabled while
    its migration provider was omitted from the one-shot, or the
    migration step failed earlier).  ``verify`` must fail clearly so
    the deployment never reports healthy against a missing required
    schema."""
    settings = pg_platform_stack.fresh_database(
        "retriva_pg_test_drift_pending")
    provider = ext_provider(tmp_path)  # stream pro.ext, never applied
    registry = registry_with(provider)
    upgrade(load_provider_registry(""), settings)  # core.platform only
    report = verify(registry, settings)
    assert report["ok"] is False
    failed = [c["check"] for c in report["checks"] if not c["ok"]]
    assert any("pro.ext" in name for name in failed), failed
    # status shows the stream as pending.
    report = status(registry, settings)
    stream = report["streams"]["pro.ext"]
    assert [p["version"] for p in stream["pending"]] == [1]


def test_concurrent_upgrades_serialize_without_duplicates(
        pg_platform_stack, tmp_path):
    settings = pg_platform_stack.fresh_database(
        "retriva_pg_test_concurrent")
    provider = ext_provider(
        tmp_path, migrations=[
            (1, "CREATE SCHEMA IF NOT EXISTS ext_test AUTHORIZATION "
                "retriva_migrator;\n",
             "DROP SCHEMA ext_test CASCADE;\n"),
            (2, "CREATE TABLE ext_test.items (id INT PRIMARY KEY);\n",
             "DROP TABLE ext_test.items;\n"),
        ])
    registry = registry_with(provider)

    errors = []
    results = []

    def runner():
        try:
            results.append(upgrade(registry, settings))
        except Exception as exc:  # noqa: BLE001 - captured for assert
            errors.append(exc)

    threads = [threading.Thread(target=runner) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert errors == []
    conn = _connect(settings, "migrator")
    try:
        rows = applied_revisions(conn, provider.provider_id,
                                 provider.stream_id)
        assert sorted(rows) == [1, 2]
        with conn.cursor() as cur:
            cur.execute(
                "SELECT version, count(*) FROM platform.schema_migrations "
                "WHERE stream = 'pro.ext' GROUP BY version")
            counts = {r[0]: r[1] for r in cur.fetchall()}
        assert counts == {1: 1, 2: 1}
    finally:
        conn.close()


def test_missing_required_role_fails_with_bootstrap_hint(
        pg_platform_stack, tmp_path):
    settings = pg_platform_stack.fresh_database(
        "retriva_pg_test_roles")
    provider = ext_provider(
        tmp_path, required_roles=("retriva_nonexistent_role",))
    registry = registry_with(provider)
    with pytest.raises(MigrationError) as excinfo:
        upgrade(registry, settings)
    assert "retriva_nonexistent_role" in str(excinfo.value)
    assert "bootstrap" in str(excinfo.value)


# --- legacy ledger adoption ---------------------------------------------------

LEGACY_BOOTSTRAP_SQL = (
    "CREATE SCHEMA IF NOT EXISTS legacy_ext AUTHORIZATION "
    "retriva_migrator;\n"
    "CREATE TABLE IF NOT EXISTS legacy_ext.schema_migrations (\n"
    "    version    INTEGER PRIMARY KEY,\n"
    "    name       TEXT NOT NULL,\n"
    "    checksum   TEXT NOT NULL,\n"
    "    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),\n"
    "    applied_by TEXT NOT NULL DEFAULT current_user);\n"
    "ALTER TABLE IF EXISTS legacy_ext.schema_migrations "
    "OWNER TO retriva_migrator;\n"
)


def _legacy_provider(tmp_path, migrations):
    return SqlMigrationProvider(
        provider_id="retriva-legacy-test",
        stream_id="pro.legacy",
        sql_dir=write_stream(tmp_path, "pro.legacy", migrations),
        dependencies=(),
        legacy_ledger=LegacyLedger(
            table="legacy_ext.schema_migrations"),
        bootstrap_sql=LEGACY_BOOTSTRAP_SQL,
    )


def _seed_legacy(settings, rows):
    """Create a legacy ledger pre-populated as by an older runner
    (migrator-owned) in the given test database."""
    conn = _connect(settings, "migrator")
    try:
        with conn.cursor() as cur:
            cur.execute(LEGACY_BOOTSTRAP_SQL)
            for version, name, checksum in rows:
                cur.execute(
                    "INSERT INTO legacy_ext.schema_migrations "
                    "(version, name, checksum) VALUES (%s, %s, %s)",
                    (version, name, checksum))
    finally:
        conn.close()


def test_legacy_ledger_adoption_preserves_identity(
        pg_platform_stack, tmp_path):
    settings = pg_platform_stack.fresh_database(
        "retriva_pg_test_adopt")
    migrations = [
        (1, "CREATE SCHEMA IF NOT EXISTS legacy_ext AUTHORIZATION "
            "retriva_migrator;\n",
         "DROP SCHEMA legacy_ext CASCADE;\n"),
        (2, "CREATE TABLE legacy_ext.items (id INT PRIMARY KEY);\n",
         "DROP TABLE legacy_ext.items;\n"),
    ]
    provider = _legacy_provider(tmp_path, migrations)

    # A development-style pre-existing database: the legacy ledger
    # already records both migrations as applied.
    checksums = {
        version: checksum_for(up, down)
        for version, up, down in migrations}
    _seed_legacy(settings,
                 [(1, "step001", checksums[1]),
                  (2, "step002", checksums[2])])

    registry = registry_with(provider)
    result = upgrade(registry, settings)
    adopted = result[provider.stream_id][0]["adopted"]
    assert [a["version"] for a in adopted] == [1, 2]
    # Nothing was (re-)applied.
    assert result[provider.stream_id][0]["applied"] == []

    conn = _connect(settings, "migrator")
    try:
        rows = applied_revisions(conn, provider.provider_id,
                                 provider.stream_id)
        assert sorted(rows) == [1, 2]
        # Identity preserved: original checksums and applier.
        assert rows[1]["checksum"] == checksums[1]
        assert rows[2]["checksum"] == checksums[2]
        assert rows[1]["applied_by"] == "retriva_migrator"
        # The legacy table is preserved read-only.
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM "
                        "legacy_ext.schema_migrations")
            assert cur.fetchone()[0] == 2
    finally:
        conn.close()

    # Rerun: full no-op (adoption already done, nothing applied).
    result = upgrade(registry, settings)
    assert result[provider.stream_id][0]["adopted"] == []
    assert result[provider.stream_id][0]["applied"] == []
    assert verify(registry, settings)["ok"] is True


def test_adoption_rejects_unknown_legacy_version(
        pg_platform_stack, tmp_path):
    settings = pg_platform_stack.fresh_database(
        "retriva_pg_test_adopt_unknown")
    migrations = [
        (1, "CREATE SCHEMA IF NOT EXISTS legacy_ext AUTHORIZATION "
            "retriva_migrator;\n",
         "DROP SCHEMA legacy_ext CASCADE;\n"),
    ]
    provider = _legacy_provider(tmp_path, migrations)
    _seed_legacy(settings,
                 [(99, "ghost_step",
                   checksum_for("SELECT 1", "SELECT 1"))])
    registry = registry_with(provider)
    with pytest.raises(MigrationError) as excinfo:
        upgrade(registry, settings)
    assert "no shipped migration" in str(excinfo.value)


def test_adoption_rejects_legacy_checksum_drift(
        pg_platform_stack, tmp_path):
    settings = pg_platform_stack.fresh_database(
        "retriva_pg_test_adopt_drift")
    migrations = [
        (1, "CREATE SCHEMA IF NOT EXISTS legacy_ext AUTHORIZATION "
            "retriva_migrator;\n",
         "DROP SCHEMA legacy_ext CASCADE;\n"),
    ]
    provider = _legacy_provider(tmp_path, migrations)
    _seed_legacy(settings,
                 [(1, "step001", "deadbeef")])
    registry = registry_with(provider)
    with pytest.raises(MigrationError) as excinfo:
        upgrade(registry, settings)
    assert "checksum drift" in str(excinfo.value)


def test_adoption_refuses_ambiguous_partial_state(
        pg_platform_stack, tmp_path):
    settings = pg_platform_stack.fresh_database(
        "retriva_pg_test_adopt_partial")
    migrations = [
        (1, "CREATE SCHEMA IF NOT EXISTS legacy_ext AUTHORIZATION "
            "retriva_migrator;\n",
         "DROP SCHEMA legacy_ext CASCADE;\n"),
        (2, "CREATE TABLE legacy_ext.items (id INT);\n",
         "DROP TABLE legacy_ext.items;\n"),
    ]
    provider = _legacy_provider(tmp_path, migrations)
    checksums = {
        version: checksum_for(up, down)
        for version, up, down in migrations}
    _seed_legacy(settings,
                 [(1, "step001", checksums[1]),
                  (2, "step002", checksums[2])])
    # A partial new-ledger state that cannot result from the
    # all-or-nothing adoption: refuse to guess.
    from retriva.infrastructure.postgres.migrations import ensure_ledger
    conn = _connect(settings, "migrator")
    try:
        conn.autocommit = True
        ensure_ledger(conn, "retriva_migrator")
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO platform.schema_migrations "
                "(provider, stream, version, name, checksum) "
                "VALUES (%s, %s, %s, %s, %s)",
                (provider.provider_id, provider.stream_id, 2,
                 "step002", "irrelevant"))
        conn.commit()
    finally:
        conn.close()
    registry = registry_with(provider)
    with pytest.raises(MigrationError) as excinfo:
        upgrade(registry, settings)
    assert "ambiguous adoption state" in str(excinfo.value)


# --- downgrade ---------------------------------------------------------------


def test_downgrade_requires_confirmation_and_reverts(
        pg_platform_stack, tmp_path):
    settings = pg_platform_stack.fresh_database(
        "retriva_pg_test_downgrade")
    provider = ext_provider(
        tmp_path, migrations=[
            (1, "CREATE SCHEMA IF NOT EXISTS ext_test AUTHORIZATION "
                "retriva_migrator;\n",
             "DROP SCHEMA ext_test CASCADE;\n"),
        ])
    registry = registry_with(provider)
    upgrade(registry, settings)

    with pytest.raises(MigrationError):
        downgrade(registry, settings, provider.stream_id, to=0)

    reverted = downgrade(registry, settings, provider.stream_id, to=0,
                         confirm_destructive=True)
    assert [r["version"] for r in reverted] == [1]

    conn = _connect(settings, "migrator")
    try:
        rows = applied_revisions(conn, provider.provider_id,
                                 provider.stream_id)
        assert rows == {}
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('ext_test.items') IS NULL")
            assert cur.fetchone()[0] is True
    finally:
        conn.close()

    # Re-upgrade restores the stream (reversible phases).
    result = upgrade(registry, settings)
    assert [a["version"] for a in
            result[provider.stream_id][0]["applied"]] == [1]


# --- role privileges -----------------------------------------------------------


def test_core_runtime_role_cannot_create_schemas(
        pg_platform_stack):
    # Roles are cluster-level; any scratch database works.
    settings = pg_platform_stack.settings
    core = _connect(settings, "core")
    try:
        with pytest.raises(psycopg2.Error) as excinfo:
            core.cursor().execute(
                "CREATE SCHEMA rogue_schema AUTHORIZATION retriva_core")
        # Permission denied (CREATE on database is migrator-only).
        assert "permission denied" in str(excinfo.value).lower()
    finally:
        core.close()


def test_migrator_owns_platform_and_core_cannot_write_ledger(
        pg_platform_stack):
    settings = pg_platform_stack.fresh_database(
        "retriva_pg_test_privs")
    registry = load_provider_registry("")
    upgrade(registry, settings)

    core = _connect(settings, "core")
    try:
        with pytest.raises(psycopg2.Error):
            core.cursor().execute(
                "INSERT INTO platform.schema_migrations "
                "(provider, stream, version, name, checksum) "
                "VALUES ('x', 'y', 1, 'z', 'w')")
    finally:
        core.close()


# --- status and readiness -------------------------------------------------------


def test_status_reports_per_stream_state(pg_platform_stack, tmp_path):
    settings = pg_platform_stack.fresh_database(
        "retriva_pg_test_status")
    provider = ext_provider(tmp_path)
    registry = registry_with(provider)
    upgrade(registry, settings)
    report = status(registry, settings)
    assert set(report["streams"]) == {
        CORE_PLATFORM_STREAM, provider.stream_id}
    stream = report["streams"][provider.stream_id]
    assert stream["current_revision"] == 1
    assert stream["latest_available"] == 1
    assert stream["pending"] == []
    assert stream["applied"][0]["name"] == "step001"
    assert stream["applied"][0]["applied_by"] == "retriva_migrator"


def test_platform_readiness_report_has_no_credentials(
        pg_platform_stack):
    from retriva.infrastructure.postgres.migrate import (
        platform_readiness,
    )
    settings = pg_platform_stack.fresh_database(
        "retriva_pg_test_readiness")
    registry = load_provider_registry("")
    upgrade(registry, settings)

    payload = platform_readiness(settings)
    assert payload["status"] == "ok"
    assert payload["connectivity"] == {"reachable": True}
    assert payload["ledger_readable"] is True
    assert payload["applied_migrations"] == 1

    import json
    rendered = json.dumps(payload, default=str)
    for role in ("admin", "migrator", "core"):
        password = settings.resolved_password(role)
        assert password not in rendered
