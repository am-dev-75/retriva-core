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
Knowledge Base domain model and registry.

Implements Phase 1 of *SDD — Knowledge Bases: From Mock to First-Class Resource*.

- :class:`KBRecord`        — Pydantic model mirroring the ``knowledge_bases`` row.
- :class:`KBRegistry`      — CRUD over the registry table.
- :func:`slugify`          — name → kb_id derivation (RD-1).
- :class:`KBValidationError`, :class:`KBNotFoundError`, :class:`KBConflictError`,
  :class:`KBImmutableError` — typed exceptions consumed by the API layer.
- :func:`seed_default_kb`  — idempotent seeding of the ``default`` KB.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from retriva.infrastructure.registry_db import RegistryDB, get_registry_db
from retriva.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Canonical id of the default KB. Seeded on first startup. Cannot be deleted.
DEFAULT_KB_ID: str = "default"

#: Allowed shape of ``kb_id`` after slugification or when supplied explicitly.
KB_ID_REGEX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

NAME_MAX_LEN = 128
DESCRIPTION_MAX_LEN = 1024


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class KBError(Exception):
    """Base class for KB-domain errors."""


class KBValidationError(KBError):
    """Raised when an input field fails validation (maps to 422)."""


class KBNotFoundError(KBError):
    """Raised when a KB does not exist (maps to 404)."""


class KBConflictError(KBError):
    """Raised when creating a KB whose ``kb_id`` already exists (maps to 409)."""


class KBImmutableError(KBError):
    """Raised when attempting a forbidden mutation, e.g. deleting ``default``
    (maps to 409)."""


# ---------------------------------------------------------------------------
# Slugification (RD-1)
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    """Derive a ``kb_id`` from a free-form ``name``.

    Rules:
    - Lower-case.
    - Replace any run of non-alphanumeric characters with a single ``-``.
    - Strip leading/trailing ``-`` and ``_``.
    - Truncate to 64 characters.

    The result must still match :data:`KB_ID_REGEX`; if it does not (e.g.
    the input was empty or all punctuation), :class:`KBValidationError`
    is raised so callers can surface a clean error rather than persist a
    surprising id.
    """
    if not isinstance(name, str):
        raise KBValidationError("name must be a string")

    lowered = name.strip().lower()
    # Collapse any non [a-z0-9] run into a single hyphen.
    collapsed = re.sub(r"[^a-z0-9]+", "-", lowered)
    trimmed = collapsed.strip("-_")[:64]

    if not trimmed or not KB_ID_REGEX.match(trimmed):
        raise KBValidationError(
            f"Cannot derive a valid kb_id from name={name!r}. "
            "Provide an explicit kb_id matching ^[a-z0-9][a-z0-9_-]{0,63}$."
        )
    return trimmed


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------

