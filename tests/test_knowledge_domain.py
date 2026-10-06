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

"""Spec 028 P2/P6: knowledge domain, identity, service lifecycle,
promotion, deletion, authority gating, and manifest scale."""

from __future__ import annotations

import types

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from retriva.knowledge.authority import (  # noqa: E402
    AuthorityError,
    AuthorityState,
    KnowledgeAuthority,
)
from retriva.knowledge.domain import (  # noqa: E402
    IngestionSyncState,
    OpState,
    TransitionError,
    VersionStatus,
    can_transition_ingestion,
    can_transition_operation,
    can_transition_version,
)
from retriva.knowledge.ids import (  # noqa: E402
    SourceIdentityError,
    connector_identity,
    internal_identity,
    mediawiki_identity,
    upload_identity,
)
from retriva.knowledge.repository import KnowledgeRepository  # noqa: E402
from retriva.knowledge.service import KnowledgeService  # noqa: E402

TENANT = "tenant-a"
TENANT_B = "tenant-b"


@pytest.fixture(autouse=True)
def _reset_authority(knowledge_repo):
    """The authority row is a deployment-global singleton shared by the
    session-scoped scratch database; reset it before each test so
    authority tests are order-independent."""
    with knowledge_repo.transaction(privileged=True) as cur:
        cur.execute(
            "UPDATE knowledge.authority SET state='schema_ready', "
            "authoritative=FALSE, native_ingestion_available=FALSE, "
            "adoption_run_ref=NULL, catalog_frozen_at=NULL, "
            "sqlite_frozen_at=NULL WHERE singleton=TRUE")
    yield


# ---------------------------------------------------------------------------
# Domain state machine (no DB)
# ---------------------------------------------------------------------------

def test_version_transition_guards():
    assert can_transition_version("staging", "parsing")
    assert can_transition_version("indexing", "indexed")
    assert not can_transition_version("indexed", "parsing")
    assert not can_transition_version("retired", "indexed")
    assert can_transition_version("indexed", "indexed")


def test_ingestion_transition_guards_and_terminal():
    assert can_transition_ingestion("registered", "parsing")
    assert can_transition_ingestion("indexing", "indexed")
    assert not can_transition_ingestion("indexed", "indexing")
    assert not can_transition_ingestion("deleted", "indexed")
    assert can_transition_ingestion("registered", "registered")


def test_operation_transition_monotonic():
    assert can_transition_operation("prepared", "executing")
    assert can_transition_operation("applied_unverified", "verified")
    assert not can_transition_operation("verified", "prepared")
    assert can_transition_operation("failed", "reconciliation_required")


def test_source_identity_namespaces_and_normalization():
    up = upload_identity("default", "Report Q3.PDF")
    assert up.namespace == "upload"
    assert "report%20q3.pdf" in up.normalized_ref
    mw = mediawiki_identity("rdwiki", "12345")
    assert mw.normalized_ref == "rdwiki:page:12345"
    con = connector_identity("email-agent", "MSG-1")
    assert con.normalized_ref == "email-agent:msg-1"
    internal = internal_identity("crat-corpus-0007")
    assert internal.normalized_ref == "crat-corpus-0007"
    with pytest.raises(SourceIdentityError):
        mediawiki_identity("", "1")


# ---------------------------------------------------------------------------
# Service lifecycle against real PostgreSQL
# ---------------------------------------------------------------------------

def _submission(service, *, tenant=TENANT, kb="default", filename="a.pdf",
                fingerprint="sha256:" + "a" * 64, job_id="job-1",
                document_id=None):
    return service.register_submission(
        tenant_id=tenant, identity=upload_identity(kb, filename),
        kb_ids=[kb], collection_name="c", job_id=job_id,
        job_type="v2_upload", content_fingerprint=fingerprint,
        content_size=10, document_id=document_id)


def test_submission_creates_rows_and_membership(knowledge_repo):
    service = KnowledgeService(knowledge_repo)
    result = _submission(service)
    assert result.document_id and result.version_id and result.ingestion_id
    assert result.version_reused is False
    with knowledge_repo.transaction(TENANT) as cur:
        doc = knowledge_repo.get_document(
            cur, tenant_id=TENANT, document_id=result.document_id)
        assert doc["lifecycle_state"] == "active"
        members = knowledge_repo.list_memberships(
            cur, tenant_id=TENANT, document_id=result.document_id)
        assert members == ["default"]


def test_identical_resubmission_reuses_version(knowledge_repo):
    service = KnowledgeService(knowledge_repo)
    first = _submission(service, filename="b.pdf",
                        fingerprint="sha256:" + "b" * 64, job_id="job-b1")
    second = _submission(service, filename="b.pdf",
                         fingerprint="sha256:" + "b" * 64, job_id="job-b2")
    assert second.document_id == first.document_id
    assert second.version_id == first.version_id
    assert second.version_reused is True


