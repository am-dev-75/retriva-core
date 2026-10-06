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

"""Spec 028 §20-22/§23/§25: authority-cutover simulation, legacy-store
freeze, retrieval equivalence, no relational N+1, and larger manifest
measurement."""

from __future__ import annotations

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from retriva.knowledge.authority import (  # noqa: E402
    AuthorityError,
    AuthorityState,
    KnowledgeAuthority,
)
from retriva.knowledge.kb_registry import (  # noqa: E402
    KBMigrationError,
    KnowledgeBaseRegistry,
)
from retriva.knowledge.visibility import (  # noqa: E402
    count_incomplete_visible_points,
    native_payload_fields,
    with_serving_clause,
)
from test_knowledge_qdrant import FakeQdrant, _Rec, _seed_point  # noqa: E402

TENANT = "tenant-sim"
COLLECTION = "retriva_chunks"


@pytest.fixture(autouse=True)
def _reset_authority(knowledge_repo):
    with knowledge_repo.transaction(privileged=True) as cur:
        cur.execute(
            "UPDATE knowledge.authority SET state='schema_ready', "
            "authoritative=FALSE, native_ingestion_available=FALSE, "
            "adoption_run_ref=NULL, catalog_frozen_at=NULL, "
            "sqlite_frozen_at=NULL WHERE singleton=TRUE")
    yield


def test_cutover_rejected_until_every_gate_passes(knowledge_repo,
                                                 equivalence_evidence):
    authority = KnowledgeAuthority(knowledge_repo)
    authority.transition(AuthorityState.ADOPTION_PENDING, operator="ops")
    authority.transition(AuthorityState.ADOPTION_VERIFIED, operator="ops")
    gates = {n: True for n in authority.cutover_gates({})}
    # One gate missing -> rejected.
    gates["retrieval_equivalence_passed"] = False
    with pytest.raises(AuthorityError):
        authority.set_authoritative(operator="ops", evidence=gates)
    # All gates -> durable cutover.
    gates["retrieval_equivalence_passed"] = True
    updated = authority.set_authoritative(
        operator="ops", evidence=gates, adoption_run_ref="run-1",
        equivalence_op_id=equivalence_evidence)
    assert updated["state"] == "authoritative"
    assert updated["authoritative"] is True
    assert updated["native_ingestion_available"] is True


def test_incomplete_visible_points_block_cutover(knowledge_repo):
    fake = FakeQdrant()
    fields = native_payload_fields(
        tenant_id=TENANT, document_id="d1", version_id="v1",
        kb_ids=["default"], serving=True)
    fake.points["ok"] = _Rec("ok", fields)
    fake.points["legacy"] = _Rec("legacy", {"doc_id": "x"})
    assert count_incomplete_visible_points(fake, COLLECTION) == 1
    fake.points["legacy"].payload.update(fields)
    assert count_incomplete_visible_points(fake, COLLECTION) == 0


def test_freeze_simulation_and_fallback_refusal(knowledge_repo,
                                               equivalence_evidence):
    from retriva.domain.kb import KBRegistry, KBConflictError

    try:
        KBRegistry().create(name="Sim Base", kb_id="sim-base",
                            collection_name="col_sim")
    except KBConflictError:
        pass
    authority = KnowledgeAuthority(knowledge_repo)
    authority.transition(
        AuthorityState.ADOPTION_PENDING, operator="ops",
        catalog_frozen=True, sqlite_frozen=True)
    row = None
    with knowledge_repo.transaction(privileged=True) as cur:
        cur.execute("SELECT catalog_frozen_at, sqlite_frozen_at FROM "
                    "knowledge.authority")
        row = cur.fetchone()
    assert row["catalog_frozen_at"] is not None
    assert row["sqlite_frozen_at"] is not None
    authority.transition(AuthorityState.ADOPTION_VERIFIED, operator="ops")
    authority.set_authoritative(
        operator="ops",
        evidence={n: True for n in authority.cutover_gates({})},
        equivalence_op_id=equivalence_evidence)
    accessor = KnowledgeBaseRegistry(knowledge_repo)
    assert accessor.mode() == "postgresql"
    with pytest.raises(KBMigrationError):
        accessor.refuse_legacy_write()


def test_suspension_stops_ingestion_preserves_retrieval(
        knowledge_repo, equivalence_evidence):
    authority = KnowledgeAuthority(knowledge_repo)
    authority.transition(AuthorityState.ADOPTION_PENDING, operator="ops")
    authority.transition(AuthorityState.ADOPTION_VERIFIED, operator="ops")
    authority.set_authoritative(
        operator="ops",
        evidence={n: True for n in authority.cutover_gates({})},
        equivalence_op_id=equivalence_evidence)
    authority.transition(AuthorityState.SUSPENDED, operator="ops")
    readiness = authority.public_readiness()
    assert readiness["authoritative"] is False
    assert readiness["native_ingestion_available"] is False
    with pytest.raises(AuthorityError):
        authority.require_native_ingestion()