class KBRecord(BaseModel):
    """In-memory representation of a row in ``knowledge_bases``.

    The ``settings`` field is exposed as a parsed ``dict`` to API consumers;
    the on-disk column ``settings_json`` is the serialized form.
    """

    kb_id: str
    name: str
    description: Optional[str] = None
    created_at: str
    updated_at: str
    settings: Dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------------ helpers

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "KBRecord":
        return cls(
            kb_id=row["kb_id"],
            name=row["name"],
            description=row["description"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            settings=json.loads(row["settings_json"] or "{}"),
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_name(name: str) -> str:
    if not isinstance(name, str):
        raise KBValidationError("name must be a string")
    stripped = name.strip()
    if not stripped:
        raise KBValidationError("name must not be empty")
    if len(stripped) > NAME_MAX_LEN:
        raise KBValidationError(f"name exceeds {NAME_MAX_LEN} characters")
    return stripped


def _validate_description(description: Optional[str]) -> Optional[str]:
    if description is None:
        return None
    if not isinstance(description, str):
        raise KBValidationError("description must be a string")
    if len(description) > DESCRIPTION_MAX_LEN:
        raise KBValidationError(
            f"description exceeds {DESCRIPTION_MAX_LEN} characters"
        )
    return description


def _validate_kb_id(kb_id: str) -> str:
    if not isinstance(kb_id, str) or not KB_ID_REGEX.match(kb_id):
        raise KBValidationError(
            f"kb_id={kb_id!r} must match ^[a-z0-9][a-z0-9_-]{{0,63}}$"
        )
    return kb_id


def _validate_settings(settings: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if settings is None:
        return {}
    if not isinstance(settings, dict):
        raise KBValidationError("settings must be a JSON object")
    # Round-trip through json to ensure serializability now, not at write time.
    try:
        json.dumps(settings)
    except (TypeError, ValueError) as exc:
        raise KBValidationError(f"settings is not JSON-serializable: {exc}") from exc
    return settings


class KBRegistry:
    """CRUD over the ``knowledge_bases`` table.

    Thread-safe via the underlying :class:`RegistryDB` write lock. Read paths
    are lock-free (SQLite handles per-connection isolation; WAL allows
    concurrent reads with one writer).
    """

    def __init__(self, db: Optional[RegistryDB] = None):
        self._db = db or get_registry_db()

    # --------------------------------------------------------------------- list

    def list(self) -> List[KBRecord]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT kb_id, name, description, created_at, updated_at, settings_json "
                "FROM knowledge_bases ORDER BY kb_id ASC"
            ).fetchall()
        return [KBRecord.from_row(r) for r in rows]

    # ---------------------------------------------------------------------- get

    def get(self, kb_id: str) -> KBRecord:
        kb_id = _validate_kb_id(kb_id)
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT kb_id, name, description, created_at, updated_at, settings_json "
                "FROM knowledge_bases WHERE kb_id = ?",
                (kb_id,),
            ).fetchone()
        if row is None:
            raise KBNotFoundError(f"KB not found: {kb_id}")
        return KBRecord.from_row(row)

    def exists(self, kb_id: str) -> bool:
        try:
            self.get(kb_id)
            return True
        except KBNotFoundError:
            return False

    # ------------------------------------------------------------------- create

    def create(
        self,
        *,
        name: str,
        kb_id: Optional[str] = None,
        description: Optional[str] = None,
        settings: Optional[Dict[str, Any]] = None,
    ) -> KBRecord:
        """Create a new KB row.

        ``kb_id`` policy (RD-1): if supplied, it is validated verbatim; if
        omitted, it is derived from ``name`` via :func:`slugify`. Collisions
        are surfaced as :class:`KBConflictError` — never silently mutated.
        """
        name = _validate_name(name)
        description = _validate_description(description)
        settings = _validate_settings(settings)

        if kb_id is None:
            kb_id = slugify(name)
        else:
            kb_id = _validate_kb_id(kb_id)

        now = _utcnow_iso()
        record = KBRecord(
            kb_id=kb_id,
            name=name,
            description=description,
            created_at=now,
            updated_at=now,
            settings=settings,
        )

        try:
            with self._db.write() as conn:
                conn.execute(
                    "INSERT INTO knowledge_bases "
                    "(kb_id, name, description, created_at, updated_at, settings_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        record.kb_id,
                        record.name,
                        record.description,
                        record.created_at,
                        record.updated_at,
                        json.dumps(record.settings),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            # Primary key collision is the only realistic IntegrityError here.
            raise KBConflictError(f"KB already exists: {kb_id}") from exc

        logger.info(f"KB created: kb_id={kb_id} name={name!r}")
        return record

    # ------------------------------------------------------------------- update

    def update(
        self,
        kb_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        settings: Optional[Dict[str, Any]] = None,
    ) -> KBRecord:
        """Update mutable fields. ``kb_id`` is immutable.

        Only fields explicitly passed (i.e. ``not None``) are updated.
        Pass ``description=""`` to set an empty description; passing
        ``description=None`` is a no-op.
        """
        kb_id = _validate_kb_id(kb_id)

        # Fetch first to give a precise 404 before doing any write.
        current = self.get(kb_id)

        new_name = current.name if name is None else _validate_name(name)
        new_description = (
            current.description if description is None else _validate_description(description)
        )
        new_settings = (
            current.settings if settings is None else _validate_settings(settings)
        )
        now = _utcnow_iso()

        with self._db.write() as conn:
            conn.execute(
                "UPDATE knowledge_bases "
                "SET name = ?, description = ?, settings_json = ?, updated_at = ? "
                "WHERE kb_id = ?",
                (
                    new_name,
                    new_description,
                    json.dumps(new_settings),
                    now,
                    kb_id,
                ),
            )

        logger.info(f"KB updated: kb_id={kb_id}")
        return KBRecord(
            kb_id=kb_id,
            name=new_name,
            description=new_description,
            created_at=current.created_at,
            updated_at=now,
            settings=new_settings,
        )

    # ------------------------------------------------------------------- delete

    def delete(self, kb_id: str) -> None:
        """Delete a registry row.

        Phase 1 deletes the registry row only. The Qdrant-points and dedup
        cascade are wired in Phase 3 (see SDD). The ``default`` KB cannot
        be deleted (RD-2-adjacent invariant).
        """
        kb_id = _validate_kb_id(kb_id)
        if kb_id == DEFAULT_KB_ID:
            raise KBImmutableError("The 'default' KB cannot be deleted.")

        with self._db.write() as conn:
            cursor = conn.execute(
                "DELETE FROM knowledge_bases WHERE kb_id = ?", (kb_id,)
            )
            if cursor.rowcount == 0:
                raise KBNotFoundError(f"KB not found: {kb_id}")

        logger.info(f"KB deleted: kb_id={kb_id}")


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def seed_default_kb(registry: Optional[KBRegistry] = None) -> KBRecord:
    """Ensure the ``default`` KB exists.

    Idempotent. Safe to call at every startup. Returns the (existing or newly
    created) record.
    """
    registry = registry or KBRegistry()
    try:
        record = registry.get(DEFAULT_KB_ID)
        logger.debug(f"Default KB already present: kb_id={record.kb_id}")
        return record
    except KBNotFoundError:
        pass

    try:
        record = registry.create(
            kb_id=DEFAULT_KB_ID,
            name="default",
            description="Default knowledge base",
            settings={},
        )
        logger.info("Default KB seeded.")
        return record
    except KBConflictError:
        # Race with another worker; re-read.
        return registry.get(DEFAULT_KB_ID)
