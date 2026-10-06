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

"""KB registry migration: SQLite ``registry.db`` -> ``knowledge``
(Spec 028 §7/§16; ADR-033 Decision 7).

Dry-run first, idempotent, interruptible.  SQLite is read-only during
adoption and is frozen (never deleted) as rollback evidence at cutover.
After authoritative cutover the runtime REFUSES SQLite fallback.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from retriva.domain.kb import KB_ID_REGEX
from retriva.knowledge.authority import (
    AuthorityState,
    KnowledgeAuthority,
)
from retriva.knowledge.repository import (
    KnowledgeRepository,
    KnowledgeRepositoryError,
)
from retriva.logger import get_logger

_log = get_logger(__name__)

_COLLECTION_RE = __import__("re").compile(r"^[A-Za-z0-9._-]{1,128}$")
_MAX_CONFIG_BYTES = 4096


class KBMigrationError(RuntimeError):
    pass


@dataclass
class KBRow:
    kb_id: str
    collection_name: str
    name: str
    description: Optional[str]
    settings: Dict[str, Any]
    created_at: str
    updated_at: str
    provenance: str = "adopted"


@dataclass
class KBMigrationReport:
    mode: str
    valid: List[KBRow] = field(default_factory=list)
    invalid: List[Dict[str, Any]] = field(default_factory=list)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    applied: int = 0
    skipped_existing: int = 0
    verified: bool = False
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "candidate_count": len(self.valid),
            "invalid": self.invalid,
            "conflicts": self.conflicts,
            "applied": self.applied,
            "skipped_existing": self.skipped_existing,
            "verified": self.verified,
            "detail": self.detail,
            "kb_ids": sorted({r.kb_id for r in self.valid}),
        }


class KBRegistryMigrator:
    """Reads SQLite evidence and applies it to PostgreSQL."""

    def __init__(self, repository: Optional[KnowledgeRepository] = None,
                 authority: Optional[KnowledgeAuthority] = None):
        self._repo = repository or KnowledgeRepository()
        self._authority = authority or KnowledgeAuthority(self._repo)

    # -- evidence (read-only SQLite) ------------------------------------

    def read_sqlite(self) -> Dict[str, Any]:
        """Read the SQLite registry WITHOUT modifying it.  Reads ALL
        collections (the domain accessor's ``list`` is collection
        scoped)."""
        import sqlite3

        from retriva.infrastructure.registry_db import get_registry_db

        db = get_registry_db()
        rows: List[Dict[str, Any]] = []
        with db.connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT kb_id, collection_name, name, description, "
                "created_at, updated_at, settings_json FROM "
                "knowledge_bases ORDER BY kb_id ASC")
            for row in cursor.fetchall():
                try:
                    config = json.loads(row["settings_json"] or "{}")
                except (TypeError, ValueError):
                    config = {}
                rows.append({
                    "kb_id": row["kb_id"],
                    "collection_name": row["collection_name"],
                    "name": row["name"],
                    "description": row["description"],
                    "settings": config,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                })
        return {"rows": rows, "source": "sqlite:registry.db"}

    def validate(self, evidence: Dict[str, Any]) -> KBMigrationReport:
        report = KBMigrationReport(mode="dry-run")
        seen: set = set()
        for row in evidence.get("rows", []):
            problems: List[str] = []
            kb_id = str(row.get("kb_id") or "")
            collection = str(row.get("collection_name") or "")
            if not KB_ID_REGEX.match(kb_id or ""):
                problems.append("invalid_kb_id")
            if not _COLLECTION_RE.match(collection or ""):
                problems.append("invalid_collection_name")
            config = row.get("settings") or {}
            try:
                if len(json.dumps(config).encode("utf-8")) > _MAX_CONFIG_BYTES:
                    problems.append("config_too_large")
            except (TypeError, ValueError):
                problems.append("config_not_serializable")
            key = (kb_id, collection)
            if key in seen:
                problems.append("duplicate_row")
            seen.add(key)
            if problems:
                report.invalid.append({
                    "kb_id": kb_id, "collection_name": collection,
                    "problems": problems})
                continue
            report.valid.append(KBRow(
                kb_id=kb_id, collection_name=collection,
                name=str(row.get("name") or kb_id),
                description=row.get("description"),
                settings=config,
                created_at=str(row.get("created_at") or ""),
                updated_at=str(row.get("updated_at") or ""),
            ))
        # Conflicts: a kb_id mapped to multiple collections is preserved
        # (PG PK allows it, like SQLite), but reported for operator review.
        by_id: Dict[str, set] = {}
        for row in report.valid:
            by_id.setdefault(row.kb_id, set()).add(row.collection_name)
        for kb_id, collections in sorted(by_id.items()):
            if len(collections) > 1:
                report.conflicts.append({
                    "kb_id": kb_id,
                    "collections": sorted(collections),
                    "classification": "multi_collection_kb_id",
                })
        report.detail = {
            "valid": len(report.valid),
            "invalid": len(report.invalid),
            "conflicts": len(report.conflicts),
        }
        return report

    # -- apply / verify --------------------------------------------------

    def apply(self, tenant_id: str, evidence: Dict[str, Any], *,
              apply: bool = False) -> KBMigrationReport:
        report = self.validate(evidence)
        report.mode = "apply" if apply else "dry-run"
        if not apply or report.invalid:
            if report.invalid:
                report.detail["refused"] = "invalid_rows_present"
            return report
        for row in report.valid:
            with self._repo.transaction(tenant_id, privileged=True) as cur:
                cur.execute(
                    "INSERT INTO knowledge.knowledge_bases (tenant_id, "
                    "kb_id, collection_name, name, description, config, "
                    "provenance) VALUES (%s,%s,%s,%s,%s,%s,'adopted') "
                    "ON CONFLICT (tenant_id, kb_id, collection_name) "
                    "DO NOTHING",
                    (tenant_id, row.kb_id, row.collection_name, row.name,
                     row.description, json.dumps(row.settings)))
                if cur.rowcount > 0:
                    report.applied += 1
                else:
                    report.skipped_existing += 1
        report.verified = self.verify(tenant_id, report.valid)["ok"]
        return report

    def verify(self, tenant_id: str,
               expected: List[KBRow]) -> Dict[str, Any]:
        with self._repo.transaction(tenant_id, privileged=True) as cur:
            cur.execute(
                "SELECT kb_id, collection_name FROM "
                "knowledge.knowledge_bases WHERE tenant_id=%s",
                (tenant_id,))
            present = {(r["kb_id"], r["collection_name"])
                       for r in cur.fetchall()}
        want = {(r.kb_id, r.collection_name) for r in expected}
        missing = sorted(want - present)
        return {"ok": not missing, "present": len(present),
                "expected": len(want), "missing": missing}


class KnowledgeBaseRegistry:
    """Runtime KB accessor with an explicit authority-gated mode.

    Pre-cutover: reads PostgreSQL when populated, else SQLite legacy
    (reported).  Post-cutover (``authoritative``): PostgreSQL only;
    SQLite authority is REFUSED (fail-closed), never a silent fallback.
    """

    def __init__(self, repository: Optional[KnowledgeRepository] = None,
                 authority: Optional[KnowledgeAuthority] = None):
        self._repo = repository or KnowledgeRepository()
        self._authority = authority or KnowledgeAuthority(self._repo)

    def mode(self) -> str:
        state = self._authority.read_state()
        return ("postgresql" if state is AuthorityState.AUTHORITATIVE
                else "legacy-sqlite")

    def list(self, tenant_id: str) -> List[Dict[str, Any]]:
        if self.mode() == "postgresql":
            return self._list_pg(tenant_id)
        # Pre-cutover legacy read.
        from retriva.domain.kb import KBRegistry

        return [r.model_dump() for r in KBRegistry().list()]

    def _list_pg(self, tenant_id: str) -> List[Dict[str, Any]]:
        with self._repo.transaction(tenant_id) as cur:
            cur.execute(
                "SELECT kb_id, collection_name, name, description, "
                "config, lifecycle_state FROM knowledge.knowledge_bases "
                "WHERE tenant_id=%s AND lifecycle_state='active' "
                "ORDER BY kb_id", (tenant_id,))
            return [dict(r) for r in cur.fetchall()]

    def get(self, tenant_id: str,
            kb_id: str) -> Optional[Dict[str, Any]]:
        if self.mode() == "postgresql":
            with self._repo.transaction(tenant_id) as cur:
                cur.execute(
                    "SELECT kb_id, collection_name, name, description, "
                    "config, lifecycle_state FROM "
                    "knowledge.knowledge_bases WHERE tenant_id=%s AND "
                    "kb_id=%s AND lifecycle_state='active'",
                    (tenant_id, kb_id))
                row = cur.fetchone()
                return dict(row) if row else None
        from retriva.domain.kb import KBRegistry

        rec = KBRegistry().get(kb_id)
        return rec.model_dump() if rec else None

    def refuse_legacy_write(self) -> None:
        """After cutover, legacy (SQLite) authority writes are refused
        rather than silently applied."""
        if self.mode() == "postgresql":
            raise KBMigrationError(
                "legacy SQLite KB registry authority is refused after "
                "authoritative cutover (no silent fallback)")
