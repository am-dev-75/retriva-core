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

"""Canonical shared-PostgreSQL configuration for Retriva.

One settings object describes the shared PostgreSQL endpoint and the
platform identities (admin, migrator, Core runtime).  Extension
runtime credentials stay extension-specific (for example the CRM
``CRM_PG_APPLICATION_*`` names) and are never read here.

Credentials come from environment variables (``RETRIVA_PG_*``) or
password files (``RETRIVA_PG_*_PASSWORD_FILE``; Docker secrets or a
mounted secret file) — never from code.  Passwords are held as
``SecretStr`` so accidental rendering of the settings object cannot
leak them.  No credential ever appears in logs, readiness output, or
error messages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from retriva.infrastructure.postgres.errors import (
    PostgresNotConfiguredError,
)

#: Platform role names provisioned by the Core bootstrap step.  The
#: application runtime role never owns schemas and cannot run DDL;
#: the migrator role owns schema changes.
ROLE_MIGRATOR = "retriva_migrator"
ROLE_CORE = "retriva_core"

#: Roles this platform settings object can build connections for.
#: (Extensions keep their own runtime identities in their own
#: settings objects.)
PLATFORM_ROLES = ("admin", "migrator", "core")

_MAX_PASSWORD_FILE_BYTES = 4096


def _read_secret_file(path: str) -> str:
    """Read a password file (Docker secret or mounted file),
    trimmed.  Raises instead of silently continuing with a wrong
    credential when the file is missing, empty, or suspiciously
    large."""
    p = Path(path)
    if not p.is_file():
        raise PostgresNotConfiguredError(
            f"password file not found: {path}")
    size = p.stat().st_size
    if size > _MAX_PASSWORD_FILE_BYTES:
        raise PostgresNotConfiguredError(
            f"password file too large ({size} bytes): {path}")
    value = p.read_text(encoding="utf-8").strip()
    if not value:
        raise PostgresNotConfiguredError(
            f"password file is empty: {path}")
    return value


class PostgresPlatformSettings(BaseSettings):
    """Shared PostgreSQL platform settings (``RETRIVA_PG_*``)."""

    model_config = SettingsConfigDict(
        env_prefix="RETRIVA_PG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- Connection endpoint -------------------------------------------
    host: str = "retriva-postgres"
    port: int = 5432
    database: str = "retriva"
    sslmode: str = "prefer"

    # --- Platform identities -------------------------------------------
    admin_user: str = "retriva_admin"
    migrator_user: str = ROLE_MIGRATOR
    core_user: str = ROLE_CORE

    # --- Credentials (env value or *_FILE secret indirection) ----------
    admin_password: SecretStr = SecretStr("")
    admin_password_file: str = ""
    migrator_password: SecretStr = SecretStr("")
    migrator_password_file: str = ""
    core_password: SecretStr = SecretStr("")
    core_password_file: str = ""

    # --- Pool bounds (synchronous psycopg2 stack) ----------------------
    pool_min_size: int = 1
    pool_max_size: int = 8
    #: Seconds to wait for a free pooled connection before failing.
    pool_acquire_timeout_seconds: float = 15.0
    connect_timeout_seconds: float = 10.0
    #: Server-side statement timeout per connection (transaction bound).
    statement_timeout_ms: int = 30_000
    #: Server-side idle-in-transaction timeout per connection.
    idle_in_transaction_timeout_ms: int = 60_000
    health_check_timeout_seconds: float = 5.0

    # -- Resolution -------------------------------------------------------

    def resolved_password(self, role: str) -> str:
        """Plaintext password for a platform role, applying
        ``*_FILE`` indirection.  Raises ``PostgresNotConfiguredError``
        when neither an env value nor a file is configured."""
        file_attr = f"{role}_password_file"
        value_attr = f"{role}_password"
        file_path: str = getattr(self, file_attr)
        if file_path:
            return _read_secret_file(file_path)
        secret: SecretStr = getattr(self, value_attr)
        plain = secret.get_secret_value() if secret is not None else ""
        if not plain:
            raise PostgresNotConfiguredError(
                f"no password configured for PostgreSQL role "
                f"'{getattr(self, role + '_user', role)}' "
                f"(set RETRIVA_PG_{role.upper()}_PASSWORD or "
                f"RETRIVA_PG_{role.upper()}_PASSWORD_FILE)")
        return plain

    def has_password(self, role: str) -> bool:
        """True when a password (env or file) is configured for a
        platform role."""
        file_attr = f"{role}_password_file"
        if getattr(self, file_attr):
            return Path(getattr(self, file_attr)).is_file()
        secret: SecretStr = getattr(self, f"{role}_password")
        return bool(secret and secret.get_secret_value())

    def connection_kwargs(self, role: str) -> Dict:
        """``psycopg2.connect()`` keyword arguments for a platform
        role.  Never logs; the returned dict contains the plaintext
        password by necessity and must not be printed."""
        user = getattr(self, f"{role}_user")
        options = (
            f"-c statement_timeout={self.statement_timeout_ms} "
            f"-c idle_in_transaction_session_timeout="
            f"{self.idle_in_transaction_timeout_ms}"
        )
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.database,
            "user": user,
            "password": self.resolved_password(role),
            "sslmode": self.sslmode,
            "connect_timeout": int(self.connect_timeout_seconds),
            "options": options,
            "application_name": f"retriva_platform_{role}",
        }


_platform_settings: Optional[PostgresPlatformSettings] = None


def get_platform_settings() -> PostgresPlatformSettings:
    """Process-wide platform settings singleton."""
    global _platform_settings
    if _platform_settings is None:
        _platform_settings = PostgresPlatformSettings()
    return _platform_settings


def reset_platform_settings() -> None:
    """Reset the singleton (test seam)."""
    global _platform_settings
    _platform_settings = None
