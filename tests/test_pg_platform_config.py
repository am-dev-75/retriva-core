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

"""Unit tests for the shared-PostgreSQL platform configuration.

No database required.  Verifies credential hygiene (no secrets in
repr), ``*_FILE`` secret indirection, connection construction, and
fail-closed missing-credential errors.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from retriva.infrastructure.postgres.config import (
    PLATFORM_ROLES,
    PostgresPlatformSettings,
)
from retriva.infrastructure.postgres.errors import (
    PostgresNotConfiguredError,
)


def _settings(**kwargs) -> PostgresPlatformSettings:
    base = dict(
        host="retriva-postgres",
        port=5432,
        database="retriva",
        admin_password=SecretStr("admin-pw"),
        migrator_password=SecretStr("migrator-pw"),
        core_password=SecretStr("core-pw"),
    )
    base.update(kwargs)
    return PostgresPlatformSettings(**base)


def test_defaults_are_neutral_and_platform_scoped():
    settings = PostgresPlatformSettings()
    assert settings.host == "retriva-postgres"
    assert settings.port == 5432
    assert settings.database == "retriva"
    assert settings.sslmode == "prefer"
    assert settings.admin_user == "retriva_admin"
    assert settings.migrator_user == "retriva_migrator"
    assert settings.core_user == "retriva_core"
    assert set(PLATFORM_ROLES) == {"admin", "migrator", "core"}


def test_env_prefix_is_retriva_pg(monkeypatch):
    monkeypatch.setenv("RETRIVA_PG_HOST", "pg.internal")
    monkeypatch.setenv("RETRIVA_PG_PORT", "6543")
    monkeypatch.setenv("RETRIVA_PG_DATABASE", "retriva")
    monkeypatch.setenv("RETRIVA_PG_SSLMODE", "disable")
    monkeypatch.setenv("RETRIVA_PG_ADMIN_USER", "root-admin")
    monkeypatch.setenv("RETRIVA_PG_MIGRATOR_USER", "alt_migrator")
    monkeypatch.setenv("RETRIVA_PG_CORE_USER", "alt_core")
    settings = PostgresPlatformSettings()
    assert settings.host == "pg.internal"
    assert settings.port == 6543
    assert settings.database == "retriva"
    assert settings.sslmode == "disable"
    assert settings.admin_user == "root-admin"
    assert settings.migrator_user == "alt_migrator"
    assert settings.core_user == "alt_core"


def test_env_does_not_accept_crm_pg_names(monkeypatch):
    """Generic platform code must not treat CRM_PG_* as canonical:
    the CRM-prefixed names are compatibility aliases handled by the
    extension's settings object, never by this one."""
    monkeypatch.setenv("CRM_PG_HOST", "crm-only-host")
    monkeypatch.setenv("CRM_PG_DATABASE", "crm_db")
    settings = PostgresPlatformSettings()
    assert settings.host == "retriva-postgres"
    assert settings.database == "retriva"


def test_no_secret_leaks_in_repr_or_str():
    settings = _settings()
    dumped = (
        repr(settings) + str(settings)
        + str(settings.model_dump())
        + str(settings.model_dump(mode="json"))
    )
    for secret in ("admin-pw", "migrator-pw", "core-pw"):
        assert secret not in dumped


def test_password_file_indirection(tmp_path):
    secret_file = tmp_path / "core_password"
    secret_file.write_text("file-secret-1\n", encoding="utf-8")
    settings = _settings(
        core_password=SecretStr(""),
        core_password_file=str(secret_file))
    assert settings.resolved_password("core") == "file-secret-1"
    assert settings.has_password("core")


def test_password_file_errors(tmp_path):
    # Missing file
    settings = _settings(
        core_password_file="/nonexistent/secret")
    with pytest.raises(PostgresNotConfiguredError):
        settings.resolved_password("core")
    # Empty file
    empty = tmp_path / "empty"
    empty.write_text("   \n", encoding="utf-8")
    settings = _settings(core_password=SecretStr(""),
                         core_password_file=str(empty))
    with pytest.raises(PostgresNotConfiguredError):
        settings.resolved_password("core")


def test_missing_password_fails_closed_with_actionable_error():
    settings = PostgresPlatformSettings(
        host="h", port=5432, database="retriva")
    with pytest.raises(PostgresNotConfiguredError) as excinfo:
        settings.resolved_password("migrator")
    message = str(excinfo.value)
    assert "RETRIVA_PG_MIGRATOR_PASSWORD" in message
    assert "RETRIVA_PG_MIGRATOR_PASSWORD_FILE" in message
    assert "password" in message.lower()


def test_connection_kwargs_shape():
    settings = _settings()
    kwargs = settings.connection_kwargs("migrator")
    assert kwargs["host"] == "retriva-postgres"
    assert kwargs["port"] == 5432
    assert kwargs["dbname"] == "retriva"
    assert kwargs["user"] == "retriva_migrator"
    assert kwargs["password"] == "migrator-pw"
    assert kwargs["sslmode"] == "prefer"
    assert "statement_timeout" in kwargs["options"]
    assert kwargs["application_name"] == "retriva_platform_migrator"
    # The admin identity is never the migrator identity and vice versa.
    assert settings.connection_kwargs("admin")["user"] == "retriva_admin"
    assert settings.connection_kwargs("core")["user"] == "retriva_core"
    with pytest.raises(AttributeError):
        settings.connection_kwargs("application")