def test_distinct_sources_same_content_are_distinct_documents(
        knowledge_repo):
    service = KnowledgeService(knowledge_repo)
    fp = "sha256:" + "c" * 64
    a = _submission(service, filename="one.pdf", fingerprint=fp,
                    job_id="job-c1")
    b = _submission(service, filename="two.pdf", fingerprint=fp,
                    job_id="job-c2")
    assert a.document_id != b.document_id


def test_changed_content_creates_new_version(knowledge_repo):
    service = KnowledgeService(knowledge_repo)
    first = _submission(service, filename="d.pdf",
                        fingerprint="sha256:" + "d" * 64, job_id="job-d1")
    second = _submission(service, filename="d.pdf",
                         fingerprint="sha256:" + "e" * 64, job_id="job-d2")
    assert second.document_id == first.document_id
    assert second.version_id != first.version_id


def test_manifest_batched_insert_and_completion_gate(knowledge_repo):
    service = KnowledgeService(knowledge_repo)
    sub = _submission(service, filename="f.pdf",
                      fingerprint="sha256:" + "f" * 64, job_id="job-f")
    rows = service.register_manifest(
        tenant_id=TENANT, version_id=sub.version_id,
        chunk_id_seed=f"{sub.document_id}:sha256:" + "f" * 64,
        chunk_count=250)
    assert len(rows) == 250
    # Not verified yet -> completion gate fails and marks partial.
    assert service.verify_version_complete(
        tenant_id=TENANT, version_id=sub.version_id) is False
    with knowledge_repo.transaction(TENANT) as cur:
        counts = knowledge_repo.count_chunks_by_state(
            cur, tenant_id=TENANT, version_id=sub.version_id)
    assert counts.get("expected") == 250


def test_promotion_and_failed_replacement_preserves_prior(
        knowledge_repo):
    service = KnowledgeService(knowledge_repo)
    v1 = _submission(service, filename="g.pdf",
                     fingerprint="sha256:" + "1" * 64, job_id="job-g1")
    service.register_manifest(
        tenant_id=TENANT, version_id=v1.version_id,
        chunk_id_seed=f"{v1.document_id}:sha256:" + "1" * 64,
        chunk_count=2)
    with knowledge_repo.transaction(TENANT) as cur:
        ords = [r["chunk_ordinal"] for r in knowledge_repo.list_chunk_states(
            cur, tenant_id=TENANT, version_id=v1.version_id)]
        knowledge_repo.set_chunks_sync_state(
            cur, tenant_id=TENANT, version_id=v1.version_id,
            ordinals=ords, sync_state="verified")
    assert service.finalize_verified(
        tenant_id=TENANT, document_id=v1.document_id,
        version_id=v1.version_id, ingestion_id=v1.ingestion_id,
        prior_version_id=None) is True
    assert service.current_version_id(
        tenant_id=TENANT, document_id=v1.document_id) == v1.version_id

    # A failed replacement must not change the current version.
    v2 = _submission(service, filename="g.pdf",
                     fingerprint="sha256:" + "2" * 64, job_id="job-g2")
    service.fail_ingestion(
        tenant_id=TENANT, ingestion_id=v2.ingestion_id,
        version_id=v2.version_id, error_code="parse_failed")
    assert service.current_version_id(
        tenant_id=TENANT, document_id=v1.document_id) == v1.version_id


def test_duplicate_completion_idempotent(knowledge_repo):
    service = KnowledgeService(knowledge_repo)
    v = _submission(service, filename="h.pdf",
                    fingerprint="sha256:" + "3" * 64, job_id="job-h")
    service.register_manifest(
        tenant_id=TENANT, version_id=v.version_id,
        chunk_id_seed=f"{v.document_id}:sha256:" + "3" * 64,
        chunk_count=1)
    with knowledge_repo.transaction(TENANT) as cur:
        knowledge_repo.set_chunks_sync_state(
            cur, tenant_id=TENANT, version_id=v.version_id,
            ordinals=[0], sync_state="verified")
    assert service.finalize_verified(
        tenant_id=TENANT, document_id=v.document_id,
        version_id=v.version_id, ingestion_id=v.ingestion_id,
        prior_version_id=None) is True
    # Second call is a no-op (guarded), still returns a bool.
    service.finalize_verified(
        tenant_id=TENANT, document_id=v.document_id,
        version_id=v.version_id, ingestion_id=v.ingestion_id,
        prior_version_id=None)


