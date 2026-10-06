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

"""Core migration provider for the ``core.knowledge`` stream (Spec 028;
ADR-033).

Registered by the Core migration CLI next to ``core.jobs``
(Core->Core import only; no Pro import).  Depends on ``core.platform``
(ledger) and ``core.jobs`` (migration ordering, role readiness,
lifecycle availability -- no cascading FKs into job-history tables;
knowledge records survive job retention).
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from retriva.infrastructure.postgres.migrations import (
    CORE_PROVIDER_ID,
    SqlMigrationProvider,
)

#: Provider and stream identity (namespace rule: ``core.*`` is
#: reserved to the Core provider).
KNOWLEDGE_PROVIDER_ID = CORE_PROVIDER_ID
KNOWLEDGE_STREAM_ID = "core.knowledge"

#: Directory with the ``core.knowledge`` stream SQL files.
KNOWLEDGE_SQL_DIR = Path(__file__).resolve().parent / "sql"


def knowledge_provider() -> SqlMigrationProvider:
    """The Core knowledge provider: the ``core.knowledge`` stream
    (schema, tables, RLS, grants, triggers, guards).

    The destructive-downgrade guard is enforced in the stream's down
    SQL itself (a database-level DO block that refuses when native
    knowledge data exists), so no Python guard is needed here.
    """
    return SqlMigrationProvider(
        provider_id=KNOWLEDGE_PROVIDER_ID,
        stream_id=KNOWLEDGE_STREAM_ID,
        sql_dir=KNOWLEDGE_SQL_DIR,
        dependencies=("core.platform", "core.jobs"),
        required_roles=("retriva_migrator", "retriva_core"),
    )


#: Module-level provider registration consumed by the Core CLI's
#: Core-stream registration list (Core->Core import only).
MIGRATION_PROVIDERS: List[SqlMigrationProvider] = [knowledge_provider()]
