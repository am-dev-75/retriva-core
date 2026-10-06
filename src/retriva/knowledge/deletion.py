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

"""Asynchronous deletion with tombstones and verified vector removal
(Spec 028 §22).

``active → delete_pending → (async evidenced Qdrant removal) →
zero-vector verified → deleted tombstone → optional privileged purge``.
Idempotent; no hard delete before zero-vector verification;
``adopted_uncertain`` records cannot be automatically deleted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from retriva.knowledge.repository import KnowledgeRepository
from retriva.logger import get_logger

_log = get_logger(__name__)

DEFAULT_RETENTION_DAYS = 90


@dataclass
class DeletionResult:
    document_id: str
    state: str
    removed_points: int = 0
    op_id: Optional[str] = None
    purge_after: Optional[str] = None


class DeletionService:
    def __init__(self, repository: Optional[KnowledgeRepository] = None,
                 qdrant_client=None):
        self._repo = repository or KnowledgeRepository()
        self._client = qdrant_client

    def _client_or_default(self):
        if self._client is not None:
            return self._client
        from retriva.indexing.qdrant_store import get_client

        return get_client()

    def request_deletion(self, tenant_id: str, document_id: str,
                         collection_name: str) -> DeletionResult:
        """Transactional intent: record delete-intent evidence and flip
        the document to delete_pending.  Idempotent."""
        with self._repo.transaction(tenant_id) as cur:
            doc = self._repo.get_document(
                cur, tenant_id=tenant_id, document_id=document_id)
            if doc is None:
                return DeletionResult(document_id, "not_found")
            if doc["lifecycle_state"] == "deleted":
                return DeletionResult(document_id, "deleted")
            if self._has_uncertain(
                    cur, tenant_id=tenant_id, document_id=document_id):
                raise ValueError(
                    "adopted_uncertain documents cannot be automatically "
                    "deleted; explicit operator evidence is required")
            op_id = self._repo.record_operation(
                cur, tenant_id=tenant_id, op_type="delete_document",
                collection_name=collection_name, document_id=document_id,
                target_summary=f"document:{document_id}",
                op_state="prepared")
            self._repo.set_document_lifecycle(
                cur, tenant_id=tenant_id, document_id=document_id,
                state="delete_pending")
        return DeletionResult(document_id, "delete_pending", op_id=op_id)

    def complete_deletion(self, tenant_id: str, document_id: str,
                          collection_name: str, *,
                          op_id: Optional[str] = None) -> DeletionResult:
        """Async worker: execute Qdrant removal (delete-by-document
        filter), verify zero matching vectors, then write the deleted
        tombstone with ``purge_after``."""
        if op_id:
            with self._repo.transaction(tenant_id) as cur:
                self._repo.update_operation_state(
                    cur, tenant_id=tenant_id, op_id=op_id,
                    op_state="executing")
        removed = 0
        try:
            from retriva.knowledge.visibility import (
                document_filter)
            client = self._client_or_default()
            before = client.count(
                collection_name=collection_name,
                count_filter=document_filter(document_id), exact=True)
            before = int(getattr(before, "count", before) or 0)
            if before:
                client.delete(
                    collection_name=collection_name,
                    points_selector=document_filter(document_id),
                    wait=True)
            after = client.count(
                collection_name=collection_name,
                count_filter=document_filter(document_id), exact=True)
            after = int(getattr(after, "count", after) or 0)
            removed = before - after
            if after != 0:
                if op_id:
                    with self._repo.transaction(tenant_id) as cur:
                        self._repo.update_operation_state(
                            cur, tenant_id=tenant_id, op_id=op_id,
                            op_state="reconciliation_required",
                            error_code="vectors_remaining")
                return DeletionResult(
                    document_id, "delete_pending", removed_points=removed,
                    op_id=op_id)
        except Exception as exc:
            _log.warning(
                "deletion Qdrant removal failed: %s",
                exc.__class__.__name__)
            if op_id:
                with self._repo.transaction(tenant_id) as cur:
                    self._repo.update_operation_state(
                        cur, tenant_id=tenant_id, op_id=op_id,
                        op_state="failed", error_code="qdrant_unavailable")
            return DeletionResult(document_id, "delete_pending",
                                  removed_points=removed, op_id=op_id)
        if op_id:
            with self._repo.transaction(tenant_id) as cur:
                self._repo.update_operation_state(
                    cur, tenant_id=tenant_id, op_id=op_id,
                    op_state="verified")
        purge_after = self._tombstone(tenant_id, document_id)
        return DeletionResult(document_id, "deleted",
                              removed_points=removed, op_id=op_id,
                              purge_after=purge_after)

    def _tombstone(self, tenant_id: str, document_id: str) -> Optional[str]:
        purge_after = datetime.now(timezone.utc) + timedelta(
            days=DEFAULT_RETENTION_DAYS)
        with self._repo.transaction(tenant_id) as cur:
            self._repo.set_document_lifecycle(
                cur, tenant_id=tenant_id, document_id=document_id,
                state="deleted", purge_after=purge_after)
            cur.execute(
                "UPDATE knowledge.ingestions SET sync_state='deleted', "
                "completed_at=now() WHERE tenant_id=%s AND "
                "document_id=%s AND sync_state='delete_pending'",
                (tenant_id, document_id))
        return purge_after.isoformat()

    def _has_uncertain(self, cur, *, tenant_id: str,
                       document_id: str) -> bool:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM knowledge.document_versions "
            "WHERE tenant_id=%s AND document_id=%s AND "
            "provenance='adopted_uncertain')", (tenant_id, document_id))
        return bool(cur.fetchone()["exists"])