def test_late_callback_never_moves_state_backward(knowledge_repo):
    service = KnowledgeService(knowledge_repo)
    v = _submission(service, filename="i.pdf",
                    fingerprint="sha256:" + "4" * 64, job_id="job-i")
    service.mark_ingestion_state(
        tenant_id=TENANT, ingestion_id=v.ingestion_id,
        sync_state="indexed")
    with pytest.raises(Exception):
        service.mark_ingestion_state(
            tenant_id=TENANT, ingestion_id=v.ingestion_id,
            sync_state="parsing")


def test_cross_tenant_isolation_fails_closed(knowledge_repo):
    service = KnowledgeService(knowledge_repo)
    sub = _submission(service, tenant=TENANT, filename="j.pdf",
                      fingerprint="sha256:" + "5" * 64, job_id="job-j")
    with knowledge_repo.transaction(TENANT_B) as cur:
        assert knowledge_repo.get_document(
            cur, tenant_id=TENANT_B,
            document_id=sub.document_id) is None


def test_runtime_role_has_no_ddl(knowledge_database):
    conn = psycopg2.connect(
        **knowledge_database.connection_kwargs("core"))
    conn.autocommit = True
    try:
        with pytest.raises(psycopg2.Error):
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE knowledge.should_fail (x int)")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Authority gating
# ---------------------------------------------------------------------------

def test_authority_defaults_fail_closed(knowledge_repo):
    authority = KnowledgeAuthority(knowledge_repo)
    readiness = authority.public_readiness()
    assert readiness["state"] == "schema_ready"
    assert readiness["authoritative"] is False
    assert readiness["native_ingestion_available"] is False
    with pytest.raises(AuthorityError):
        authority.require_native_ingestion()


def test_authority_transition_requires_gates_and_is_privileged(
        knowledge_repo, equivalence_evidence):
    authority = KnowledgeAuthority(knowledge_repo)
    # Illegal jump refused.
    with pytest.raises(AuthorityError):
        authority.transition(AuthorityState.AUTHORITATIVE,
                             operator="ops", note="skip")
    # Cutover refused without all gates.
    with pytest.raises(AuthorityError):
        authority.set_authoritative(operator="ops", evidence={})
    # Legal path.
    authority.transition(AuthorityState.ADOPTION_PENDING,
                         operator="ops")
    authority.transition(AuthorityState.ADOPTION_VERIFIED,
                         operator="ops")
    gates = {name: True for name in authority.cutover_gates({})}
    updated = authority.set_authoritative(
        operator="ops", evidence=gates,
        equivalence_op_id=equivalence_evidence)
    assert updated["state"] == "authoritative"
    readiness = authority.public_readiness()
    assert readiness["authoritative"] is True
    assert readiness["native_ingestion_available"] is True
    authority.require_native_ingestion()  # no raise
    # Suspension preserves retrieval, stops native ingestion.
    authority.transition(AuthorityState.SUSPENDED, operator="ops")
    with pytest.raises(AuthorityError):
        authority.require_native_ingestion()
    # Restore for other tests.
    authority.transition(AuthorityState.AUTHORITATIVE, operator="ops")


def test_authority_runtime_role_cannot_write(knowledge_database):
    conn = psycopg2.connect(
        **knowledge_database.connection_kwargs("core"))
    conn.autocommit = True
    try:
        with pytest.raises(psycopg2.Error):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE knowledge.authority SET state='suspended'")
    finally:
        conn.close()


def test_manifest_scale_measurement(knowledge_repo):
    """Representative per-row/index size for one-manifest-row-per-point
    (Spec 028 §8).  Inserts a bounded sample and records real sizes."""
    service = KnowledgeService(knowledge_repo)
    sub = _submission(service, filename="scale.pdf",
                      fingerprint="sha256:" + "9" * 64,
                      job_id="job-scale")
    sample = 2000
    rows = [
        {"chunk_ordinal": i, "point_id": f"{i:032x}",
         "chunk_contract_version": "chunk1", "sync_state": "expected"}
        for i in range(sample)
    ]
    with knowledge_repo.transaction(TENANT) as cur:
        knowledge_repo.insert_version_chunks(
            cur, tenant_id=TENANT, version_id=sub.version_id, rows=rows)
    conn = psycopg2.connect(
        **knowledge_repo._settings.connection_kwargs("migrator"))
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.knowledge_privileged',"
                "'granted', false)")
            cur.execute(
                "SELECT pg_relation_size('knowledge.version_chunks')")
            heap = cur.fetchone()[0]
            cur.execute(
                "SELECT pg_indexes_size('knowledge.version_chunks')")
            idx = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM knowledge.version_chunks")
            n = cur.fetchone()[0]
        print(f"MEASURE manifest_rows={n} heap_bytes={heap} "
              f"index_bytes={idx} "
              f"per_row_with_index={((heap + idx) / max(1, n)):.1f}")
        assert heap / max(1, n) < 400
    finally:
        conn.close()
