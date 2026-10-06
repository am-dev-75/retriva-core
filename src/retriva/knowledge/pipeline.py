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

"""Runtime pipeline integration for the three ingestion workflows
(Spec 028 §5; P4).

Submission hooks (:meth:`KnowledgePipeline.begin_*`) create the
source/document/version/ingestion evidence and return a serializable
:class:`KnowledgeContext` that is stored in the durable job input
metadata.  Execution hooks (:meth:`record_intent`,
:meth:`mark_applied`, :meth:`complete`) build the per-point manifest,
record deterministic Qdrant operation evidence, verify the outcome,
promote the version in PostgreSQL, and activate/deactivate Qdrant
serving visibility — in the accepted order, so the prior current
version stays visible until the new one is proven.

The pipeline is a no-op unless the ``knowledge`` schema is present AND
authority is ``authoritative`` (native ingestion).  When the schema is
absent the legacy runtime path is unchanged; when the schema exists
but authority is not authoritative, native ingestion is rejected
(fail-closed pre-cutover posture).
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from retriva.knowledge.authority import (
    AuthorityError,
    AuthorityState,
    KnowledgeAuthority,
)
from retriva.knowledge.integration import KnowledgeIntegration
from retriva.knowledge.repository import (
    KnowledgeRepository,
    KnowledgeRepositoryError,
)
from retriva.knowledge.visibility import native_payload_fields
from retriva.logger import get_logger

_log = get_logger(__name__)


class KnowledgeIngestionUnavailable(RuntimeError):
    """Native metadata-dependent ingestion is not available."""


@dataclass
class KnowledgeContext:
    """Serializable knowledge context threaded through the durable job."""

    tenant_id: str
    document_id: str
    version_id: str
    ingestion_id: str
    chunk_id_seed: str
    kb_ids: List[str]
    collection_name: str
    prior_version_id: Optional[str] = None
    job_type: str = "v2_document"
    op_id: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, data: Optional[Dict[str, Any]]
                     ) -> Optional["KnowledgeContext"]:
        if not data:
            return None
        return cls(
            tenant_id=str(data["tenant_id"]),
            document_id=str(data["document_id"]),
            version_id=str(data["version_id"]),
            ingestion_id=str(data["ingestion_id"]),
            chunk_id_seed=str(data.get("chunk_id_seed", "")),
            kb_ids=list(data.get("kb_ids") or []),
            collection_name=str(data.get("collection_name") or ""),
            prior_version_id=data.get("prior_version_id"),
            job_type=str(data.get("job_type") or "v2_document"),
            op_id=data.get("op_id"),
        )


# ---------------------------------------------------------------------------
# Schema/authority probe (bounded cache; resettable for tests)
# ---------------------------------------------------------------------------

_PROBE_TTL_SECONDS = 10.0
_probe_lock = threading.Lock()
_probe_cache: Dict[Any, tuple] = {}


def reset_pipeline_cache() -> None:
    with _probe_lock:
        _probe_cache.clear()


def _probe_key(settings) -> tuple:
    return (settings.host, settings.port, settings.database)


def _authority_state_cached(authority: KnowledgeAuthority) -> AuthorityState:
    settings = authority._repo._settings  # noqa: SLF001 - bounded probe
    key = _probe_key(settings)
    now = time.monotonic()
    with _probe_lock:
        hit = _probe_cache.get(key)
        if hit and now - hit[0] < _PROBE_TTL_SECONDS:
            return hit[1]
    state = authority.read_state()
    with _probe_lock:
        _probe_cache[key] = (now, state)
    return state


def _schema_present(repo: KnowledgeRepository) -> bool:
    """True only when the ``knowledge`` schema/table exists.  A missing
    schema or an unreachable/unconfigured database is False (legacy
    path), never a swallowed 'ready' state."""
    try:
        with repo.transaction() as cur:
            cur.execute(
                "SELECT to_regclass('knowledge.authority') IS NOT NULL "
                "AS present")
            row = cur.fetchone()
            return bool(row["present"]) if row else False
    except Exception:
        return False


class KnowledgePipeline:
    """Submission + execution integration for the ingestion cohort."""

    def __init__(self, repository: Optional[KnowledgeRepository] = None,
                 integration: Optional[KnowledgeIntegration] = None,
                 client_factory=None):
        self._repo = repository or KnowledgeRepository()
        self._integration = integration or KnowledgeIntegration(self._repo)
        self._authority = self._integration._authority  # noqa: SLF001
        self._client_factory = client_factory

    # -- gating ----------------------------------------------------------

    def schema_present(self) -> bool:
        return _schema_present(self._repo)

    def enabled(self) -> bool:
        """True when native knowledge ingestion is permitted.  Rejects
        (raises) when the schema is present but authority is not
        authoritative; returns False when the schema is absent so the
        legacy path continues."""
        if not self.schema_present():
            return False
        state = _authority_state_cached(self._authority)
        if state is AuthorityState.AUTHORITATIVE:
            return True
        raise KnowledgeIngestionUnavailable(
            "native knowledge ingestion is unavailable while knowledge "
            f"authority state is '{state.value}' (fail-closed "
            "pre-cutover posture)")

    # -- submission hooks ------------------------------------------------

    def begin_upload(self, *, tenant_id: str, kb_id: str, source_path: str,
                     filename: Optional[str], collection_name: str,
                     job_id: Optional[str],
                     content_fingerprint: Optional[str]
                     ) -> Optional[KnowledgeContext]:
        try:
            sub = self._integration.begin_upload(
                tenant_id=tenant_id, kb_id=kb_id, source_path=source_path,
                filename=filename, collection_name=collection_name,
                job_id=job_id or f"pending:{tenant_id}",
                content_fingerprint=content_fingerprint)
            return KnowledgeContext(
                tenant_id=tenant_id, document_id=sub.document_id,
                version_id=sub.version_id, ingestion_id=sub.ingestion_id,
                chunk_id_seed=f"{sub.document_id}:pending",
                kb_ids=[kb_id], collection_name=collection_name,
                job_type="v2_upload")
        except (AuthorityError, KnowledgeRepositoryError) as exc:
            raise KnowledgeIngestionUnavailable(
                f"knowledge submission failed ({exc.__class__.__name__})"
            ) from exc

    def begin_document(self, *, tenant_id: str, source_uri: str,
                       kb_id: str, collection_name: str,
                       content_fingerprint: Optional[str],
                       job_id: Optional[str] = None,
                       logical_ref: Optional[str] = None
                       ) -> Optional[KnowledgeContext]:
        sub = self._integration.begin_document(
            tenant_id=tenant_id, source_uri=source_uri, kb_id=kb_id,
            collection_name=collection_name,
            job_id=job_id or f"pending:{tenant_id}",
            content_fingerprint=content_fingerprint,
            logical_ref=logical_ref)
        return KnowledgeContext(
            tenant_id=tenant_id, document_id=sub.document_id,
            version_id=sub.version_id, ingestion_id=sub.ingestion_id,
            chunk_id_seed=f"{sub.document_id}:pending",
            kb_ids=[kb_id], collection_name=collection_name,
            job_type="v2_document")

    # -- execution hooks -------------------------------------------------

    def begin_page(self, *, tenant_id: str, xml_path: str, page_id: object,
                   kb_id: str, collection_name: str,
                   content_fingerprint: str,
                   source_revision: Optional[str] = None,
                   title: Optional[str] = None,
                   job_id: Optional[str] = None) -> KnowledgeContext:
        """Register one MediaWiki page (logical document keyed by page
        identity; version keyed by content fingerprint)."""
        reg = self._integration.begin_mediawiki_page(
            tenant_id=tenant_id, xml_path=xml_path, page_id=page_id,
            kb_id=kb_id, collection_name=collection_name,
            content_fingerprint=content_fingerprint,
            source_revision=source_revision, title=title, job_id=job_id)
        return KnowledgeContext(
            tenant_id=tenant_id, document_id=reg.document_id,
            version_id=reg.version_id, ingestion_id=reg.ingestion_id,
            chunk_id_seed="", kb_ids=[kb_id],
            collection_name=collection_name, job_type="v2_mediawiki")

    def activate_existing(self, context: KnowledgeContext) -> bool:
        """Idempotently make an already-indexed version current and
        serving (used when a MediaWiki page is a content duplicate)."""
        client = self._client()
        with self._repo.transaction(context.tenant_id) as cur:
            doc = self._repo.get_document(
                cur, tenant_id=context.tenant_id,
                document_id=context.document_id)
            prior = (doc or {}).get("current_version_id")
            self._repo.promote_version(
                cur, tenant_id=context.tenant_id,
                document_id=context.document_id,
                new_version_id=context.version_id,
                prior_version_id=prior)
            self._repo.set_ingestion_state(
                cur, tenant_id=context.tenant_id,
                ingestion_id=context.ingestion_id, sync_state="indexed")
        try:
            from retriva.knowledge.visibility import set_serving
            set_serving(client, context.collection_name, context.version_id,
                        True)
            if prior and prior != context.version_id:
                set_serving(client, context.collection_name, prior, False)
        except Exception as exc:
            _log.warning("activate_existing serving failed: %s",
                         exc.__class__.__name__)
        return True

    def context_for_job(self, tenant_id: str, job_id: str,
                        job_type: str) -> Optional[KnowledgeContext]:
        """Look up knowledge evidence for a durable job (by value).
        Returns None when the job is not knowledge-managed."""
        if not self.schema_present():
            return None
        try:
            with self._repo.transaction(tenant_id) as cur:
                cur.execute(
                    "SELECT * FROM knowledge.ingestions WHERE "
                    "tenant_id=%s AND job_id=%s AND job_type=%s "
                    "ORDER BY started_at ASC LIMIT 1",
                    (tenant_id, job_id, job_type))
                row = cur.fetchone()
                if row is None:
                    return None
                version = (self._repo.get_version(
                    cur, tenant_id=tenant_id,
                    version_id=row["target_version_id"])
                    if row.get("target_version_id") else None)
        except Exception:
            return None
        return KnowledgeContext(
            tenant_id=tenant_id, document_id=row["document_id"],
            version_id=row["target_version_id"] or "",
            ingestion_id=row["ingestion_id"],
            chunk_id_seed=(version or {}).get("chunk_id_seed", ""),
            kb_ids=list(row.get("kb_ids") or []),
            collection_name=row["collection_name"], job_type=job_type)

    def record_intent(self, context: KnowledgeContext,
                      point_ids: Sequence[str]) -> str:
        """Build the expected manifest and record the upsert operation
        intent (one bounded batch) BEFORE the Qdrant mutation."""
        rows = [
            {"chunk_ordinal": i, "point_id": str(pid),
             "chunk_contract_version": "native",
             "sync_state": "expected"}
            for i, pid in enumerate(point_ids)
        ]
        with self._repo.transaction(context.tenant_id) as cur:
            self._repo.insert_version_chunks(
                cur, tenant_id=context.tenant_id,
                version_id=context.version_id, rows=rows)
            op_id = self._repo.record_operation(
                cur, tenant_id=context.tenant_id, op_type="upsert_batch",
                collection_name=context.collection_name,
                version_id=context.version_id,
                document_id=context.document_id,
                ingestion_id=context.ingestion_id,
                batch_no=0, batch_count=1,
                expected_count=len(rows), op_state="prepared")
        context.op_id = op_id
        return op_id

    def mark_applied(self, context: KnowledgeContext) -> None:
        """Qdrant upsert succeeded: record applied_unverified evidence and
        flip manifest rows to applied_unverified (verification of the
        full outcome happens in :meth:`complete`)."""
        if not context.op_id:
            return
        with self._repo.transaction(context.tenant_id) as cur:
            self._repo.update_operation_state(
                cur, tenant_id=context.tenant_id, op_id=context.op_id,
                op_state="applied_unverified")
            self._repo.set_chunks_sync_state(
                cur, tenant_id=context.tenant_id,
                version_id=context.version_id,
                ordinals=self._ordinals(context),
                sync_state="applied_unverified", op_id=context.op_id)

    def complete(self, context: KnowledgeContext, *,
                 observed_chunk_count: int) -> bool:
        """Verify, promote, activate new, deactivate prior.  Returns
        False (with reconciliation evidence) if verification fails."""
        client = self._client()
        # 1) manifest + operation verification
        with self._repo.transaction(context.tenant_id) as cur:
            counts = self._repo.count_chunks_by_state(
                cur, tenant_id=context.tenant_id,
                version_id=context.version_id)
            total = sum(counts.values())
            applied = counts.get("applied_unverified", 0)
            if total == 0 or applied != total:
                self._repo.set_ingestion_state(
                    cur, tenant_id=context.tenant_id,
                    ingestion_id=context.ingestion_id,
                    sync_state="index_partial",
                    observed_chunk_count=observed_chunk_count,
                    error_code="manifest_unverified")
                return False
            self._repo.set_chunks_sync_state(
                cur, tenant_id=context.tenant_id,
                version_id=context.version_id,
                ordinals=self._ordinals(context), sync_state="verified",
                op_id=context.op_id)
            if context.op_id:
                self._repo.update_operation_state(
                    cur, tenant_id=context.tenant_id, op_id=context.op_id,
                    op_state="verified")
            doc = self._repo.get_document(
                cur, tenant_id=context.tenant_id,
                document_id=context.document_id)
            prior = (doc or {}).get("current_version_id")
            # 3) promote the new current version in PostgreSQL
            self._repo.promote_version(
                cur, tenant_id=context.tenant_id,
                document_id=context.document_id,
                new_version_id=context.version_id,
                prior_version_id=prior)
        # 4) activate new points
        try:
            from retriva.knowledge.visibility import count_version_points
            from retriva.knowledge.visibility import set_serving
            set_serving(client, context.collection_name, context.version_id,
                        True)
            observed = count_version_points(
                client, context.collection_name, context.version_id)
        except Exception as exc:
            _log.warning("serving activation failed: %s",
                         exc.__class__.__name__)
            observed = -1
        if observed != total:
            with self._repo.transaction(context.tenant_id) as cur:
                self._repo.set_ingestion_state(
                    cur, tenant_id=context.tenant_id,
                    ingestion_id=context.ingestion_id,
                    sync_state="reconciliation_required",
                    observed_chunk_count=observed_chunk_count,
                    error_code="activation_unverified")
            return False
        # 5) deactivate prior points
        if prior and prior != context.version_id:
            try:
                from retriva.knowledge.visibility import set_serving
                set_serving(client, context.collection_name, prior, False)
            except Exception as exc:
                _log.warning("prior deactivation failed: %s",
                             exc.__class__.__name__)
        with self._repo.transaction(context.tenant_id) as cur:
            self._repo.set_ingestion_state(
                cur, tenant_id=context.tenant_id,
                ingestion_id=context.ingestion_id,
                sync_state="indexed",
                observed_chunk_count=observed_chunk_count)
        return True

    def fail(self, context: Optional[KnowledgeContext],
             error_code: str = "ingestion_failed") -> None:
        if context is None:
            return
        try:
            with self._repo.transaction(context.tenant_id) as cur:
                self._repo.set_ingestion_state(
                    cur, tenant_id=context.tenant_id,
                    ingestion_id=context.ingestion_id,
                    sync_state="failed", error_code=error_code[:128])
        except Exception:
            _log.warning("knowledge fail hook could not record state")

    def visibility_fields(self, context: KnowledgeContext, *,
                          serving: bool = False) -> Dict[str, Any]:
        return native_payload_fields(
            tenant_id=context.tenant_id, document_id=context.document_id,
            version_id=context.version_id, kb_ids=context.kb_ids,
            serving=serving, provenance_class="native")

    # -- helpers ---------------------------------------------------------

    def _client(self):
        if self._client_factory is not None:
            return self._client_factory()
        from retriva.indexing.qdrant_store import get_client
        return get_client()

    def _ordinals(self, context: KnowledgeContext) -> List[int]:
        with self._repo.transaction(context.tenant_id) as cur:
            rows = self._repo.list_chunk_states(
                cur, tenant_id=context.tenant_id,
                version_id=context.version_id)
        return [int(r["chunk_ordinal"]) for r in rows]

    def _prior_version(self, tenant_id: str,
                       document_id: Optional[str]) -> Optional[str]:
        if not document_id:
            return None
        try:
            return self._integration.service.current_version_id(
                tenant_id=tenant_id, document_id=document_id)
        except Exception:
            return None


_pipeline_singleton: Optional[KnowledgePipeline] = None
_pipeline_lock = threading.Lock()


def knowledge_pipeline() -> KnowledgePipeline:
    global _pipeline_singleton
    if _pipeline_singleton is None:
        with _pipeline_lock:
            if _pipeline_singleton is None:
                _pipeline_singleton = KnowledgePipeline()
    return _pipeline_singleton


def reset_pipeline() -> None:
    global _pipeline_singleton
    with _pipeline_lock:
        _pipeline_singleton = None
    reset_pipeline_cache()
