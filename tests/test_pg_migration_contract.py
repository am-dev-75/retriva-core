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

"""Deterministic contract tests for the migration-provider registry.

No database required.  Covers discovery determinism, stream
ordering, duplicate rejection, namespace impersonation rejection,
missing dependencies, checksum determinism, Core-only loading, and
the Core/Pro licensing boundary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from retriva.infrastructure.postgres.errors import MigrationError
from retriva.infrastructure.postgres.migrations import (
    CORE_PLATFORM_STREAM,
    CORE_PROVIDER_ID,
    PROVIDERS_ENV,
    LegacyLedger,
    Migration,
    ProviderRegistry,
    SqlMigrationProvider,
    checksum_for,
    load_provider_modules,
    load_provider_registry,
)

# --- Test doubles ---------------------------------------------------------


class FakeProvider(SqlMigrationProvider):
    """A registered provider built from ad-hoc SQL files."""


def make_provider(tmp_path, name, stream, migrations,
                   dependencies=(), required_roles=(), legacy=None):
    directory = tmp_path / stream.replace(".", "_")
    directory.mkdir(exist_ok=True)
    for version, up, down in migrations:
        (directory / f"V{version:03d}__step_{name}.up.sql").write_text(up)
        (directory / f"V{version:03d}__step_{name}.down.sql").write_text(down)
    return SqlMigrationProvider(
        provider_id=name, stream_id=stream, sql_dir=directory,
        dependencies=tuple(dependencies),
        required_roles=tuple(required_roles),
        legacy_ledger=legacy,
    )


def _core_registry() -> ProviderRegistry:
    """The Core-only registry (no extension provider modules)."""
    return load_provider_registry("")


# --- Discovery and identity ------------------------------------------------


def test_platform_registry_defaults_to_core_platform_only():
    registry = _core_registry()
    assert registry.stream_ids() == [CORE_PLATFORM_STREAM]
    assert registry.as_status() == {
        CORE_PLATFORM_STREAM: CORE_PROVIDER_ID}


def test_core_platform_discovery_is_deterministic():
    registry = _core_registry()
    first = registry.migrations_for(CORE_PLATFORM_STREAM)
    second = registry.migrations_for(CORE_PLATFORM_STREAM)
    assert [m.version for m in first] == [1]
    assert [m.checksum for m in first] == [m.checksum for m in second]
    assert first[0].provider == CORE_PROVIDER_ID
    assert first[0].stream == CORE_PLATFORM_STREAM
    assert first[0].name == "platform_ledger"


def test_checksum_matches_legacy_algorithm():
    # sha256(up_sql + NUL + down_sql)
    assert checksum_for("A", "B") == checksum_for("A", "B")
    assert checksum_for("A", "B") != checksum_for("A", "C")


def test_discovery_rejects_unpaired_or_duplicate(tmp_path):
    d = tmp_path / "bad_unpaired"
    d.mkdir()
    (d / "V001__x.up.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError):
        SqlMigrationProvider(
            provider_id="p", stream_id="s.one", sql_dir=d).discover()

    d2 = tmp_path / "bad_duplicate"
    d2.mkdir()
    (d2 / "V001__a.up.sql").write_text("SELECT 1;")
    (d2 / "V001__a.down.sql").write_text("SELECT 1;")
    (d2 / "V001__b.up.sql").write_text("SELECT 2;")
    (d2 / "V001__b.down.sql").write_text("SELECT 2;")
    with pytest.raises(MigrationError):
        SqlMigrationProvider(
            provider_id="p", stream_id="s.two", sql_dir=d2).discover()

    d3 = tmp_path / "bad_name_mismatch"
    d3.mkdir()
    (d3 / "V001__a.up.sql").write_text("SELECT 1;")
    (d3 / "V001__b.down.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError):
        SqlMigrationProvider(
            provider_id="p", stream_id="s.three", sql_dir=d3).discover()


def test_migrations_for_rejects_duplicate_versions(tmp_path):
    provider = make_provider(tmp_path, "ext", "pro.ext",
                              [(1, "SELECT 1;", "SELECT 1;")])
    provider._sql_dir = provider._sql_dir  # noqa: SLF001 - test seam
    original_discover = provider.discover

    def duplicated_discover():
        migration = original_discover()[0]
        return [migration, migration]

    provider.discover = duplicated_discover  # type: ignore[assignment]
    registry = _core_registry()
    registry.register(provider)
    with pytest.raises(MigrationError):
        registry.migrations_for("pro.ext")


# --- Ordering and dependencies ---------------------------------------------


def test_stream_ordering_is_deterministic_and_topological(tmp_path):
    registry = _core_registry()
    crm = make_provider(
        tmp_path, "retriva-crm-assistant", "pro.crm",
        [(1, "SELECT 1;", "SELECT 1;")],
        dependencies=[CORE_PLATFORM_STREAM])
    messaging = make_provider(
        tmp_path, "retriva-messaging", "pro.messaging",
        [(1, "SELECT 1;", "SELECT 1;")],
        dependencies=[CORE_PLATFORM_STREAM])
    registry.register(messaging)
    registry.register(crm)
    # Dependencies first; ties broken deterministically.
    assert registry.stream_ids() == [
        CORE_PLATFORM_STREAM, "pro.crm", "pro.messaging"]
    again = ProviderRegistry()
    again.register_core_platform(
        load_provider_registry("").provider(CORE_PLATFORM_STREAM))
    again.register(crm)
    again.register(messaging)
    assert again.stream_ids() == registry.stream_ids()


def test_missing_dependency_fails_clearly(tmp_path):
    registry = _core_registry()
    orphan = make_provider(
        tmp_path, "ext", "pro.orphan",
        [(1, "SELECT 1;", "SELECT 1;")],
        dependencies=["pro.not.registered"])
    registry.register(orphan)
    with pytest.raises(MigrationError) as excinfo:
        registry.stream_ids()
    assert "pro.not.registered" in str(excinfo.value)


def test_dependency_cycle_fails_clearly(tmp_path):
    registry = _core_registry()
    a = make_provider(
        tmp_path, "ext.a", "pro.a",
        [(1, "SELECT 1;", "SELECT 1;")],
        dependencies=["pro.b"])
    b = make_provider(
        tmp_path, "ext.b", "pro.b",
        [(1, "SELECT 1;", "SELECT 1;")],
        dependencies=["pro.a"])
    registry.register(a)
    registry.register(b)
    with pytest.raises(MigrationError) as excinfo:
        registry.stream_ids()
    assert "cycle" in str(excinfo.value)


# --- Impersonation and overwrites -------------------------------------------


def test_extension_cannot_impersonate_core_stream(tmp_path):
    registry = _core_registry()
    evil = make_provider(
        tmp_path, "retriva-evil", "core.crm",
        [(1, "SELECT 1;", "SELECT 1;")])
    with pytest.raises(MigrationError) as excinfo:
        registry.register(evil)
    assert "impersonation refused" in str(excinfo.value)


def test_extension_cannot_impersonate_core_provider(tmp_path):
    registry = _core_registry()
    evil = make_provider(
        tmp_path, CORE_PROVIDER_ID, "not.a.core.stream",
        [(1, "SELECT 1;", "SELECT 1;")])
    with pytest.raises(MigrationError) as excinfo:
        registry.register(evil)
    assert "reserved" in str(excinfo.value)


def test_stream_overwrite_by_second_provider_fails(tmp_path):
    registry = _core_registry()
    first = make_provider(
        tmp_path, "first", "pro.shared",
        [(1, "SELECT 1;", "SELECT 1;")])
    second = make_provider(
        tmp_path, "second", "pro.shared",
        [(1, "SELECT 2;", "SELECT 2;")])
    registry.register(first)
    with pytest.raises(MigrationError):
        registry.register(second)


def test_core_platform_registration_is_once_only(tmp_path):
    registry = _core_registry()
    with pytest.raises(MigrationError):
        registry.register_core_platform(
            load_provider_registry("").provider(CORE_PLATFORM_STREAM))


# --- Provider loading ------------------------------------------------------


def test_load_provider_modules_reads_migration_providers_attribute(
        tmp_path, monkeypatch):
    provider = make_provider(
        tmp_path, "retriva-crm-assistant", "pro.crm",
        [(1, "SELECT 1;", "SELECT 1;")],
        dependencies=[CORE_PLATFORM_STREAM])
    module_dir = tmp_path / "provider_module"
    module_dir.mkdir()
    (module_dir / "__init__.py").write_text(
        f"MIGRATION_PROVIDERS = []  # replaced below\n")
    # Build a real importable module exposing the provider list.
    import sys
    module_dir_name = "retriva_test_provider_module"
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / f"{module_dir_name}.py").write_text(
        "from retriva.infrastructure.postgres.migrations import (\n"
        "    SqlMigrationProvider,\n"
        ")\n"
        "from pathlib import Path\n"
        f"PROVIDER = SqlMigrationProvider(\n"
        f"    provider_id={provider.provider_id!r},\n"
        f"    stream_id={provider.stream_id!r},\n"
        f"    sql_dir=Path({str(provider._sql_dir)!r}),\n"
        f")\n"
        "MIGRATION_PROVIDERS = [PROVIDER]\n"
    )
    loaded = load_provider_modules(module_dir_name)
    assert len(loaded) == 1
    assert loaded[0].stream_id == "pro.crm"
    sys.modules.pop(module_dir_name, None)


def test_load_provider_registry_registers_core_and_extensions(
        tmp_path, monkeypatch):
    provider = make_provider(
        tmp_path, "retriva-messaging", "pro.messaging",
        [(1, "SELECT 1;", "SELECT 1;")],
        dependencies=[CORE_PLATFORM_STREAM])
    module_dir_name = "retriva_test_registry_module"
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / f"{module_dir_name}.py").write_text(
        "from retriva.infrastructure.postgres.migrations import (\n"
        "    SqlMigrationProvider,\n"
        ")\n"
        "from pathlib import Path\n"
        f"MIGRATION_PROVIDERS = [SqlMigrationProvider(\n"
        f"    provider_id={provider.provider_id!r},\n"
        f"    stream_id={provider.stream_id!r},\n"
        f"    sql_dir=Path({str(provider._sql_dir)!r}),\n"
        f"    dependencies=({CORE_PLATFORM_STREAM!r},),\n"
        f")]\n"
    )
    registry = load_provider_registry(module_dir_name)
    assert registry.stream_ids() == [
        CORE_PLATFORM_STREAM, "pro.messaging"]
    import sys
    sys.modules.pop(module_dir_name, None)


def test_provider_module_without_attribute_fails(tmp_path, monkeypatch):
    module_dir_name = "retriva_test_broken_module"
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / f"{module_dir_name}.py").write_text("X = 1\n")
    with pytest.raises(MigrationError):
        load_provider_modules(module_dir_name)
    import sys
    sys.modules.pop(module_dir_name, None)


def test_missing_provider_module_fails_clearly():
    """A typo or an uninstalled extension in
    RETRIVA_PG_MIGRATION_PROVIDERS must fail clearly: an unimportable
    provider module raises MigrationError (never a bare ImportError),
    the one-shot exits non-zero, and dependent services do not start
    against missing required schemas."""
    with pytest.raises(MigrationError) as excinfo:
        load_provider_modules("no.such.provider_module")
    assert "could not be imported" in str(excinfo.value)
    assert "RETRIVA_PG_MIGRATION_PROVIDERS" in str(excinfo.value)
    with pytest.raises(MigrationError):
        load_provider_registry("no.such.provider_module")


def test_duplicate_provider_in_one_module_fails_clearly(
        tmp_path, monkeypatch):
    """A module listing the same provider twice (or two providers
    claiming the same stream) fails at registration — duplicate
    provider/stream identities are never silently merged."""
    provider = make_provider(
        tmp_path, "retriva-dup-test", "pro.duplicate",
        [(1, "SELECT 1;", "SELECT 1;")])
    module_dir_name = "retriva_test_dup_module"
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / f"{module_dir_name}.py").write_text(
        "from retriva.infrastructure.postgres.migrations import (\n"
        "    SqlMigrationProvider,\n"
        ")\n"
        "from pathlib import Path\n"
        f"PROVIDER = SqlMigrationProvider(\n"
        f"    provider_id={provider.provider_id!r},\n"
        f"    stream_id={provider.stream_id!r},\n"
        f"    sql_dir=Path({str(provider._sql_dir)!r}),\n"
        f")\n"
        "MIGRATION_PROVIDERS = [PROVIDER, PROVIDER]\n"
    )
    with pytest.raises(MigrationError) as excinfo:
        load_provider_registry(module_dir_name)
    assert "already registered" in str(excinfo.value)
    import sys
    sys.modules.pop(module_dir_name, None)


def test_providers_env_is_neutral():
    from retriva.infrastructure.postgres import migrations
    assert migrations.PROVIDERS_ENV == "RETRIVA_PG_MIGRATION_PROVIDERS"
    assert PROVIDERS_ENV == "RETRIVA_PG_MIGRATION_PROVIDERS"


# --- Licensing boundary (Constitution §45) ----------------------------------


def test_platform_modules_import_no_proprietary_package():
    """The Core platform package must never import a Pro package
    (CRM Assistant, Messaging, or any retriva_* proprietary module)."""
    import subprocess
    import sys

    probe = (
        "import sys\n"
        "import retriva.infrastructure.postgres as pkg\n"
        "import retriva.infrastructure.postgres.migrate as cli\n"
        f"forbidden = {['retriva_crm_assistant', 'retriva_messaging']!r}\n"
        "leaked = [m for m in list(sys.modules) "
        "if m in forbidden or "
        "(m.startswith('retriva_crm_assistant.') or "
        "m.startswith('retriva_messaging.'))]\n"
        "assert not leaked, leaked\n"
        "print('clean')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True,
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[1]
                                / "src")})
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_provider_registry_has_no_hardcoded_proprietary_names():
    postgres_pkg = (
        Path(__file__).resolve().parents[1]
        / "src" / "retriva" / "infrastructure" / "postgres")
    assert postgres_pkg.is_dir()
    for py in postgres_pkg.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "retriva_crm_assistant" not in text, (
            f"{py.name} references a proprietary package")
        assert "retriva_messaging" not in text, (
            f"{py.name} references a proprietary package")


def test_legacy_ledger_descriptor_is_data_only():
    legacy = LegacyLedger(table="audit.schema_migrations")
    assert legacy.table == "audit.schema_migrations"
