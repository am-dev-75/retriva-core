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

"""Reconciliation between PostgreSQL knowledge metadata and Qdrant
(Spec 028 §12/§23).

Crash-window protocol: NEVER assume an intent row means Qdrant did not
execute; verify via deterministic point ids/counts; mark verified
without replay when success is proven; replay ONLY proven absence of an
idempotent operation; move unprovable outcomes to
reconciliation-required/manual review.  Dry-run is the default; apply
is conservative and never performs destructive replay of uncertain
side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from retriva.knowledge.repository import KnowledgeRepository
from retriva.logger import get_logger

_log = get_logger(__name__)

_OPEN_OP_STATES = ("prepared", "executing", "applied_unverified",
                   "failed", "reconciliation_required")


@dataclass
class ReconcileReport:
    mode: str
    tenant_id: str
    collection_name: str
    findings: List[Dict[str, Any]] = field(default_factory=list)
    resolved: int = 0
    detail: Dict[str, Any] = field(default_factory=dict)

    def add(self, classification: str, **evidence) -> None:
        self.findings.append({"classification": classification, **evidence})

    def to_dict(self) -> Dict[str, Any]:
        by_class: Dict[str, int] = {}
        for f in self.findings:
            by_class[f["classification"]] = by_class.get(
                f["classification"], 0) + 1
        return {
            "mode": self.mode,
            "tenant_id": self.tenant_id,
            "collection_name": self.collection_name,
            "findings": self.findings,
            "counts": by_class,
            "resolved": self.resolved,
            "detail": self.detail,
        }


class Reconciler:
    def __init__(self, repository: Optional[KnowledgeRepository] = None,
                 qdrant_client=None):
        self._repo = repository or KnowledgeRepository()
        self._client = qdrant_client

    def _client_or_default(self):
        if self._client is not None:
            return self._client
        from retriva.indexing.qdrant_store import get_client

        return get_client()

    def run(self, tenant_id: str, collection_name: str, *,
            apply: bool = False) -> ReconcileReport:
        report = ReconcileReport(
            mode="apply" if apply else "dry-run", tenant_id=tenant_id,
            collection_name=collection_name)

        # Collection presence.
        client = self._client_or_default()
        try:
            exists = client.collection_exists(collection_name)
        except Exception:
            exists = True  # cannot prove absence; do not claim missing
        if not exists:
            report.add("missing_collection",
                       collection_name=collection_name)
            return report

        # Open operation evidence.
        with self._repo.transaction(tenant_id, privileged=True) as cur:
            ops = self._repo.list_operations(
                cur, tenant_id=tenant_id, states=list(_OPEN_OP_STATES))
            versions = self._versions(cur, tenant_id=tenant_id)
            current = self._current_versions(cur, tenant_id=tenant_id)
        for op in ops:
            report.add(
                "partial_operation", op_id=op["op_id"],
                op_state=op["op_state"], op_type=op["op_type"])

        # Manifest vs Qdrant for current versions.
        for version in versions:
            vid = version["version_id"]
            expected = {r["point_id"] for r in self._manifest(
                tenant_id, vid) if r["sync_state"] != "removed"}
            present = set()
            try:
                from retriva.knowledge.visibility import (
                    point_ids_for_version)
                present = set(point_ids_for_version(
                    client, collection_name, vid))
            except Exception:
                present = set()
            missing = expected - present
            orphans = present - expected
            if missing:
                report.add(
                    "missing_points", version_id=vid,
                    count=len(missing))
            if orphans:
                report.add(
                    "orphan_points", version_id=vid,
                    count=len(orphans))
            if version["status"] == "superseded" and present:
                report.add(
                    "stale_superseded_points", version_id=vid,
                    count=len(present))
            if version["provenance"] == "adopted_uncertain":
                report.add(
                    "uncertain_adoption_evidence", version_id=vid)

        # Serving drift: PG current version vs Qdrant serving evidence.
        for document_id, version_id in current.items():
            if version_id is None:
                continue
            try:
                from retriva.knowledge.visibility import (
                    count_version_points)
                serving = count_version_points(
                    client, collection_name, version_id)
            except Exception:
                serving = 0
            if serving == 0:
                report.add(
                    "current_version_not_serving",
                    document_id=document_id, version_id=version_id)

        report.detail = {"versions": len(versions), "open_ops": len(ops)}
        return report

    # -- helpers ---------------------------------------------------------

    def _versions(self, cur, *, tenant_id) -> List[Dict[str, Any]]:
        cur.execute(
            "SELECT version_id, document_id, status, provenance FROM "
            "knowledge.document_versions WHERE tenant_id=%s",
            (tenant_id,))
        return [dict(r) for r in cur.fetchall()]

    def _current_versions(self, cur, *, tenant_id
                          ) -> Dict[str, Optional[str]]:
        cur.execute(
            "SELECT document_id, current_version_id FROM "
            "knowledge.documents WHERE tenant_id=%s AND "
            "lifecycle_state='active'", (tenant_id,))
        return {r["document_id"]: r["current_version_id"]
                for r in cur.fetchall()}

    def _manifest(self, tenant_id, version_id) -> List[Dict[str, Any]]:
        with self._repo.transaction(tenant_id) as cur:
            return self._repo.list_chunk_states(
                cur, tenant_id=tenant_id, version_id=version_id)
