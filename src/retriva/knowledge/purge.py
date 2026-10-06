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

"""Privileged, manual, batch-bounded purge of deleted tombstones
(Spec 028 §22; ADR-033 Decision 8).

Eligibility: lifecycle_state='deleted', ``purge_after`` reached, and NO
``adopted_uncertain`` version.  Never age-purges active, pending,
uncertain, reconciliation-required, or adopted_uncertain records.
Dry-run first; no scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from retriva.knowledge.repository import KnowledgeRepository
from retriva.logger import get_logger

_log = get_logger(__name__)

DEFAULT_RETENTION_DAYS = 90


@dataclass
class PurgeReport:
    mode: str
    tenant_id: Optional[str]
    batch: int
    retention_days: int
    eligible: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    purged: int = 0
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "tenant_id": self.tenant_id,
            "eligible": len(self.eligible),
            "skipped": len(self.skipped),
            "purged": self.purged,
            "retention_days": self.retention_days,
            "detail": self.detail,
        }


class Purger:
    def __init__(self, repository: Optional[KnowledgeRepository] = None):
        self._repo = repository or KnowledgeRepository()

    def run(self, tenant_id: Optional[str], *, apply: bool = False,
            batch: int = 100,
            retention_days: int = DEFAULT_RETENTION_DAYS
            ) -> PurgeReport:
        report = PurgeReport(
            mode="apply" if apply else "dry-run", tenant_id=tenant_id,
            batch=batch, retention_days=retention_days)
        with self._repo.transaction(privileged=True) as cur:
            cur.execute(
                "SELECT d.document_id, d.tenant_id, d.purge_after FROM "
                "knowledge.documents d WHERE d.lifecycle_state='deleted' "
                "AND d.purge_after IS NOT NULL AND d.purge_after <= now() "
                "AND (%s IS NULL OR d.tenant_id = %s) "
                "AND NOT EXISTS (SELECT 1 FROM "
                "knowledge.document_versions v WHERE "
                "v.document_id = d.document_id AND "
                "v.provenance = 'adopted_uncertain') "
                "ORDER BY d.purge_after LIMIT %s",
                (tenant_id, tenant_id, max(1, int(batch))))
            candidates = [dict(r) for r in cur.fetchall()]
            report.eligible = candidates
            if not apply:
                report.detail = {"would_purge": len(candidates)}
                return report
            for row in candidates:
                self._purge_document(cur, row)
                report.purged += 1
        report.detail = {"purged": report.purged}
        return report

    def _purge_document(self, cur, row: Dict[str, Any]) -> None:
        doc_id = row["document_id"]
        tid = row["tenant_id"]
        # Order respects FK restrict/cascade: operations -> ingestions ->
        # manifest/versions -> memberships -> document -> orphan sources.
        cur.execute(
            "DELETE FROM knowledge.qdrant_operations WHERE tenant_id=%s "
            "AND document_id=%s", (tid, doc_id))
        cur.execute(
            "DELETE FROM knowledge.ingestions WHERE tenant_id=%s AND "
            "document_id=%s", (tid, doc_id))
        cur.execute(
            "DELETE FROM knowledge.version_chunks WHERE tenant_id=%s AND "
            "version_id IN (SELECT version_id FROM "
            "knowledge.document_versions WHERE document_id=%s)",
            (tid, doc_id))
        cur.execute(
            "DELETE FROM knowledge.document_versions WHERE tenant_id=%s "
            "AND document_id=%s", (tid, doc_id))
        cur.execute(
            "DELETE FROM knowledge.kb_memberships WHERE tenant_id=%s AND "
            "document_id=%s", (tid, doc_id))
        cur.execute(
            "DELETE FROM knowledge.documents WHERE tenant_id=%s AND "
            "document_id=%s", (tid, doc_id))
        # Sources are tombstoned, not cascade-deleted; purge only when no
        # remaining document references the source.
        cur.execute(
            "DELETE FROM knowledge.sources s WHERE s.tenant_id=%s AND "
            "NOT EXISTS (SELECT 1 FROM knowledge.documents d WHERE "
            "d.source_id = s.source_id)", (tid,))
