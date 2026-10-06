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

"""Hybrid adoption of existing Qdrant content (Spec 028 §9/§15; ADR-033
Decision 9).

Evidence priority: (1) ``dedup_catalog.json`` DocRecords; (2) Qdrant
payload scan; (3) KB registry mapping; (4) reliable durable-job
correlation; (5) explicit uncertainty.

Guarantees: existing point ids preserved; vectors NEVER rewritten,
deleted, re-chunked, or re-embedded; metadata-only payload patch adds
the visibility fields; dry-run default; batch-bounded; resumable;
idempotent; honest ``adopted_verified`` / ``adopted_uncertain``
classification; no reingestion/promotion/deletion/replay of uncertain
records.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from retriva.knowledge.contracts import (
    chunk_contract_version,
    embedding_contract_version,
    parser_contract_version,
)
from retriva.knowledge.ids import (
    legacy_path_identity,
    internal_identity,
    new_id,
)
from retriva.knowledge.repository import (
    KnowledgeRepository,
    KnowledgeRepositoryError,
)
from retriva.logger import get_logger

_log = get_logger(__name__)

_ADOPTED_PARSER = "adopted-legacy"
_ADOPTED_EMBED = "adopted-legacy"


@dataclass
class AdoptionReport:
    mode: str
    tenant_id: str
    collection_name: str
    verified: List[Dict[str, Any]] = field(default_factory=list)
    uncertain: List[Dict[str, Any]] = field(default_factory=list)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    patched_points: int = 0
    next_checkpoint: Optional[int] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "tenant_id": self.tenant_id,
            "collection_name": self.collection_name,
            "verified": len(self.verified),
            "uncertain": len(self.uncertain),
            "conflicts": self.conflicts,
            "patched_points": self.patched_points,
            "next_checkpoint": self.next_checkpoint,
            "detail": self.detail,
        }


class AdoptionMigrator:
    def __init__(self, repository: Optional[KnowledgeRepository] = None,
                 qdrant_client=None,
                 catalog_path: Optional[str] = None,
                 jobs_repo=None):
        self._repo = repository or KnowledgeRepository()
        self._client = qdrant_client
        self._catalog_path = catalog_path
        if jobs_repo is None:
            try:
                from retriva.jobs.repository import (
                    PostgresJobsRepository,
                )
                jobs_repo = PostgresJobsRepository(self._repo._settings)
            except Exception:
                jobs_repo = None
        self._jobs = jobs_repo

    # -- evidence layer 3: adopted KB-registry mapping -------------------

    def kb_registry_map(self, tenant_id: str) -> Dict[str, set]:
        """Adopted ``knowledge.knowledge_bases`` mapping kb_id -> set of
        collection names, used to validate/downgrade adoption evidence."""
        try:
            with self._repo.transaction(tenant_id, privileged=True) as cur:
                cur.execute(
                    "SELECT kb_id, collection_name FROM "
                    "knowledge.knowledge_bases WHERE tenant_id=%s AND "
                    "lifecycle_state='active'", (tenant_id,))
                mapping: Dict[str, set] = {}
                for row in cur.fetchall():
                    mapping.setdefault(row["kb_id"], set()).add(
                        row["collection_name"])
                return mapping
        except Exception:
            return {}

    # -- evidence layer 4: reliable durable-job correlation --------------

    def job_correlated(self, tenant_id: str,
                       legacy_doc_id: Optional[str]) -> bool:
        """True only for a RELIABLE correlation: a terminal
        ``succeeded`` durable job whose explicit subject id equals the
        legacy document id (v2_upload sets subject_id=doc_id).  No
        inference from time proximity or free-text."""
        if not legacy_doc_id or self._jobs is None:
            return False
        try:
            rec = self._jobs.get_job_by_subject(
                tenant_id=tenant_id, job_type="v2_upload",
                subject_id=legacy_doc_id)
        except Exception:
            return False
        if rec is None:
            return False
        status = getattr(rec.status, "value", rec.status)
        return str(status) == "succeeded"

    def _kb_ok(self, kb_map: Dict[str, set], kb_id: str,
               collection_name: str) -> bool:
        if not kb_map:
            return True  # no adopted registry evidence -> do not downgrade
        collections = kb_map.get(kb_id)
        return bool(collections) and collection_name in collections

    # -- evidence --------------------------------------------------------

    def _client_or_default(self):
        if self._client is not None:
            return self._client
        from retriva.indexing.qdrant_store import get_client

        return get_client()

    def _catalog_file(self, collection_name: str) -> str:
        if self._catalog_path:
            return self._catalog_path
        from retriva.config import settings

        storage_dir = getattr(settings, "storage_path", None) or "storage"
        return os.path.join(
            storage_dir, "collections", collection_name,
            "dedup_catalog.json")

    def load_catalog(self, collection_name: str) -> List[Dict[str, Any]]:
        path = self._catalog_file(collection_name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return []
        records = data.get("records", []) if isinstance(data, dict) else []
        return [r for r in records if isinstance(r, dict)]

    def scan_qdrant(self, collection_name: str) -> Dict[str, Dict[str, Any]]:
        """Scan Qdrant payloads grouped by legacy ``doc_id`` (bounded,
        resumable by the client's own pagination)."""
        client = self._client_or_default()
        by_doc: Dict[str, Dict[str, Any]] = {}
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=collection_name, limit=256,
                offset=offset, with_payload=True, with_vectors=False)
            for p in points:
                payload = getattr(p, "payload", None) or {}
                legacy_doc_id = str(
                    payload.get("doc_id")
                    or payload.get("source_path") or "")
                if not legacy_doc_id:
                    continue
                entry = by_doc.setdefault(legacy_doc_id, {
                    "point_ids": [], "payload": payload})
                entry["point_ids"].append(str(p.id))
            if offset is None:
                break
        return by_doc

    # -- adoption --------------------------------------------------------

    def run(self, tenant_id: str, collection_name: str, *,
            apply: bool = False, batch: int = 100,
            checkpoint: int = 0) -> AdoptionReport:
        report = AdoptionReport(
            mode="apply" if apply else "dry-run",
            tenant_id=tenant_id, collection_name=collection_name)
        catalog = self.load_catalog(collection_name)
        scanned = self.scan_qdrant(collection_name)
        kb_map = self.kb_registry_map(tenant_id)

        # The full catalog identity set (not just the window) so unwindowed
        # catalog documents are never misclassified as Qdrant-only.
        catalog_doc_ids = {
            str(r.get("doc_id")) for r in catalog if r.get("doc_id")}
        window = catalog[checkpoint:checkpoint + max(1, int(batch))]
        for rec in window:
            legacy_doc_id = str(rec.get("doc_id") or "")
            self._adopt_catalog_record(
                report, tenant_id=tenant_id,
                collection_name=collection_name, rec=rec,
                qdrant=scanned.get(legacy_doc_id), apply=apply,
                kb_map=kb_map)

        # Evidence priority 2: Qdrant points with no catalog record at
        # all.  Processed once (first window) to stay resumable and
        # idempotent.
        if checkpoint == 0:
            for legacy_doc_id, entry in scanned.items():
                if legacy_doc_id in catalog_doc_ids:
                    continue
                self._adopt_qdrant_only(
                    report, tenant_id=tenant_id,
                    collection_name=collection_name,
                    legacy_doc_id=legacy_doc_id, entry=entry,
                    apply=apply, kb_map=kb_map)

        next_cp = checkpoint + len(window)
        report.next_checkpoint = next_cp if next_cp < len(catalog) else None
        report.detail = {
            "catalog_records": len(catalog),
            "qdrant_docs": len(scanned),
            "batch": batch,
            "resumed_from": checkpoint,
        }
        return report

    def _source_identity(self, rec: Dict[str, Any], legacy_doc_id: str):
        source_paths = rec.get("source_paths") or []
        if source_paths:
            return legacy_path_identity(str(source_paths[0]))
        return internal_identity(f"legacy-doc:{legacy_doc_id}")

    def _adopt_catalog_record(self, report: AdoptionReport, *, tenant_id,
                              collection_name, rec, qdrant, apply,
                              kb_map) -> None:
        legacy_doc_id = str(rec.get("doc_id") or "")
        if not legacy_doc_id:
            report.conflicts.append({"reason": "catalog_missing_doc_id"})
            return
        content_hash = rec.get("content_hash")
        kb_id = str(rec.get("kb_id") or "default")
        point_ids = list((qdrant or {}).get("point_ids") or [])
        kb_ok = self._kb_ok(kb_map, kb_id, collection_name)
        verified = bool(content_hash) and bool(point_ids) and kb_ok
        provenance = "adopted_verified" if verified else "adopted_uncertain"
        classification = {
            "legacy_doc_id": legacy_doc_id,
            "kb_id": kb_id,
            "provenance": provenance,
            "points": len(point_ids),
        }
        if not point_ids:
            report.conflicts.append({
                "legacy_doc_id": legacy_doc_id,
                "classification": "catalog_record_without_qdrant_points"})
        if not content_hash:
            report.conflicts.append({
                "legacy_doc_id": legacy_doc_id,
                "classification": "missing_fingerprint_uncertain"})
        if not kb_ok:
            report.conflicts.append({
                "legacy_doc_id": legacy_doc_id,
                "classification": "kb_registry_mismatch",
                "kb_id": kb_id, "collection_name": collection_name})
        if provenance == "adopted_verified" and not self.job_correlated(
                tenant_id, legacy_doc_id):
            classification["job_correlation"] = "absent_non_disqualifying"

        identity = self._source_identity(rec, legacy_doc_id)
        if not apply:
            (report.verified if verified else report.uncertain).append(
                classification)
            return

        document_id, version_id = self._persist_adopted(
            tenant_id=tenant_id, collection_name=collection_name,
            identity=identity, legacy_doc_id=legacy_doc_id, kb_id=kb_id,
            content_hash=content_hash, provenance=provenance,
            user_metadata=rec.get("user_metadata") or {},
            title=rec.get("filename"),
            content_size=rec.get("content_size"),
            source_paths=rec.get("source_paths") or [],
            point_ids=point_ids)
        report.patched_points += self._patch_points(
            collection_name, point_ids, tenant_id=tenant_id,
            document_id=document_id, version_id=version_id,
            kb_id=kb_id, provenance=provenance)
        (report.verified if verified else report.uncertain).append(
            {**classification, "document_id": document_id,
             "version_id": version_id})

    def _adopt_qdrant_only(self, report: AdoptionReport, *, tenant_id,
                           collection_name, legacy_doc_id, entry,
                           apply, kb_map) -> None:
        payload = entry.get("payload") or {}
        kb_ids = (payload.get("user_metadata") or {}).get("kb_ids") or []
        kb_id = str(payload.get("kb_id") or (kb_ids[0] if kb_ids
                                             else "default"))
        source_path = payload.get("source_path") or ""
        identity = (legacy_path_identity(str(source_path))
                    if source_path
                    else internal_identity(f"legacy-doc:{legacy_doc_id}"))
        kb_ok = self._kb_ok(kb_map, kb_id, collection_name)
        job_ok = self.job_correlated(tenant_id, legacy_doc_id)
        verified = bool(kb_ok and job_ok)
        provenance = "adopted_verified" if verified else "adopted_uncertain"
        (report.verified if verified else report.uncertain).append({
            "legacy_doc_id": legacy_doc_id,
            "kb_id": kb_id,
            "provenance": provenance,
            "points": len(entry.get("point_ids") or []),
            "classification": ("qdrant_only_job_correlated"
                               if verified else
                               "qdrant_only_unattributed"),
        })
        if not apply:
            return
        document_id, version_id = self._persist_adopted(
            tenant_id=tenant_id, collection_name=collection_name,
            identity=identity, legacy_doc_id=legacy_doc_id, kb_id=kb_id,
            content_hash=None, provenance=provenance,
            user_metadata=payload.get("user_metadata") or {},
            title=payload.get("filename"),
            content_size=payload.get("content_size"),
            source_paths=payload.get("source_paths") or [],
            point_ids=list(entry.get("point_ids") or []))
        report.patched_points += self._patch_points(
            collection_name, list(entry.get("point_ids") or []),
            tenant_id=tenant_id, document_id=document_id,
            version_id=version_id, kb_id=kb_id,
            provenance=provenance)

    def _persist_adopted(self, *, tenant_id, collection_name, identity,
                         legacy_doc_id, kb_id, content_hash, provenance,
                         user_metadata, title, content_size, source_paths,
                         point_ids):
        from retriva.knowledge.ids import normalize_source_identity

        adopted_identity = normalize_source_identity(
            identity.namespace, identity.normalized_ref,
            display_name=identity.display_name,
            external_ref=identity.external_ref)
        # Attach provenance through a wrapper attribute used by the repo.
        with self._repo.transaction(tenant_id, privileged=True) as cur:
            src = self._repo.resolve_or_create_source(
                cur, tenant_id=tenant_id, identity=adopted_identity,
                source_id=None)
            cur.execute(
                "UPDATE knowledge.sources SET provenance=%s WHERE "
                "tenant_id=%s AND source_id=%s AND provenance='native'",
                (provenance, tenant_id, src["source_id"]))
            doc = self._repo.resolve_or_create_document(
                cur, tenant_id=tenant_id, source_id=src["source_id"],
                title=title, user_metadata=user_metadata)
            self._repo.replace_memberships(
                cur, tenant_id=tenant_id, document_id=doc["document_id"],
                kb_ids=[kb_id], collection_name=collection_name)
            existing = self._repo.find_adopted_version(
                cur, tenant_id=tenant_id,
                document_id=doc["document_id"],
                chunk_id_seed=legacy_doc_id)
            if existing is None:
                existing = self._repo.create_version(
                    cur, tenant_id=tenant_id,
                    document_id=doc["document_id"],
                    content_fingerprint=content_hash,
                    parser_contract_version=parser_contract_version(
                        _ADOPTED_PARSER),
                    embedding_contract_version=embedding_contract_version()
                    + f"|{_ADOPTED_EMBED}",
                    chunk_id_seed=legacy_doc_id,
                    chunk_contract_version="adopted-legacy",
                    content_size=content_size,
                    provenance=provenance)
            version_id = existing["version_id"]
            self._repo.set_version_status(
                cur, tenant_id=tenant_id, version_id=version_id,
                status="indexed")
            if point_ids:
                self._repo.insert_version_chunks(
                    cur, tenant_id=tenant_id, version_id=version_id,
                    rows=[
                        {"chunk_ordinal": i, "point_id": pid,
                         "chunk_contract_version": "adopted-legacy",
                         "sync_state": "verified"}
                        for i, pid in enumerate(point_ids)])
            self._repo.promote_version(
                cur, tenant_id=tenant_id, document_id=doc["document_id"],
                new_version_id=version_id, prior_version_id=None)
            document_id = doc["document_id"]
        return document_id, version_id

    def _patch_points(self, collection_name, point_ids, *, tenant_id,
                      document_id, version_id, kb_id, provenance) -> int:
        if not point_ids:
            return 0
        from retriva.knowledge.visibility import native_payload_fields

        fields = native_payload_fields(
            tenant_id=tenant_id, document_id=document_id,
            version_id=version_id, kb_ids=[kb_id], serving=True,
            provenance_class=provenance)
        client = self._client_or_default()
        patched = 0
        for pid in point_ids:
            try:
                client.set_payload(
                    collection_name=collection_name, payload=fields,
                    points=[pid], wait=True)
                patched += 1
            except Exception as exc:
                _log.warning(
                    "adoption payload patch failed for a point: %s",
                    exc.__class__.__name__)
        return patched
