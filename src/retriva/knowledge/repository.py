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

"""Parameterized repository for the ``knowledge`` schema (Spec 028).

- All SQL is parameterized; identifiers are package constants.
- Tenant context is set with ``SET LOCAL app.current_tenant`` and
  fails closed when unset (RLS never matches an unset GUC).
- The privileged maintenance context sets
  ``app.knowledge_privileged = 'granted'`` on a migrator connection
  (operator-only: adoption apply, reconcile apply, purge, authority
  cutover, KB registry cutover).  Administrative operations never
  fall back silently to the runtime role.
- Qdrant calls never happen inside these transactions; callers record
  operation evidence before/after Qdrant mutation (architecture §12).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Sequence

from retriva.infrastructure.postgres.config import (
    PostgresPlatformSettings,
    get_platform_settings,
)
from retriva.infrastructure.postgres.tenant import (
    set_tenant_context,
    validate_tenant_id,
)
from retriva.knowledge.domain import (
    IngestionSyncState,
    OpState,
    VersionStatus,
    assert_ingestion_transition,
    assert_version_transition,
)
from retriva.logger import get_logger

_log = get_logger(__name__)

_PRIVILEGED_GUC = "app.knowledge_privileged"


class KnowledgeRepositoryError(RuntimeError):
    """Repository-level failure (sanitized)."""


def connect(settings: PostgresPlatformSettings, role: str):
    import psycopg2

    return psycopg2.connect(**settings.connection_kwargs(role))


class KnowledgeRepository:
    """Tenant-scoped and privileged access to the knowledge schema."""

    def __init__(self, settings: Optional[PostgresPlatformSettings] = None):
        self._settings = settings or get_platform_settings()

    # -- connection / transaction helpers --------------------------------

    def _connect(self, *, privileged: bool):
        return connect(
            self._settings, "migrator" if privileged else "core")

    @contextmanager
    def transaction(self, tenant_id: Optional[str] = None, *,
                    privileged: bool = False):
        """One transaction.  ``tenant_id`` is required for tenant-scoped
        work; privileged administrative work may omit it and sets the
        privileged GUC (global operations)."""
        import psycopg2
        from psycopg2.extras import RealDictCursor

        if tenant_id is not None:
            validate_tenant_id(tenant_id)
        conn = self._connect(privileged=privileged)
        try:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    if tenant_id is not None:
                        set_tenant_context(cur, tenant_id)
                    if privileged:
                        cur.execute(
                            "SELECT set_config(%s, 'granted', true)",
                            (_PRIVILEGED_GUC,))
                    yield cur
        except psycopg2.Error as exc:
            raise KnowledgeRepositoryError(
                f"knowledge transaction failed ({exc.__class__.__name__})"
            ) from exc
        finally:
            conn.close()

    # -- sources ---------------------------------------------------------

    def resolve_or_create_source(self, cur, *, tenant_id: str,
                                 identity, source_id: Optional[str] = None
                                 ) -> Dict[str, Any]:
        cur.execute(
            "SELECT source_id, provenance, lifecycle_state FROM "
            "knowledge.sources WHERE tenant_id=%s AND namespace=%s "
            "AND normalized_ref=%s",
            (tenant_id, identity.namespace, identity.normalized_ref))
        row = cur.fetchone()
        if row is not None:
            return dict(row)
        from retriva.knowledge.ids import new_id

        source_id = source_id or new_id()
        cur.execute(
            "INSERT INTO knowledge.sources (source_id, tenant_id, "
            "source_type, namespace, normalized_ref, display_name, "
            "external_ref, connector_provider, provenance, "
            "safe_metadata) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (tenant_id, namespace, normalized_ref) "
            "DO NOTHING RETURNING source_id",
            (source_id, tenant_id, identity.source_type,
             identity.namespace, identity.normalized_ref,
             identity.display_name, identity.external_ref,
             identity.connector_provider,
             getattr(identity, "provenance", "native"),
             "{}"))
        inserted = cur.fetchone()
        if inserted is not None:
            return {"source_id": inserted["source_id"],
                    "provenance": getattr(identity, "provenance",
                                          "native"),
                    "lifecycle_state": "active"}
        # Concurrent insert won the race.
        cur.execute(
            "SELECT source_id, provenance, lifecycle_state FROM "
            "knowledge.sources WHERE tenant_id=%s AND namespace=%s "
            "AND normalized_ref=%s",
            (tenant_id, identity.namespace, identity.normalized_ref))
        return dict(cur.fetchone())

    # -- documents -------------------------------------------------------

    def get_document(self, cur, *, tenant_id: str, document_id: str
                     ) -> Optional[Dict[str, Any]]:
        cur.execute(
            "SELECT * FROM knowledge.documents WHERE tenant_id=%s "
            "AND document_id=%s", (tenant_id, document_id))
        row = cur.fetchone()
        return dict(row) if row else None

    def resolve_or_create_document(
            self, cur, *, tenant_id: str, source_id: str,
            document_id: Optional[str] = None,
            title: Optional[str] = None,
            user_metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        if document_id is not None:
            cur.execute(
                "SELECT * FROM knowledge.documents WHERE tenant_id=%s "
                "AND document_id=%s", (tenant_id, document_id))
            row = cur.fetchone()
            if row is not None:
                return dict(row)
        cur.execute(
            "SELECT * FROM knowledge.documents WHERE tenant_id=%s "
            "AND source_id=%s", (tenant_id, source_id))
        row = cur.fetchone()
        if row is not None:
            return dict(row)
        from retriva.knowledge.ids import new_id

        document_id = document_id or new_id()
        import json

        cur.execute(
            "INSERT INTO knowledge.documents (document_id, tenant_id, "
            "source_id, title, user_metadata) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (document_id) DO NOTHING RETURNING *",
            (document_id, tenant_id, source_id, title,
             json.dumps(user_metadata or {})))
        row = cur.fetchone()
        if row is not None:
            return dict(row)
        cur.execute(
            "SELECT * FROM knowledge.documents WHERE document_id=%s",
            (document_id,))
        return dict(cur.fetchone())

    def set_document_user_metadata(self, cur, *, tenant_id: str,
                                   document_id: str,
                                   user_metadata: Dict[str, Any]) -> None:
        import json

        cur.execute(
            "UPDATE knowledge.documents SET user_metadata=%s, "
            "updated_at=now() WHERE tenant_id=%s AND document_id=%s",
            (json.dumps(user_metadata), tenant_id, document_id))

    def set_document_lifecycle(self, cur, *, tenant_id: str,
                               document_id: str, state: str,
                               purge_after=None) -> None:
        cur.execute(
            "UPDATE knowledge.documents SET lifecycle_state=%s, "
            "deleted_at=CASE WHEN %s='deleted' THEN now() "
            "ELSE deleted_at END, purge_after=%s, updated_at=now() "
            "WHERE tenant_id=%s AND document_id=%s",
            (state, state, purge_after, tenant_id, document_id))

    # -- versions --------------------------------------------------------

    def find_version_by_identity(
            self, cur, *, tenant_id: str, document_id: str,
            content_fingerprint: str, parser_contract_version: str,
            embedding_contract_version: str
    ) -> Optional[Dict[str, Any]]:
        cur.execute(
            "SELECT * FROM knowledge.document_versions WHERE "
            "tenant_id=%s AND document_id=%s AND content_fingerprint=%s "
            "AND parser_contract_version=%s AND "
            "embedding_contract_version=%s",
            (tenant_id, document_id, content_fingerprint,
             parser_contract_version, embedding_contract_version))
        row = cur.fetchone()
        return dict(row) if row else None

    def get_version(self, cur, *, tenant_id: str, version_id: str
                    ) -> Optional[Dict[str, Any]]:
        cur.execute(
            "SELECT * FROM knowledge.document_versions WHERE "
            "tenant_id=%s AND version_id=%s", (tenant_id, version_id))
        row = cur.fetchone()
        return dict(row) if row else None

    def find_adopted_version(self, cur, *, tenant_id: str,
                             document_id: str,
                             chunk_id_seed: str
                             ) -> Optional[Dict[str, Any]]:
        cur.execute(
            "SELECT * FROM knowledge.document_versions WHERE "
            "tenant_id=%s AND document_id=%s AND chunk_id_seed=%s",
            (tenant_id, document_id, chunk_id_seed))
        row = cur.fetchone()
        return dict(row) if row else None

    def create_version(self, cur, *, tenant_id: str, document_id: str,
                       content_fingerprint: Optional[str],
                       parser_contract_version: str,
                       embedding_contract_version: str,
                       chunk_id_seed: str,
                       chunk_contract_version: str,
                       media_type: Optional[str] = None,
                       content_size: Optional[int] = None,
                       storage_ref: Optional[str] = None,
                       source_revision: Optional[str] = None,
                       provenance: str = "native",
                       version_id: Optional[str] = None
                       ) -> Dict[str, Any]:
        from retriva.knowledge.ids import new_id

        version_id = version_id or new_id()
        cur.execute(
            "INSERT INTO knowledge.document_versions (version_id, "
            "tenant_id, document_id, content_fingerprint, "
            "parser_contract_version, embedding_contract_version, "
            "media_type, content_size, storage_ref, source_revision, "
            "chunk_id_seed, chunk_contract_version, provenance) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING RETURNING *",
            (version_id, tenant_id, document_id, content_fingerprint,
             parser_contract_version, embedding_contract_version,
             media_type, content_size, storage_ref, source_revision,
             chunk_id_seed, chunk_contract_version, provenance))
        row = cur.fetchone()
        if row is not None:
            return dict(row)
        if content_fingerprint is not None:
            existing = self.find_version_by_identity(
                cur, tenant_id=tenant_id, document_id=document_id,
                content_fingerprint=content_fingerprint,
                parser_contract_version=parser_contract_version,
                embedding_contract_version=embedding_contract_version)
            if existing is not None:
                return existing
        cur.execute(
            "SELECT * FROM knowledge.document_versions WHERE "
            "version_id=%s", (version_id,))
        return dict(cur.fetchone())

    def set_version_status(self, cur, *, tenant_id: str,
                           version_id: str, status: str) -> int:
        current = self.get_version(
            cur, tenant_id=tenant_id, version_id=version_id)
        if current is None:
            raise KnowledgeRepositoryError("version not found")
        assert_version_transition(current["status"], status)
        cur.execute(
            "UPDATE knowledge.document_versions SET status=%s "
            "WHERE tenant_id=%s AND version_id=%s AND status=%s",
            (status, tenant_id, version_id, current["status"]))
        return cur.rowcount

    # -- memberships -----------------------------------------------------

    def replace_memberships(self, cur, *, tenant_id: str,
                            document_id: str, kb_ids: Sequence[str],
                            collection_name: str) -> None:
        cur.execute(
            "DELETE FROM knowledge.kb_memberships WHERE tenant_id=%s "
            "AND document_id=%s AND collection_name=%s",
            (tenant_id, document_id, collection_name))
        for kb_id in sorted({k for k in kb_ids if k}):
            cur.execute(
                "INSERT INTO knowledge.kb_memberships (tenant_id, "
                "document_id, kb_id, collection_name) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (tenant_id, document_id, kb_id, collection_name))

    def list_memberships(self, cur, *, tenant_id: str,
                         document_id: str) -> List[str]:
        cur.execute(
            "SELECT kb_id FROM knowledge.kb_memberships WHERE "
            "tenant_id=%s AND document_id=%s ORDER BY kb_id",
            (tenant_id, document_id))
        return [r["kb_id"] for r in cur.fetchall()]

    # -- ingestions ------------------------------------------------------

    def create_ingestion(self, cur, *, tenant_id: str, job_id: str,
                         job_type: str, document_id: str,
                         collection_name: str, kb_ids: Sequence[str],
                         target_version_id: Optional[str] = None,
                         ingestion_mode: str = "create",
                         sync_state: str = "registered",
                         expected_chunk_count: Optional[int] = None,
                         attempt_id: Optional[str] = None,
                         ingestion_id: Optional[str] = None
                         ) -> Dict[str, Any]:
        from retriva.knowledge.ids import new_id
        import json

        ingestion_id = ingestion_id or new_id()
        cur.execute(
            "INSERT INTO knowledge.ingestions (ingestion_id, tenant_id, "
            "job_id, attempt_id, job_type, document_id, "
            "target_version_id, collection_name, kb_ids, "
            "ingestion_mode, sync_state, expected_chunk_count) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (ingestion_id, tenant_id, job_id, attempt_id, job_type,
             document_id, target_version_id, collection_name,
             json.dumps(list(kb_ids)), ingestion_mode, sync_state,
             expected_chunk_count))
        return dict(cur.fetchone())

    def set_ingestion_state(self, cur, *, tenant_id: str,
                            ingestion_id: str, sync_state: str,
                            observed_chunk_count: Optional[int] = None,
                            error_code: Optional[str] = None,
                            error_summary: Optional[str] = None
                            ) -> int:
        cur.execute(
            "SELECT sync_state FROM knowledge.ingestions WHERE "
            "tenant_id=%s AND ingestion_id=%s",
            (tenant_id, ingestion_id))
        row = cur.fetchone()
        if row is None:
            raise KnowledgeRepositoryError("ingestion not found")
        assert_ingestion_transition(row["sync_state"], sync_state)
        cur.execute(
            "UPDATE knowledge.ingestions SET sync_state=%s, "
            "observed_chunk_count=COALESCE(%s, observed_chunk_count), "
            "error_code=COALESCE(%s, error_code), "
            "error_summary=COALESCE(%s, error_summary), "
            "completed_at=CASE WHEN %s IN ('indexed','failed','deleted') "
            "THEN now() ELSE completed_at END "
            "WHERE tenant_id=%s AND ingestion_id=%s AND sync_state=%s",
            (sync_state, observed_chunk_count, error_code, error_summary,
             sync_state, tenant_id, ingestion_id, row["sync_state"]))
        return cur.rowcount

    def get_ingestion(self, cur, *, tenant_id: str, ingestion_id: str
                      ) -> Optional[Dict[str, Any]]:
        cur.execute(
            "SELECT * FROM knowledge.ingestions WHERE tenant_id=%s "
            "AND ingestion_id=%s", (tenant_id, ingestion_id))
        row = cur.fetchone()
        return dict(row) if row else None

    # -- version chunks (manifest) --------------------------------------

    def insert_version_chunks(
            self, cur, *, tenant_id: str, version_id: str,
            rows: Iterable[Dict[str, Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        values: List[str] = []
        params: List[Any] = []
        for r in rows:
            values.append("(%s,%s,%s,%s,%s,%s,%s,%s)")
            params.extend([
                tenant_id, version_id, int(r["chunk_ordinal"]),
                str(r["point_id"]), r.get("chunk_fingerprint"),
                r.get("byte_count"),
                r.get("chunk_contract_version", "chunk1"),
                r.get("sync_state", "expected")])
        cur.execute(
            "INSERT INTO knowledge.version_chunks (tenant_id, "
            "version_id, chunk_ordinal, point_id, chunk_fingerprint, "
            "byte_count, chunk_contract_version, sync_state) VALUES "
            + ",".join(values) + " ON CONFLICT (version_id, "
            "chunk_ordinal) DO NOTHING",
            params)
        return cur.rowcount

    def set_chunks_sync_state(self, cur, *, tenant_id: str,
                              version_id: str,
                              ordinals: Sequence[int], sync_state: str,
                              op_id: Optional[str] = None) -> int:
        if not ordinals:
            return 0
        cur.execute(
            "UPDATE knowledge.version_chunks SET sync_state=%s, "
            "op_id=COALESCE(%s, op_id) WHERE tenant_id=%s AND "
            "version_id=%s AND chunk_ordinal = ANY(%s)",
            (sync_state, op_id, tenant_id, version_id,
             list(ordinals)))
        return cur.rowcount

    def list_chunk_states(self, cur, *, tenant_id: str, version_id: str
                          ) -> List[Dict[str, Any]]:
        cur.execute(
            "SELECT chunk_ordinal, point_id, sync_state FROM "
            "knowledge.version_chunks WHERE tenant_id=%s AND "
            "version_id=%s ORDER BY chunk_ordinal",
            (tenant_id, version_id))
        return [dict(r) for r in cur.fetchall()]

    def count_chunks_by_state(self, cur, *, tenant_id: str,
                              version_id: str) -> Dict[str, int]:
        cur.execute(
            "SELECT sync_state, count(*) AS n FROM "
            "knowledge.version_chunks WHERE tenant_id=%s AND "
            "version_id=%s GROUP BY sync_state",
            (tenant_id, version_id))
        return {r["sync_state"]: int(r["n"]) for r in cur.fetchall()}

    # -- qdrant operations ----------------------------------------------

    def record_operation(self, cur, *, tenant_id: str, op_type: str,
                         collection_name: str,
                         version_id: Optional[str] = None,
                         document_id: Optional[str] = None,
                         ingestion_id: Optional[str] = None,
                         batch_no: int = 0, batch_count: int = 1,
                         target_summary: Optional[str] = None,
                         expected_count: Optional[int] = None,
                         op_state: str = "prepared",
                         op_id: Optional[str] = None
                         ) -> str:
        from retriva.knowledge.ids import new_id

        op_id = op_id or new_id()
        cur.execute(
            "INSERT INTO knowledge.qdrant_operations (op_id, tenant_id, "
            "ingestion_id, document_id, version_id, op_type, "
            "collection_name, batch_no, batch_count, target_summary, "
            "expected_count, op_state) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING op_id",
            (op_id, tenant_id, ingestion_id, document_id, version_id,
             op_type, collection_name, batch_no, batch_count,
             target_summary, expected_count, op_state))
        return cur.fetchone()["op_id"]

    def update_operation_state(
            self, cur, *, tenant_id: str, op_id: str, op_state: str,
            error_code: Optional[str] = None,
            error_summary: Optional[str] = None) -> int:
        cur.execute(
            "UPDATE knowledge.qdrant_operations SET op_state=%s, "
            "executed_at=CASE WHEN %s IN ('applied_unverified','verified') "
            "AND executed_at IS NULL THEN now() ELSE executed_at END, "
            "verified_at=CASE WHEN %s='verified' THEN now() "
            "ELSE verified_at END, "
            "error_code=COALESCE(%s, error_code), "
            "error_summary=COALESCE(%s, error_summary), "
            "attempt_no=attempt_no + CASE WHEN %s='reconciliation_required' "
            "THEN 1 ELSE 0 END "
            "WHERE tenant_id=%s AND op_id=%s",
            (op_state, op_state, op_state, error_code, error_summary,
             op_state, tenant_id, op_id))
        return cur.rowcount

    def list_operations(self, cur, *, tenant_id: Optional[str] = None,
                        states: Optional[Sequence[str]] = None,
                        limit: int = 500) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if tenant_id is not None:
            clauses.append("tenant_id=%s")
            params.append(tenant_id)
        if states:
            clauses.append("op_state = ANY(%s)")
            params.append(list(states))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 5000)))
        cur.execute(
            f"SELECT * FROM knowledge.qdrant_operations {where} "
            "ORDER BY prepared_at LIMIT %s", params)
        return [dict(r) for r in cur.fetchall()]

    # -- promotion / deletion -------------------------------------------

    def promote_version(self, cur, *, tenant_id: str, document_id: str,
                        new_version_id: str, prior_version_id: Optional[str]
                        ) -> bool:
        """Atomic promotion: set the new current version, flip the old
        to superseded, bump the serving generation.  Guarded so a late
        or duplicate transaction is a no-op."""
        cur.execute(
            "UPDATE knowledge.documents SET current_version_id=%s, "
            "serving_generation=serving_generation + 1, updated_at=now() "
            "WHERE tenant_id=%s AND document_id=%s AND "
            "(current_version_id IS DISTINCT FROM %s)",
            (new_version_id, tenant_id, document_id, new_version_id))
        promoted = cur.rowcount > 0
        cur.execute(
            "UPDATE knowledge.document_versions SET status='indexed', "
            "promoted_at=COALESCE(promoted_at, now()) WHERE tenant_id=%s "
            "AND version_id=%s AND status IN ('staging','parsing',"
            "'embedding','indexing','indexed')",
            (tenant_id, new_version_id))
        if prior_version_id and prior_version_id != new_version_id:
            cur.execute(
                "UPDATE knowledge.document_versions SET "
                "status='superseded', superseded_at=now() WHERE "
                "tenant_id=%s AND version_id=%s AND status='indexed'",
                (tenant_id, prior_version_id))
        return promoted

    # -- inspection ------------------------------------------------------

    def get_authority_row(self, cur) -> Optional[Dict[str, Any]]:
        cur.execute("SELECT * FROM knowledge.authority "
                    "WHERE singleton = TRUE")
        row = cur.fetchone()
        return dict(row) if row else None

    def get_operation_privileged(self, op_id: str
                                 ) -> Optional[Dict[str, Any]]:
        """Durable operation-evidence lookup across tenants (privileged
        operator/authority context only)."""
        with self.transaction(privileged=True) as cur:
            cur.execute(
                "SELECT op_id, op_state, op_type, collection_name, "
                "version_id, target_summary, expected_count, "
                "verified_at FROM knowledge.qdrant_operations WHERE "
                "op_id=%s", (op_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def finalize_operation_evidence(
            self, cur, *, tenant_id: str, op_id: str, summary: str,
            op_state: str, inspected: Optional[int] = None) -> int:
        cur.execute(
            "UPDATE knowledge.qdrant_operations SET target_summary=%s, "
            "expected_count=COALESCE(%s, expected_count), op_state=%s, "
            "executed_at=COALESCE(executed_at, now()), "
            "verified_at=CASE WHEN %s='verified' THEN now() "
            "ELSE verified_at END WHERE tenant_id=%s AND op_id=%s",
            (summary[:1024], inspected, op_state, op_state, tenant_id,
             op_id))
        return cur.rowcount
