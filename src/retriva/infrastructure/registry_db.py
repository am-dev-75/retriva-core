# Copyright (C) 2026 Andrea Marson (am.dev.75@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
SQLite-backed registry database for Retriva Core.

Currently hosts a single table — ``knowledge_bases`` — but is designed as a
general-purpose registry file that other lightweight metadata tables may join
later (see SDD RD-3 for the policy on per-subsystem files).

Design notes
------------
- One SQLite file per subsystem. The KB registry lives at
  ``<storage_path>/registry.db``.
- Connection-per-call (mirrors the simplicity of
  :class:`retriva.ingestion.dedup.DeduplicationStore`). SQLite handles
  per-connection synchronization; we add a module-level ``threading.Lock``
  for write paths to keep semantics predictable under uvicorn workers
  sharing a process.
- WAL journaling is enabled for better read/write concurrency.
- Schema creation is idempotent (``CREATE TABLE IF NOT EXISTS``); no migration
  framework is introduced in this iteration.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from retriva.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

KNOWLEDGE_BASES_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_bases (
    kb_id         TEXT PRIMARY KEY NOT NULL,
    name          TEXT NOT NULL,
    description   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    settings_json TEXT NOT NULL DEFAULT '{}'
);
"""


# ---------------------------------------------------------------------------
# RegistryDB
# ---------------------------------------------------------------------------

class RegistryDB:
    """Thin wrapper around a SQLite file holding Retriva Core registry tables.

    Use :meth:`connect` as a context manager to obtain a short-lived
    connection. The class itself holds no open connections.
    """

    _write_lock = threading.Lock()

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            from retriva.config import settings
            storage_path = getattr(settings, "storage_path", "storage")
            db_path = os.path.join(storage_path, "registry.db")

        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

        self._initialize_schema()

    # ------------------------------------------------------------------ I/O

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a SQLite connection with row factory set to ``sqlite3.Row``.

        The connection is closed at the end of the ``with`` block. Callers
        are responsible for committing within the block when writing.
        """
        conn = sqlite3.connect(
            self._path,
            isolation_level=None,  # explicit transaction control
            timeout=30.0,
        )
        try:
            conn.row_factory = sqlite3.Row
            # Pragmas applied per-connection; cheap and idempotent.
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection guarded by the module-level write lock.

        Use for any path that performs INSERT / UPDATE / DELETE. The lock
        serializes writers within a single process; SQLite serializes across
        processes via its own file lock.
        """
        with self._write_lock:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE;")
                try:
                    yield conn
                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

    # ----------------------------------------------------------- Schema mgmt

    def _initialize_schema(self) -> None:
        """Create tables if they do not exist. Idempotent."""
        with self.connect() as conn:
            conn.executescript(KNOWLEDGE_BASES_SCHEMA)
            logger.debug(f"Registry DB schema ensured at {self._path}")


# A module-level singleton is convenient but not enforced. Callers that need
# isolation (e.g. tests) instantiate their own RegistryDB pointing at a
# temporary path.
_default_db: Optional[RegistryDB] = None


def get_registry_db() -> RegistryDB:
    """Return the process-wide default ``RegistryDB`` (lazy)."""
    global _default_db
    if _default_db is None:
        _default_db = RegistryDB()
    return _default_db


def reset_registry_db_for_tests() -> None:
    """Drop the cached singleton. Test-only."""
    global _default_db
    _default_db = None
