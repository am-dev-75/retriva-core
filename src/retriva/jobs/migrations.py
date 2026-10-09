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

"""Core migration provider for the ``core.jobs`` stream (Spec 025;
ADR-030 Decision 3).

Registered by the Core migration CLI next to ``core.platform``
(Core→Core import only; no Pro import; no Compose change — the
existing core one-shot applies this stream through the extended
registration list).  Depends on ``core.platform`` (the ledger).
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from retriva.infrastructure.postgres.migrations import (
    CORE_PROVIDER_ID,
    SqlMigrationProvider,
)

#: Provider and stream identity (namespace rule: ``core.*`` is
#: reserved to the Core provider; ``retriva.jobs`` is Core-owned).
JOBS_PROVIDER_ID = CORE_PROVIDER_ID
JOBS_STREAM_ID = "core.jobs"

#: Directory with the ``core.jobs`` stream SQL files.
JOBS_SQL_DIR = Path(__file__).resolve().parent / "sql"


def jobs_provider() -> SqlMigrationProvider:
    """The Core durable-jobs provider: the ``core.jobs`` stream
    (schema adoption, job tables, RLS, grants, append-only events,
    and the Spec 036 / ADR-041 RLS-safe monitoring aggregate
    interface)."""
    return SqlMigrationProvider(
        provider_id=JOBS_PROVIDER_ID,
        stream_id=JOBS_STREAM_ID,
        sql_dir=JOBS_SQL_DIR,
        dependencies=("core.platform",),
        required_roles=("retriva_migrator", "retriva_core",
                        "retriva_monitor_owner"),
    )


#: Module-level provider registration consumed by the Core CLI's
#: Core-stream registration list (Core→Core import only).
MIGRATION_PROVIDERS: List[SqlMigrationProvider] = [jobs_provider()]
