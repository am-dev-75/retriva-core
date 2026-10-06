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

"""Spec 028 P4: runtime pipeline integration — submission, staged
serving, verified promotion, prior deactivation, crash window, and
fail-closed gating (real PostgreSQL + deterministic Qdrant double)."""

from __future__ import annotations

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from qdrant_client import models  # noqa: E402

from retriva.knowledge.authority import (  # noqa: E402
    AuthorityState,
    KnowledgeAuthority,
)
from retriva.knowledge.pipeline import (  # noqa: E402
    KnowledgePipeline,
    KnowledgeIngestionUnavailable,
    reset_pipeline,
)
from retriva.knowledge.visibility import (  # noqa: E402
    set_serving,
    with_serving_clause,
)
from test_knowledge_qdrant import FakeQdrant  # noqa: E402

TENANT = "tenant-pipe"
COLL = "retriva_chunks"


def _make_authoritative(authority: KnowledgeAuthority,
                        equivalence_op_id: str) -> None:
    authority.transition(AuthorityState.ADOPTION_PENDING, operator="ops")
    authority.transition(AuthorityState.ADOPTION_VERIFIED, operator="ops")
    authority.set_authoritative(
        operator="ops",
        evidence={n: True for n in authority.cutover_gates({})},
        equivalence_op_id=equivalence_op_id)


@pytest.fixture()
def authoritative(knowledge_repo, equivalence_evidence):
    authority = KnowledgeAuthority(knowledge_repo)
    with knowledge_repo.transaction(privileged=True) as cur:
        cur.execute(
            "UPDATE knowledge.authority SET state='schema_ready', "
            "authoritative=FALSE, native_ingestion_available=FALSE, "
            "adoption_run_ref=NULL WHERE singleton=TRUE")
    _make_authoritative(authority, equivalence_evidence)
    reset_pipeline()
    yield authority
    with knowledge_repo.transaction(privileged=True) as cur:
        cur.execute(
            "UPDATE knowledge.authority SET state='schema_ready', "
            "authoritative=FALSE, native_ingestion_available=FALSE "
            "WHERE singleton=TRUE")
    reset_pipeline()


def _upsert(fake, pipe, ctx, point_ids):
    fake.upsert(collection_name=COLL, points=[
        models.PointStruct(
            id=pid, vector=[0.1, 0.2, 0.3, 0.4],
            payload={**pipe.visibility_fields(ctx, serving=False),
                     "doc_id": "legacy-{}".format(i), "text": "x"})
        for i, pid in enumerate(point_ids)])


def test_pipeline_end_to_end_promotion(knowledge_repo, authoritative):
    fake = FakeQdrant()
    pipe = KnowledgePipeline(knowledge_repo,
                             client_factory=lambda: fake)
    assert pipe.enabled() is True

    ctx = pipe.begin_upload(
        tenant_id=TENANT, kb_id="default", source_path="/srv/a.pdf",
        filename="a.pdf", collection_name=COLL, job_id="job-p1",
        content_fingerprint="sha256:" + "a" * 64)
    assert ctx is not None
    got = pipe.context_for_job(TENANT, "job-p1", "v2_upload")
    assert got is not None and got.version_id == ctx.version_id

    ids = [f"{i:032x}" for i in range(3)]
    pipe.record_intent(ctx, ids)
    _upsert(fake, pipe, ctx, ids)
    pipe.mark_applied(ctx)
    assert pipe.complete(ctx, observed_chunk_count=3) is True

    # All new points serving; document current version promoted.
    assert all(fake.points[i].payload["serving"] is True for i in ids)
    from retriva.knowledge.repository import KnowledgeRepository
    repo = KnowledgeRepository(knowledge_repo._settings)
    with repo.transaction(TENANT) as c:
        assert repo.get_document(
            c, tenant_id=TENANT,
            document_id=ctx.document_id)["current_version_id"] == \
            ctx.version_id


def test_replacement_deactivates_prior(knowledge_repo, authoritative):
    fake = FakeQdrant()
    pipe = KnowledgePipeline(knowledge_repo,
                             client_factory=lambda: fake)
    v1 = pipe.begin_upload(
        tenant_id=TENANT, kb_id="default", source_path="/srv/b.pdf",
        filename="b.pdf", collection_name=COLL, job_id="job-b1",
        content_fingerprint="sha256:" + "b" * 64)
    ids1 = [f"1{i:031x}" for i in range(2)]
    pipe.record_intent(v1, ids1)
    _upsert(fake, pipe, v1, ids1)
    pipe.mark_applied(v1)
    assert pipe.complete(v1, observed_chunk_count=2) is True

    v2 = pipe.begin_upload(
        tenant_id=TENANT, kb_id="default", source_path="/srv/b.pdf",
        filename="b.pdf", collection_name=COLL, job_id="job-b2",
        content_fingerprint="sha256:" + "c" * 64)
    assert v2.document_id == v1.document_id
    assert v2.version_id != v1.version_id
    ids2 = [f"2{i:031x}" for i in range(3)]
    pipe.record_intent(v2, ids2)
    _upsert(fake, pipe, v2, ids2)
    pipe.mark_applied(v2)
    assert pipe.complete(v2, observed_chunk_count=3) is True

    assert all(fake.points[i].payload["serving"] is True for i in ids2)
    assert all(fake.points[i].payload["serving"] is False for i in ids1)


def test_crash_window_leaves_replacement_hidden(
        knowledge_repo, authoritative):
    fake = FakeQdrant()
    pipe = KnowledgePipeline(knowledge_repo,
                             client_factory=lambda: fake)
    v1 = pipe.begin_upload(
        tenant_id=TENANT, kb_id="default", source_path="/srv/c.pdf",
        filename="c.pdf", collection_name=COLL, job_id="job-c1",
        content_fingerprint="sha256:" + "d" * 64)
    ids1 = [f"3{i:031x}" for i in range(1)]
    pipe.record_intent(v1, ids1)
    _upsert(fake, pipe, v1, ids1)
    pipe.mark_applied(v1)
    pipe.complete(v1, observed_chunk_count=1)

    # Replacement staged but the worker crashes after the Qdrant
    # upsert and before complete(): PG not promoted, points hidden.
    v2 = pipe.begin_upload(
        tenant_id=TENANT, kb_id="default", source_path="/srv/c.pdf",
        filename="c.pdf", collection_name=COLL, job_id="job-c2",
        content_fingerprint="sha256:" + "e" * 64)
    ids2 = [f"4{i:031x}" for i in range(1)]
    pipe.record_intent(v2, ids2)
    _upsert(fake, pipe, v2, ids2)
    # no mark_applied / complete
    clause = with_serving_clause(None)
    visible = [r.id for r in fake.points.values()
               if fake._match(r.payload, clause)]
    assert set(visible) == set(ids1)
    with knowledge_repo.transaction(TENANT) as cur:
        doc = knowledge_repo.get_document(
            cur, tenant_id=TENANT, document_id=v2.document_id)
    assert doc["current_version_id"] == v1.version_id


def test_gating_rejects_when_schema_present_not_authoritative(
        knowledge_repo):
    with knowledge_repo.transaction(privileged=True) as cur:
        cur.execute(
            "UPDATE knowledge.authority SET state='schema_ready', "
            "authoritative=FALSE, native_ingestion_available=FALSE "
            "WHERE singleton=TRUE")
    reset_pipeline()
    pipe = KnowledgePipeline(knowledge_repo, client_factory=FakeQdrant)
    assert pipe.schema_present() is True
    with pytest.raises(KnowledgeIngestionUnavailable):
        pipe.enabled()
