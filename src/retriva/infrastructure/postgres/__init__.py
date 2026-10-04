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

"""Shared PostgreSQL platform for Retriva Core and extensions.

Generic, edition-neutral PostgreSQL infrastructure (Constitution
§20/§45; Spec 024; ADR-029): one instance, one ``retriva`` database,
module-owned schemas, provider-based migration streams, dedicated
roles.  This package contains no CRM or Messaging domain knowledge
and imports no proprietary package.

Submodules:

- :mod:`errors`    — platform error taxonomy (no secrets in messages).
- :mod:`config`    — ``RETRIVA_PG_*`` settings and connection
                     construction.
- :mod:`bootstrap` — idempotent role provisioning.
- :mod:`migrations` — migration-provider contract, registry, ledger,
                     and runner.
- :mod:`migrate_cli` — deployment-time CLI.

Heavy imports (psycopg2) are lazy so importing the package stays
cheap; deterministic unit tests run without a database.
"""

from __future__ import annotations

from retriva.infrastructure.postgres.errors import (
    MigrationError,
    PostgresConnectionError,
    PostgresNotConfiguredError,
    PostgresPlatformError,
)
from retriva.infrastructure.postgres.migrations import (
    LEDGER_SCHEMA,
    LEDGER_TABLE,
    LegacyLedger,
    Migration,
    MigrationProvider,
    ProviderRegistry,
    SqlMigrationProvider,
    load_provider_registry,
)
from retriva.infrastructure.postgres.config import (
    PostgresPlatformSettings,
    get_platform_settings,
    reset_platform_settings,
)

__all__ = [
    "LEDGER_SCHEMA",
    "LEDGER_TABLE",
    "LegacyLedger",
    "Migration",
    "MigrationError",
    "MigrationProvider",
    "PostgresConnectionError",
    "PostgresNotConfiguredError",
    "PostgresPlatformError",
    "PostgresPlatformSettings",
    "ProviderRegistry",
    "SqlMigrationProvider",
    "get_platform_settings",
    "reset_platform_settings",
    "load_provider_registry",
]
