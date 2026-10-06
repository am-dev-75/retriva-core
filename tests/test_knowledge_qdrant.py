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

"""Spec 028 P5/P6/P7/P8: visibility filter, adoption, reconciliation,
deletion, and purge — using a deterministic in-memory Qdrant double
(the local Qdrant server requires UUID point ids, which the legacy md5
point-id scheme does not use)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from qdrant_client import models  # noqa: E402

from retriva.knowledge.adoption import AdoptionMigrator  # noqa: E402
from retriva.knowledge.authority import (  # noqa: E402
    AuthorityState,
    KnowledgeAuthority,
)
from retriva.knowledge.commands import (  # noqa: E402
    adopt as adopt_cmd,
    reconcile as reconcile_cmd,
    status as status_cmd,
)
from retriva.knowledge.deletion import DeletionService  # noqa: E402
from retriva.knowledge.purge import Purger  # noqa: E402
from retriva.knowledge.reconcile import Reconciler  # noqa: E402
from retriva.knowledge.repository import KnowledgeRepository  # noqa: E402
from retriva.knowledge.visibility import (  # noqa: E402
    count_incomplete_visible_points,
    count_version_points,
    incomplete_payload_filter,
    native_payload_fields,
    point_ids_for_version,
    set_serving,
    with_serving_clause,
)

TENANT = "tenant-q"
COLLECTION = "retriva_chunks"


# ---------------------------------------------------------------------------
# Deterministic Qdrant double
# ---------------------------------------------------------------------------

@dataclass
class _Rec:
    id: str
    payload: Dict[str, Any]


class _Count:
    def __init__(self, count: int):
        self.count = count


class FakeQdrant:
    def __init__(self):
        self.points: Dict[str, _Rec] = {}

    # -- helpers ---------------------------------------------------------

    def _match(self, payload: Dict[str, Any], flt) -> bool:
        if flt is None:
            return True
        if isinstance(flt, list):
            return all(self._match(payload, f) for f in flt)
        if isinstance(flt, models.Filter):
            if flt.must and not all(
                    self._match(payload, c) for c in flt.must):
                return False
            if flt.should and not any(
                    self._match(payload, c) for c in flt.should):
                return False
            if flt.must_not and any(
                    self._match(payload, c) for c in flt.must_not):
                return False
            return True
        if isinstance(flt, models.FieldCondition):
            value = payload.get(flt.key)
            if flt.match is None:
                return value is not None
            if isinstance(flt.match, models.MatchValue):
                return value == flt.match.value
            if isinstance(flt.match, models.MatchAny):
                if isinstance(value, list):
                    return any(v in flt.match.any for v in value)
                return value in flt.match.any
            return False
        if isinstance(flt, models.IsEmptyCondition):
            key = flt.is_empty.key
            return key not in payload or payload.get(key) is None
        return False

    # -- Qdrant surface used by the knowledge modules --------------------

    def collection_exists(self, collection_name: str) -> bool:
        return True

    def create_collection(self, *args, **kwargs) -> None:
        return None

    def create_payload_index(self, *args, **kwargs) -> None:
        return None

    def upsert(self, *, collection_name, points) -> None:
        for p in points:
            self.points[str(p.id)] = _Rec(str(p.id), dict(p.payload))

    def count(self, *, collection_name, count_filter=None,
              exact=True) -> _Count:
        return _Count(sum(1 for r in self.points.values()
                          if self._match(r.payload, count_filter)))

    def scroll(self, *, collection_name, scroll_filter=None, limit=100,
               offset=None, with_payload=True, with_vectors=False):
        matching = [r for r in self.points.values()
                    if self._match(r.payload, scroll_filter)]
        matching.sort(key=lambda r: r.id)
        start = int(offset or 0)
        window = matching[start:start + limit]
        next_offset = (start + limit) if (start + limit) < len(matching) \
            else None
        return [models.Record(id=r.id, payload=r.payload, vector=None)
                for r in window], next_offset

    def set_payload(self, *, collection_name, payload, points=None, wait=True
                    ) -> None:
        if isinstance(points, list):
            targets = [str(i) for i in points]
            for pid in targets:
                if pid in self.points:
                    self.points[pid].payload.update(payload)
            return
        for r in self.points.values():
            if self._match(r.payload, points):
                r.payload.update(payload)

    def delete(self, *, collection_name, points_selector=None, wait=True
               ) -> None:
        if isinstance(points_selector, list):
            for pid in points_selector:
                self.points.pop(str(pid), None)
            return
        for pid in [pid for pid, r in self.points.items()
                    if self._match(r.payload, points_selector)]:
            self.points.pop(pid, None)

    def query_points(self, *, collection_name, query, query_filter=None,
                     limit=10, with_payload=True):
        from types import SimpleNamespace

        matching = [r for r in self.points.values()
                    if self._match(r.payload, query_filter)]
        return SimpleNamespace(points=[
            SimpleNamespace(id=r.id, score=1.0,
                            payload=dict(r.payload))
            for r in matching[:limit]])


def _seed_point(fake: FakeQdrant, point_id: str, *, doc_id: str,
                source_path: str = "/data/a.pdf", kb_id: str = "default",
                content_hash: str = "sha256:" + "a" * 64):
    fake.points[point_id] = _Rec(point_id, {
        "text": "hello",
        "doc_id": doc_id,
        "source_path": source_path,
        "source_paths": [source_path],
        "filename": source_path.rsplit("/", 1)[-1],
        "content_hash": content_hash,
        "content_hash_algorithm": "sha256",
        "user_metadata": {"kb_ids": [kb_id]},
        "kb_id": kb_id,
    })


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------

def test_serving_clause_is_static_and_keeps_unmarked_visible():
    clause = with_serving_clause(None)
    assert clause.should and len(clause.should) == 2
    fake = FakeQdrant()
    fake.points["a"] = _Rec("a", {"serving": True})
    fake.points["b"] = _Rec("b", {"serving": False})
    fake.points["c"] = _Rec("c", {})
    visible = [r.id for r in fake.points.values()
               if fake._match(r.payload, clause)]
    assert set(visible) == {"a", "c"}


def test_set_serving_flips_only_target_version():
    fake = FakeQdrant()
    fake.points["a"] = _Rec("a", {"version_id": "v1", "serving": True})
    fake.points["b"] = _Rec("b", {"version_id": "v2", "serving": False})
    set_serving(fake, COLLECTION, "v1", False)
    assert fake.points["a"].payload["serving"] is False
    assert fake.points["b"].payload["serving"] is False


def test_incomplete_visible_points_gate():
    fake = FakeQdrant()
    fields = native_payload_fields(
        tenant_id=TENANT, document_id="d1", version_id="v1",
        kb_ids=["default"], serving=True)
    fake.points["ok"] = _Rec("ok", fields)
    fake.points["legacy"] = _Rec("legacy", {"doc_id": "x"})
    assert count_incomplete_visible_points(fake, COLLECTION) == 1


# ---------------------------------------------------------------------------
# Adoption
# ---------------------------------------------------------------------------

def _catalog(tmp_path, records: List[Dict[str, Any]]) -> str:
    path = tmp_path / "dedup_catalog.json"
    path.write_text(json.dumps({"records": records}))
    return str(path)


def test_adoption_catalog_first_verified(knowledge_repo, tmp_path):
    fake = FakeQdrant()
    _seed_point(fake, "a" * 32, doc_id="doc_legacy1")
    catalog = _catalog(tmp_path, [{
        "doc_id": "doc_legacy1", "kb_id": "default",
        "collection_name": COLLECTION,
        "content_hash": "sha256:" + "a" * 64,
        "source_paths": ["/data/a.pdf"], "filename": "a.pdf",
        "content_size": 5, "user_metadata": {},
        "chunk_count": 1, "ingestion_status": "completed",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }])
    migrator = AdoptionMigrator(
        knowledge_repo, qdrant_client=fake, catalog_path=catalog)
    dry = migrator.run(TENANT, COLLECTION, apply=False)
    assert len(dry.verified) == 1 and not dry.uncertain
    applied = migrator.run(TENANT, COLLECTION, apply=True)
    assert applied.patched_points == 1
    # Point id preserved; visibility fields patched; vectors untouched.
    point = fake.points["a" * 32]
    assert point.payload["serving"] is True
    assert point.payload["provenance_class"] == "adopted_verified"
    assert point.payload["tenant_id"] == TENANT
    assert "text" in point.payload  # vector content fields untouched
    # Idempotent rerun.
    again = migrator.run(TENANT, COLLECTION, apply=True)
    assert again.patched_points == 1


def test_adoption_qdrant_only_is_uncertain(knowledge_repo, tmp_path):
    fake = FakeQdrant()
    _seed_point(fake, "b" * 32, doc_id="doc_orphan")
    catalog = _catalog(tmp_path, [])
    migrator = AdoptionMigrator(
        knowledge_repo, qdrant_client=fake, catalog_path=catalog)
    report = migrator.run(TENANT, COLLECTION, apply=True)
    assert len(report.uncertain) >= 1
    assert fake.points["b" * 32].payload["provenance_class"] == \
        "adopted_uncertain"


def test_adoption_missing_points_is_uncertain(knowledge_repo, tmp_path):
    fake = FakeQdrant()
    catalog = _catalog(tmp_path, [{
        "doc_id": "doc_missing", "kb_id": "default",
        "collection_name": COLLECTION,
        "content_hash": "sha256:" + "c" * 64,
        "source_paths": ["/data/missing.pdf"], "filename": "missing.pdf",
        "user_metadata": {}, "chunk_count": 0,
        "ingestion_status": "pending", "created_at": "x", "updated_at": "x",
    }])
    migrator = AdoptionMigrator(
        knowledge_repo, qdrant_client=fake, catalog_path=catalog)
    report = migrator.run(TENANT, COLLECTION, apply=True)
    assert any(c["classification"] ==
               "catalog_record_without_qdrant_points"
               for c in report.conflicts)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def test_reconcile_detects_missing_and_orphan(knowledge_repo, tmp_path):
    fake = FakeQdrant()
    _seed_point(fake, "d" * 32, doc_id="doc_r")
    catalog = _catalog(tmp_path, [{
        "doc_id": "doc_r", "kb_id": "default",
        "collection_name": COLLECTION,
        "content_hash": "sha256:" + "d" * 64,
        "source_paths": ["/data/r.pdf"], "filename": "r.pdf",
        "user_metadata": {}, "chunk_count": 2,
        "ingestion_status": "completed", "created_at": "x", "updated_at": "x",
    }])
    migrator = AdoptionMigrator(
        knowledge_repo, qdrant_client=fake, catalog_path=catalog)
    migrator.run(TENANT, COLLECTION, apply=True)
    # Point exists in Qdrant but only one manifest row; no missing.
    report = Reconciler(
        knowledge_repo, qdrant_client=fake).run(TENANT, COLLECTION)
    assert report.to_dict()["counts"]
    # Now remove the point -> missing detected.
    fake.points.pop("d" * 32)
    report2 = Reconciler(
        knowledge_repo, qdrant_client=fake).run(TENANT, COLLECTION)
    assert any(f["classification"] == "missing_points"
               for f in report2.findings)


def test_reconcile_command_reports_findings(knowledge_repo, tmp_path):
    fake = FakeQdrant()
    catalog = _catalog(tmp_path, [])
    payload = reconcile_cmd(
        TENANT, COLLECTION, repository=knowledge_repo,
        qdrant_client=fake)
    assert "findings" in payload


# ---------------------------------------------------------------------------
# Deletion + purge
# ---------------------------------------------------------------------------

def test_deletion_tombstone_and_purge(knowledge_repo):
    fake = FakeQdrant()
    # Native document with a serving point carrying document_id.
    from retriva.knowledge.ids import upload_identity
    from retriva.knowledge.service import KnowledgeService

    service = KnowledgeService(knowledge_repo)
    sub = service.register_submission(
        tenant_id=TENANT, identity=upload_identity("default", "del.pdf"),
        kb_ids=["default"], collection_name=COLLECTION, job_id="job-del",
        job_type="v2_upload",
        content_fingerprint="sha256:" + "e" * 64)
    fake.points["f" * 32] = _Rec("f" * 32, native_payload_fields(
        tenant_id=TENANT, document_id=sub.document_id,
        version_id=sub.version_id, kb_ids=["default"], serving=True))

    deletion = DeletionService(knowledge_repo, qdrant_client=fake)
    intent = deletion.request_deletion(TENANT, sub.document_id, COLLECTION)
    assert intent.state == "delete_pending"
    # Idempotent repeat.
    assert deletion.request_deletion(
        TENANT, sub.document_id, COLLECTION).state in (
        "delete_pending", "deleted")
    result = deletion.complete_deletion(
        TENANT, sub.document_id, COLLECTION, op_id=intent.op_id)
    assert result.state == "deleted"
    assert result.removed_points == 1
    assert result.purge_after
    assert not fake.points  # zero vectors verified

    # Force retention elapsed, then purge.
    with knowledge_repo.transaction(TENANT, privileged=True) as cur:
        cur.execute(
            "UPDATE knowledge.documents SET purge_after=now() - "
            "interval '1 day' WHERE tenant_id=%s AND document_id=%s",
            (TENANT, sub.document_id))
    dry = Purger(knowledge_repo).run(TENANT, apply=False)
    assert len(dry.eligible) == 1 and dry.purged == 0
    applied = Purger(knowledge_repo).run(TENANT, apply=True)
    assert applied.purged == 1
    with knowledge_repo.transaction(TENANT, privileged=True) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM knowledge.documents WHERE "
            "document_id=%s", (sub.document_id,))
        assert int(cur.fetchone()["n"]) == 0


def test_adopted_uncertain_cannot_be_auto_deleted(knowledge_repo, tmp_path):
    fake = FakeQdrant()
    _seed_point(fake, "9" * 32, doc_id="doc_unc")
    catalog = _catalog(tmp_path, [])
    AdoptionMigrator(
        knowledge_repo, qdrant_client=fake, catalog_path=catalog).run(
        TENANT, COLLECTION, apply=True)
    with knowledge_repo.transaction(TENANT, privileged=True) as cur:
        cur.execute(
            "SELECT document_id FROM knowledge.document_versions WHERE "
            "provenance='adopted_uncertain' LIMIT 1")
        row = cur.fetchone()
    assert row is not None
    deletion = DeletionService(knowledge_repo, qdrant_client=fake)
    with pytest.raises(ValueError):
        deletion.request_deletion(TENANT, row["document_id"], COLLECTION)


def test_purge_skips_uncertain_rows(knowledge_repo, tmp_path):
    fake = FakeQdrant()
    _seed_point(fake, "8" * 32, doc_id="doc_skip")
    catalog = _catalog(tmp_path, [])
    AdoptionMigrator(
        knowledge_repo, qdrant_client=fake, catalog_path=catalog).run(
        TENANT, COLLECTION, apply=True)
    with knowledge_repo.transaction(TENANT, privileged=True) as cur:
        cur.execute(
            "UPDATE knowledge.documents SET lifecycle_state='deleted', "
            "purge_after=now() - interval '1 day' WHERE tenant_id=%s AND "
            "document_id IN (SELECT document_id FROM "
            "knowledge.document_versions WHERE "
            "provenance='adopted_uncertain')", (TENANT,))
    report = Purger(knowledge_repo).run(TENANT, apply=True)
    assert report.purged == 0


# ---------------------------------------------------------------------------
# Operator status
# ---------------------------------------------------------------------------

def test_status_command_is_operator_only(knowledge_repo):
    payload = status_cmd(TENANT, repository=knowledge_repo)
    assert payload["ok"] is True
    assert "authority" in payload
    assert set(payload["authority"]) == {
        "state", "authoritative", "native_ingestion_available"}


# ---------------------------------------------------------------------------
# Adoption evidence layers 3 (KB registry) and 4 (durable jobs)
# ---------------------------------------------------------------------------

def test_adoption_kb_registry_mismatch_downgrades(knowledge_repo, tmp_path):
    fake = FakeQdrant()
    _seed_point(fake, "7" * 32, doc_id="doc_kb", kb_id="other")
    catalog = _catalog(tmp_path, [{
        "doc_id": "doc_kb", "kb_id": "other",
        "collection_name": COLLECTION,
        "content_hash": "sha256:" + "7" * 64,
        "source_paths": ["/data/kb.pdf"], "filename": "kb.pdf",
        "user_metadata": {}, "chunk_count": 1,
        "ingestion_status": "completed", "created_at": "x", "updated_at": "x",
    }])
    migrator = AdoptionMigrator(
        knowledge_repo, qdrant_client=fake, catalog_path=catalog)
    with knowledge_repo.transaction(TENANT, privileged=True) as cur:
        cur.execute(
            "INSERT INTO knowledge.knowledge_bases (tenant_id, kb_id, "
            "collection_name, name, provenance) VALUES (%s,'default',%s,"
            "'default','adopted') ON CONFLICT DO NOTHING",
            (TENANT, COLLECTION))
    report = migrator.run(TENANT, COLLECTION, apply=False)
    assert any(c["classification"] == "kb_registry_mismatch"
               for c in report.conflicts)
    assert any(u["legacy_doc_id"] == "doc_kb" for u in report.uncertain)


def test_adoption_job_correlation_upgrades_qdrant_only(
        knowledge_repo, tmp_path):
    from types import SimpleNamespace

    class _FakeJobs:
        def get_job_by_subject(self, *, tenant_id, job_type, subject_id):
            if subject_id == "doc_job_corr":
                return SimpleNamespace(
                    status=SimpleNamespace(value="succeeded"))
            return None

    fake = FakeQdrant()
    _seed_point(fake, "6" * 32, doc_id="doc_job_corr")
    catalog = _catalog(tmp_path, [])
    migrator = AdoptionMigrator(
        knowledge_repo, qdrant_client=fake, catalog_path=catalog)
    migrator._jobs = _FakeJobs()
    report = migrator.run(TENANT, COLLECTION, apply=False)
    assert any(v["legacy_doc_id"] == "doc_job_corr"
               for v in report.verified)


def test_adoption_resume_and_idempotency(knowledge_repo, tmp_path):
    fake = FakeQdrant()
    records = []
    for i in range(3):
        pid = f"{i + 1:032x}"
        _seed_point(fake, pid, doc_id=f"doc_resume_{i}")
        records.append({
            "doc_id": f"doc_resume_{i}", "kb_id": "default",
            "collection_name": COLLECTION,
            "content_hash": "sha256:" + f"{i + 5:064x}",
            "source_paths": [f"/data/r{i}.pdf"], "filename": f"r{i}.pdf",
            "user_metadata": {}, "chunk_count": 1,
            "ingestion_status": "completed", "created_at": "x",
            "updated_at": "x"})
    catalog = _catalog(tmp_path, records)
    migrator = AdoptionMigrator(
        knowledge_repo, qdrant_client=fake, catalog_path=catalog)
    seen = 0
    checkpoint = 0
    while checkpoint is not None:
        report = migrator.run(TENANT, COLLECTION, apply=True, batch=1,
                              checkpoint=checkpoint)
        seen += len(report.verified) + len(report.uncertain)
        checkpoint = report.next_checkpoint
    assert seen == 3
    # Idempotent rerun creates no duplicates.
    rerun = migrator.run(TENANT, COLLECTION, apply=True, batch=100)
    with knowledge_repo.transaction(TENANT) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM knowledge.document_versions WHERE "
            "tenant_id=%s AND chunk_id_seed LIKE 'doc_resume_%%'",
            (TENANT,))
        assert int(cur.fetchone()["n"]) == 3
