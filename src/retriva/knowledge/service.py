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

"""One common Core-owned knowledge service (Spec 028 §20).

Used by the generic v2 document, v2 upload, and v2 MediaWiki adapters.
Adapters normalize workflow-specific source identity and metadata; the
domain lifecycle and state machines live here only.  No per-workflow
relational lifecycle exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from retriva.knowledge.contracts import (
    chunk_contract_version,
    embedding_contract_version,
    parser_contract_version,
)
from retriva.knowledge.domain import (
    IngestionMode,
    IngestionSyncState,
    Provenance,
    VersionStatus,
)
from retriva.knowledge.ids import (
    SourceIdentity,
    derive_point_id,
    new_id,
)
from retriva.knowledge.repository import KnowledgeRepository
from retriva.logger import get_logger

_log = get_logger(__name__)


@dataclass
class SubmissionResult:
    tenant_id: str
    source_id: str
    document_id: str
    version_id: str
    ingestion_id: str
    version_reused: bool
    provenance: str = Provenance.NATIVE.value


@dataclass
class VersionRegistration:
    document_id: str
    version_id: str
    ingestion_id: str
    reused: bool


class KnowledgeService:
    """Common knowledge-domain lifecycle service."""

    def __init__(self, repository: Optional[KnowledgeRepository] = None):
        self._repo = repository or KnowledgeRepository()

    @property
    def repository(self) -> KnowledgeRepository:
        return self._repo

    # -- submission ------------------------------------------------------

    def register_submission(
        self, *, tenant_id: str, identity: SourceIdentity,
        kb_ids: Sequence[str], collection_name: str, job_id: str,
        job_type: str, content_fingerprint: Optional[str],
        content_size: Optional[int] = None,
        media_type: Optional[str] = None,
        storage_ref: Optional[str] = None,
        title: Optional[str] = None,
        user_metadata: Optional[Dict[str, Any]] = None,
        document_id: Optional[str] = None,
        parser_name: str = "default",
        source_revision: Optional[str] = None,
        ingestion_mode: str = IngestionMode.CREATE.value,
    ) -> SubmissionResult:
        """One transaction: resolve/create source + document + memberships,
        resolve/reuse/create the version, create ingestion evidence.

        The durable job is submitted by the caller AFTER this commits
        (the document_id feeds the job subject).  Idempotent: identical
        resubmission with the same processing contract resolves to the
        same version; a concurrent loser observes the winner.
        """
        parser_cv = parser_contract_version(parser_name)
        embed_cv = embedding_contract_version()
        chunk_cv = chunk_contract_version()
        with self._repo.transaction(tenant_id) as cur:
            src = self._repo.resolve_or_create_source(
                cur, tenant_id=tenant_id, identity=identity)
            doc = self._repo.resolve_or_create_document(
                cur, tenant_id=tenant_id, source_id=src["source_id"],
                document_id=document_id, title=title,
                user_metadata=user_metadata)
            self._repo.replace_memberships(
                cur, tenant_id=tenant_id, document_id=doc["document_id"],
                kb_ids=kb_ids, collection_name=collection_name)
            existing = None
            if content_fingerprint is not None:
                existing = self._repo.find_version_by_identity(
                    cur, tenant_id=tenant_id,
                    document_id=doc["document_id"],
                    content_fingerprint=content_fingerprint,
                    parser_contract_version=parser_cv,
                    embedding_contract_version=embed_cv)
            reused = existing is not None
            if existing is None:
                chunk_id_seed = self._chunk_id_seed(
                    doc["document_id"], content_fingerprint)
                existing = self._repo.create_version(
                    cur, tenant_id=tenant_id,
                    document_id=doc["document_id"],
                    content_fingerprint=content_fingerprint,
                    parser_contract_version=parser_cv,
                    embedding_contract_version=embed_cv,
                    chunk_id_seed=chunk_id_seed,
                    chunk_contract_version=chunk_cv,
                    media_type=media_type, content_size=content_size,
                    storage_ref=storage_ref,
                    source_revision=source_revision,
                    provenance=Provenance.NATIVE.value)
            ingestion = self._repo.create_ingestion(
                cur, tenant_id=tenant_id, job_id=job_id, job_type=job_type,
                document_id=doc["document_id"],
                collection_name=collection_name, kb_ids=kb_ids,
                target_version_id=existing["version_id"],
                ingestion_mode=ingestion_mode,
                sync_state=IngestionSyncState.REGISTERED.value)
        return SubmissionResult(
            tenant_id=tenant_id, source_id=src["source_id"],
            document_id=doc["document_id"],
            version_id=existing["version_id"],
            ingestion_id=ingestion["ingestion_id"],
            version_reused=reused)

    def register_page_version(
        self, *, tenant_id: str, identity: SourceIdentity,
        kb_ids: Sequence[str], collection_name: str,
        content_fingerprint: str, source_revision: Optional[str],
        content_size: Optional[int] = None,
        media_type: Optional[str] = None,
        title: Optional[str] = None,
        user_metadata: Optional[Dict[str, Any]] = None,
        parser_name: str = "default",
        job_id: Optional[str] = None,
    ) -> VersionRegistration:
        """Resolve/create a per-page document + version OUTSIDE a job
        submission transaction (MediaWiki creates one document per
        page during processing).  The ingestion row is created with a
        synthetic correlation id when no job id is available.
        """
        parser_cv = parser_contract_version(parser_name)
        embed_cv = embedding_contract_version()
        chunk_cv = chunk_contract_version()
        with self._repo.transaction(tenant_id) as cur:
            src = self._repo.resolve_or_create_source(
                cur, tenant_id=tenant_id, identity=identity)
            doc = self._repo.resolve_or_create_document(
                cur, tenant_id=tenant_id, source_id=src["source_id"],
                title=title, user_metadata=user_metadata)
            self._repo.replace_memberships(
                cur, tenant_id=tenant_id, document_id=doc["document_id"],
                kb_ids=kb_ids, collection_name=collection_name)
            existing = self._repo.find_version_by_identity(
                cur, tenant_id=tenant_id,
                document_id=doc["document_id"],
                content_fingerprint=content_fingerprint,
                parser_contract_version=parser_cv,
                embedding_contract_version=embed_cv)
            reused = existing is not None
            if existing is None:
                existing = self._repo.create_version(
                    cur, tenant_id=tenant_id,
                    document_id=doc["document_id"],
                    content_fingerprint=content_fingerprint,
                    parser_contract_version=parser_cv,
                    embedding_contract_version=embed_cv,
                    chunk_id_seed=self._chunk_id_seed(
                        doc["document_id"], content_fingerprint),
                    chunk_contract_version=chunk_cv,
                    media_type=media_type, content_size=content_size,
                    source_revision=source_revision,
                    provenance=Provenance.NATIVE.value)
            ingestion = self._repo.create_ingestion(
                cur, tenant_id=tenant_id,
                job_id=job_id or f"inline:{new_id()}",
                job_type="v2_mediawiki",
                document_id=doc["document_id"],
                collection_name=collection_name, kb_ids=kb_ids,
                target_version_id=existing["version_id"],
                ingestion_mode=IngestionMode.CREATE.value,
                sync_state=IngestionSyncState.REGISTERED.value)
        return VersionRegistration(
            document_id=doc["document_id"],
            version_id=existing["version_id"],
            ingestion_id=ingestion["ingestion_id"], reused=reused)

    # -- manifest --------------------------------------------------------

    @staticmethod
    def _chunk_id_seed(document_id: str,
                       content_fingerprint: Optional[str]) -> str:
        """Deterministic seed for point-id derivation.

        NOT a content-hash identity: it is a persisted version property
        (architecture §3.5) combining the opaque document id with the
        content fingerprint.  Legacy adopted ids are stored verbatim.
        """
        fp = content_fingerprint or "nofingerprint"
        return f"{document_id}:{fp}"

    def register_manifest(self, *, tenant_id: str, version_id: str,
                          chunk_id_seed: str,
                          chunk_count: int) -> List[Dict[str, Any]]:
        """Seed one bounded manifest row per expected point (batched,
        same transaction as the write)."""
        chunk_cv = chunk_contract_version()
        rows = [
            {
                "chunk_ordinal": i,
                "point_id": derive_point_id(chunk_id_seed, i),
                "chunk_contract_version": chunk_cv,
                "sync_state": "expected",
            }
            for i in range(max(0, int(chunk_count)))
        ]
        with self._repo.transaction(tenant_id) as cur:
            self._repo.insert_version_chunks(
                cur, tenant_id=tenant_id, version_id=version_id,
                rows=rows)
        return rows

    def record_upsert_batch(
            self, *, tenant_id: str, version_id: str, document_id: str,
            collection_name: str, ingestion_id: Optional[str],
            batch_no: int, batch_count: int, point_ids: Sequence[str],
            op_id: Optional[str] = None) -> str:
        """Record operation evidence + flip manifest rows to
        ``applied_unverified`` in ONE transaction, then the caller
        performs the Qdrant upsert outside the transaction."""
        with self._repo.transaction(tenant_id) as cur:
            op_id = self._repo.record_operation(
                cur, tenant_id=tenant_id, op_type="upsert_batch",
                collection_name=collection_name, version_id=version_id,
                document_id=document_id, ingestion_id=ingestion_id,
                batch_no=batch_no, batch_count=batch_count,
                expected_count=len(point_ids),
                op_state="prepared", op_id=op_id)
            ordinals = self._ordinals_for_points(
                cur, tenant_id=tenant_id, version_id=version_id,
                point_ids=point_ids)
            self._repo.set_chunks_sync_state(
                cur, tenant_id=tenant_id, version_id=version_id,
                ordinals=ordinals, sync_state="applied_unverified",
                op_id=op_id)
        return op_id

    def mark_operation_verified(self, *, tenant_id: str, op_id: str
                                ) -> int:
        with self._repo.transaction(tenant_id) as cur:
            return self._repo.update_operation_state(
                cur, tenant_id=tenant_id, op_id=op_id,
                op_state="verified")

    def mark_operation_failed(self, *, tenant_id: str, op_id: str,
                              error_code: str) -> int:
        with self._repo.transaction(tenant_id) as cur:
            return self._repo.update_operation_state(
                cur, tenant_id=tenant_id, op_id=op_id,
                op_state="failed", error_code=error_code[:128])

    # -- lifecycle -------------------------------------------------------

    def mark_ingestion_state(self, *, tenant_id: str, ingestion_id: str,
                             sync_state: str,
                             observed_chunk_count: Optional[int] = None,
                             error_code: Optional[str] = None
                             ) -> int:
        with self._repo.transaction(tenant_id) as cur:
            return self._repo.set_ingestion_state(
                cur, tenant_id=tenant_id, ingestion_id=ingestion_id,
                sync_state=sync_state,
                observed_chunk_count=observed_chunk_count,
                error_code=error_code)

    def verify_version_complete(self, *, tenant_id: str,
                                version_id: str) -> bool:
        """Require observed == expected AND every manifest row verified
        AND every op verified before promotion is allowed."""
        with self._repo.transaction(tenant_id) as cur:
            counts = self._repo.count_chunks_by_state(
                cur, tenant_id=tenant_id, version_id=version_id)
            total = sum(counts.values())
            verified = counts.get("verified", 0)
            version = self._repo.get_version(
                cur, tenant_id=tenant_id, version_id=version_id)
            expected = (version or {}).get("chunk_count_expected")
            if total == 0:
                return False
            if verified != total:
                return False
            if expected is not None and total != expected:
                return False
            return True

    def finalize_verified(self, *, tenant_id: str, document_id: str,
                          version_id: str, ingestion_id: str,
                          prior_version_id: Optional[str],
                          observed_chunk_count: Optional[int] = None
                          ) -> bool:
        """Completion gate: verify manifest + ops, then atomically
        promote the new version in PostgreSQL.  A late/duplicate call is
        an idempotent no-op."""
        with self._repo.transaction(tenant_id) as cur:
            counts = self._repo.count_chunks_by_state(
                cur, tenant_id=tenant_id, version_id=version_id)
            total = sum(counts.values())
            version = self._repo.get_version(
                cur, tenant_id=tenant_id, version_id=version_id)
            if version is None:
                return False
            expected = version.get("chunk_count_expected")
            if total == 0 or counts.get("verified", 0) != total:
                self._repo.set_ingestion_state(
                    cur, tenant_id=tenant_id, ingestion_id=ingestion_id,
                    sync_state=IngestionSyncState.INDEX_PARTIAL.value,
                    observed_chunk_count=observed_chunk_count,
                    error_code="manifest_incomplete")
                return False
            if expected is not None and total != expected:
                self._repo.set_ingestion_state(
                    cur, tenant_id=tenant_id, ingestion_id=ingestion_id,
                    sync_state=IngestionSyncState.INDEX_PARTIAL.value,
                    observed_chunk_count=observed_chunk_count,
                    error_code="chunk_count_mismatch")
                return False
            promoted = self._repo.promote_version(
                cur, tenant_id=tenant_id, document_id=document_id,
                new_version_id=version_id, prior_version_id=prior_version_id)
            self._repo.set_ingestion_state(
                cur, tenant_id=tenant_id, ingestion_id=ingestion_id,
                sync_state=IngestionSyncState.INDEXED.value,
                observed_chunk_count=observed_chunk_count)
            return promoted

    def fail_ingestion(self, *, tenant_id: str, ingestion_id: str,
                       version_id: Optional[str] = None,
                       error_code: str = "ingestion_failed") -> None:
        """Record failure; the PRIOR current version is untouched."""
        with self._repo.transaction(tenant_id) as cur:
            try:
                self._repo.set_ingestion_state(
                    cur, tenant_id=tenant_id, ingestion_id=ingestion_id,
                    sync_state=IngestionSyncState.FAILED.value,
                    error_code=error_code[:128])
            except Exception:
                _log.warning("could not mark ingestion failed")
            if version_id:
                try:
                    self._repo.set_version_status(
                        cur, tenant_id=tenant_id, version_id=version_id,
                        status=VersionStatus.FAILED.value)
                except Exception:
                    pass

    def fail_version_partial(self, *, tenant_id: str,
                             ingestion_id: str, version_id: str) -> None:
        with self._repo.transaction(tenant_id) as cur:
            self._repo.set_ingestion_state(
                cur, tenant_id=tenant_id, ingestion_id=ingestion_id,
                sync_state=IngestionSyncState.INDEX_PARTIAL.value,
                error_code="partial_indexing")
            try:
                self._repo.set_version_status(
                    cur, tenant_id=tenant_id, version_id=version_id,
                    status=VersionStatus.INDEX_PARTIAL.value)
            except Exception:
                pass

    # -- helpers ---------------------------------------------------------

    def current_version_id(self, *, tenant_id: str,
                           document_id: str) -> Optional[str]:
        with self._repo.transaction(tenant_id) as cur:
            doc = self._repo.get_document(
                cur, tenant_id=tenant_id, document_id=document_id)
            return doc.get("current_version_id") if doc else None

    def _ordinals_for_points(self, cur, *, tenant_id: str,
                             version_id: str,
                             point_ids: Sequence[str]) -> List[int]:
        if not point_ids:
            return []
        cur.execute(
            "SELECT chunk_ordinal, point_id FROM "
            "knowledge.version_chunks WHERE tenant_id=%s AND "
            "version_id=%s AND point_id = ANY(%s)",
            (tenant_id, version_id, list(point_ids)))
        return [int(r["chunk_ordinal"]) for r in cur.fetchall()]