def test_retrieval_equivalence_across_adoption(knowledge_repo):
    """Adoption is metadata-only: the set of retrievable point ids is
    unchanged and the serving filter keeps adopted points visible."""
    from retriva.indexing.qdrant_store import search_chunks

    fake = FakeQdrant()
    _seed_point(fake, "a" * 32, doc_id="doc_eq1")
    _seed_point(fake, "b" * 32, doc_id="doc_eq2")

    def ids():
        out = search_chunks(fake, query_vector=[0.1, 0.2, 0.3, 0.4],
                            retriever_top_k=10)
        # search_chunks returns payloads; use doc_id as stable identity
        return sorted(p.get("doc_id") for p in out)

    before = ids()
    # Simulate authoritative serving fields on existing points
    # (metadata-only; no vector rewrite).
    for pid, rec in fake.points.items():
        rec.payload["serving"] = True
        rec.payload["provenance_class"] = "adopted_verified"
        rec.payload["tenant_id"] = TENANT
    after = ids()
    assert before == after


def test_no_relational_n_plus_one_in_retrieval(knowledge_repo,
                                               monkeypatch):
    """Ordinary retrieval must not touch PostgreSQL per search/result."""
    from retriva.indexing.qdrant_store import search_chunks
    from retriva.knowledge.repository import KnowledgeRepository

    def _boom(*args, **kwargs):
        raise AssertionError(
            "retrieval must not open a PostgreSQL transaction")

    monkeypatch.setattr(KnowledgeRepository, "transaction", _boom)
    fake = FakeQdrant()
    _seed_point(fake, "a" * 32, doc_id="doc_n1")
    fake.points["a" * 32].payload["serving"] = True
    out = search_chunks(fake, query_vector=[0.1, 0.2, 0.3, 0.4],
                        retriever_top_k=5)
    assert out  # results returned with zero PG queries


@pytest.mark.parametrize("rows", [100_000])
def test_larger_manifest_measurement(knowledge_repo, rows):
    """Representative 100k-row manifest load: heap/index size and a
    reconciliation query plan (Spec 028 §8/§23)."""
    from retriva.knowledge.service import KnowledgeService

    service = KnowledgeService(knowledge_repo)
    sub = service.register_submission(
        tenant_id=TENANT,
        identity=__import__("retriva.knowledge.ids", fromlist=["x"])
        .upload_identity("default", "scale100k.pdf"),
        kb_ids=["default"], collection_name=COLLECTION, job_id="job-100k",
        job_type="v2_upload",
        content_fingerprint="sha256:" + "a" * 64)
    chunk = 10_000
    with knowledge_repo.transaction(TENANT) as cur:
        for start in range(0, rows, chunk):
            batch = [
                {"chunk_ordinal": i, "point_id": f"{i:032x}",
                 "chunk_contract_version": "chunk1",
                 "sync_state": "applied_unverified"}
                for i in range(start, min(start + chunk, rows))
            ]
            knowledge_repo.insert_version_chunks(
                cur, tenant_id=TENANT, version_id=sub.version_id,
                rows=batch)
    conn = psycopg2.connect(
        **knowledge_repo._settings.connection_kwargs("migrator"))
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.knowledge_privileged',"
                "'granted', false)")
            cur.execute("ANALYZE knowledge.version_chunks")
            cur.execute(
                "SELECT pg_relation_size('knowledge.version_chunks')")
            heap = cur.fetchone()[0]
            cur.execute(
                "SELECT pg_indexes_size('knowledge.version_chunks')")
            idx = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM knowledge.version_chunks "
                        "WHERE version_id=%s", (sub.version_id,))
            n = cur.fetchone()[0]
            cur.execute(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT "
                "count(*) FROM knowledge.version_chunks WHERE "
                "tenant_id=%s AND version_id=%s AND "
                "sync_state='applied_unverified'", (TENANT, sub.version_id))
            plan = cur.fetchone()[0]
        print(f"MEASURE100K rows={n} heap_bytes={heap} index_bytes={idx} "
              f"per_row_with_index={((heap + idx) / max(1, n)):.1f}")
        assert n == rows
        assert heap > 0 and idx > 0
    finally:
        conn.close()


def test_cutover_requires_durable_equivalence_evidence(
        knowledge_repo, equivalence_evidence):
    """A transient in-memory equivalence boolean is insufficient; the
    gate needs a durable verified adopt_verify operation."""
    authority = KnowledgeAuthority(knowledge_repo)
    authority.transition(AuthorityState.ADOPTION_PENDING, operator="ops")
    authority.transition(AuthorityState.ADOPTION_VERIFIED, operator="ops")
    gates = {n: True for n in authority.cutover_gates({})}
    # Equivalence asserted but no durable evidence id -> rejected.
    with pytest.raises(AuthorityError):
        authority.set_authoritative(operator="ops", evidence=gates)
    # A non-verified / non-adopt_verify op does not satisfy the gate.
    with knowledge_repo.transaction("spec028-equivalence",
                                    privileged=True) as cur:
        bad_op = knowledge_repo.record_operation(
            cur, tenant_id="spec028-equivalence",
            op_type="upsert_batch", collection_name="retriva_chunks",
            op_state="prepared")
    with pytest.raises(AuthorityError):
        authority.set_authoritative(
            operator="ops", evidence=gates, equivalence_op_id=bad_op)
    # The durable verified adopt_verify operation satisfies it.
    updated = authority.set_authoritative(
        operator="ops", evidence=gates,
        equivalence_op_id=equivalence_evidence)
    assert updated["state"] == "authoritative"
