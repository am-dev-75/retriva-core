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

"""Spec 028 §7/§11: automated Qdrant visible-point cutover gate."""

from __future__ import annotations

import json

import pytest
from qdrant_client import models

from retriva.knowledge.authority import (
    AuthorityError, AuthorityState, KnowledgeAuthority,
)
from retriva.knowledge.scan import scan_visible_points
from retriva.knowledge.visibility import with_serving_clause
from test_knowledge_qdrant import FakeQdrant

TENANT = "tenant-scan"
COLL = "retriva_chunks"
FULL = {
    "tenant_id": TENANT, "document_id": "d1", "version_id": "v1",
    "serving": True, "kb_ids": ["kbA"], "provenance_class": "native",
}


def _pt(fake, pid, payload):
    fake.upsert(collection_name=COLL, points=[
        models.PointStruct(id=pid, vector=[0.0] * 4, payload=payload)])


def test_empty_collection_ok():
    scan = scan_visible_points(FakeQdrant(), COLL, tenant_id=TENANT)
    assert scan.complete and scan.inspected == 0 and scan.ok


def test_one_complete_visible_point_ok():
    fake = FakeQdrant()
    _pt(fake, "a" * 32, dict(FULL))
    scan = scan_visible_points(fake, COLL, tenant_id=TENANT)
    assert scan.ok and scan.visible == 1


def test_multiple_pages_counts():
    fake = FakeQdrant()
    for i in range(5):
        _pt(fake, f"{i:032x}", dict(FULL, document_id=f"d{i}"))
    scan = scan_visible_points(fake, COLL, tenant_id=TENANT, batch=1)
    assert scan.inspected == 5 and scan.visible == 5 and scan.ok


@pytest.mark.parametrize("field", ["tenant_id", "document_id",
                                   "version_id", "serving", "kb_ids",
                                   "provenance_class"])
def test_missing_field_is_incomplete(field):
    fake = FakeQdrant()
    payload = dict(FULL)
    payload.pop(field)
    _pt(fake, "b" * 32, payload)
    scan = scan_visible_points(fake, COLL, tenant_id=TENANT)
    assert scan.incomplete == 1 and any(field in k for k in scan.field_problems)


def test_malformed_field_types():
    fake = FakeQdrant()
    _pt(fake, "c" * 32, dict(FULL, tenant_id=123, kb_ids="notalist"))
    scan = scan_visible_points(fake, COLL, tenant_id=TENANT)
    assert scan.incomplete == 1


def test_staging_superseded_stale_not_required():
    fake = FakeQdrant()
    _pt(fake, "d" * 32, {"serving": False})  # staging / superseded / stale
    scan = scan_visible_points(fake, COLL, tenant_id=TENANT)
    assert scan.inspected == 1 and scan.visible == 0 and scan.ok


def test_legacy_missing_and_orphan_block():
    fake = FakeQdrant()
    _pt(fake, "e" * 32, {"doc_id": "legacy"})   # legacy, no fields
    _pt(fake, "f" * 32, {"text": "orphan"})     # orphan, no serving
    scan = scan_visible_points(fake, COLL, tenant_id=TENANT)
    assert scan.incomplete == 2 and not scan.ok


def test_conflict_unknown_kb():
    fake = FakeQdrant()
    _pt(fake, "1" * 32, dict(FULL, kb_ids=["kbUNKNOWN"]))
    scan = scan_visible_points(fake, COLL, tenant_id=TENANT,
                               known_kbs=["kbA"])
    assert scan.conflicts == 1 and not scan.ok


def test_interruption_resume():
    fake = FakeQdrant()
    for i in range(4):
        _pt(fake, f"{i:032x}", dict(FULL, document_id=f"d{i}"))
    first = scan_visible_points(fake, COLL, tenant_id=TENANT, batch=2,
                                max_batches=1)
    assert not first.complete and first.next_offset is not None
    rest = scan_visible_points(fake, COLL, tenant_id=TENANT, batch=2,
                               start_offset=first.next_offset)
    assert rest.complete and (first.inspected + rest.inspected) == 4


def test_scan_does_not_mutate_qdrant():
    fake = FakeQdrant()
    _pt(fake, "a" * 32, dict(FULL))
    _pt(fake, "b" * 32, {"serving": False})
    before = {k: dict(v.payload) for k, v in fake.points.items()}
    scan_visible_points(fake, COLL, tenant_id=TENANT)
    after = {k: dict(v.payload) for k, v in fake.points.items()}
    assert before == after


def test_bounded_output_no_text_leak():
    fake = FakeQdrant()
    _pt(fake, "a" * 32, dict(FULL, text="SECRET DOCUMENT BODY"))
    summary = scan_visible_points(fake, COLL, tenant_id=TENANT).to_summary()
    assert "SECRET" not in json.dumps(summary)


def test_authoritative_mode_excludes_missing_tenant():
    fake = FakeQdrant()
    _pt(fake, "a" * 32, dict(FULL))
    _pt(fake, "b" * 32, {"serving": True, "document_id": "d",
                         "version_id": "v", "kb_ids": ["kbA"],
                         "provenance_class": "native"})  # no tenant_id
    compat = scan_visible_points(fake, COLL, tenant_id=TENANT)
    auth = scan_visible_points(fake, COLL, tenant_id=TENANT,
                               authoritative=True)
    assert auth.visible == 1 and auth.incomplete == 0
    assert compat.visible == 2  # compatibility sees the unmarked tenant


# -- authority integration -------------------------------------------------

def _authority(knowledge_repo):
    return KnowledgeAuthority(knowledge_repo)


def test_compute_scan_persists_verified_evidence(knowledge_repo):
    fake = FakeQdrant()
    _pt(fake, "a" * 32, dict(FULL))
    authority = _authority(knowledge_repo)
    op_id, scan = authority.compute_cutover_scan(
        tenant_id=TENANT, collection_name=COLL, client=fake,
        operator="ops", known_kbs=["kbA"])
    assert scan.ok
    op = knowledge_repo.get_operation_privileged(op_id)
    assert op["op_type"] == "adopt_verify" and op["op_state"] == "verified"
    assert json.loads(op["target_summary"])["kind"] == "visible_point_scan"


def test_cutover_rejected_on_incomplete_visible_point(knowledge_repo):
    fake = FakeQdrant()
    _pt(fake, "e" * 32, {"doc_id": "legacy"})
    authority = _authority(knowledge_repo)
    op_id, scan = authority.compute_cutover_scan(
        tenant_id=TENANT, collection_name=COLL, client=fake,
        operator="ops")
    assert not scan.ok
    with knowledge_repo.transaction(privileged=True) as cur:
        cur.execute("UPDATE knowledge.authority SET state='schema_ready', "
                    "authoritative=FALSE, native_ingestion_available=FALSE "
                    "WHERE singleton=TRUE")
    authority.transition(AuthorityState.ADOPTION_PENDING, operator="ops")
    authority.transition(AuthorityState.ADOPTION_VERIFIED, operator="ops")
    gates = {n: True for n in authority.cutover_gates({})}
    before = authority.read_state()
    with pytest.raises(AuthorityError):
        authority.set_authoritative(
            operator="ops", evidence=gates, equivalence_op_id=op_id,
            target_collection=COLL, tenant_id=TENANT)
    assert authority.read_state() == before
    # no serving mutation from the rejected command
    assert fake.points["e" * 32].payload.get("serving") is None


def test_cutover_success_after_complete_scan(knowledge_repo):
    fake = FakeQdrant()
    _pt(fake, "a" * 32, dict(FULL))
    authority = _authority(knowledge_repo)
    with knowledge_repo.transaction(privileged=True) as cur:
        cur.execute("UPDATE knowledge.authority SET state='schema_ready', "
                    "authoritative=FALSE, native_ingestion_available=FALSE "
                    "WHERE singleton=TRUE")
    authority.transition(AuthorityState.ADOPTION_PENDING, operator="ops")
    authority.transition(AuthorityState.ADOPTION_VERIFIED, operator="ops")
    op_id, scan = authority.compute_cutover_scan(
        tenant_id=TENANT, collection_name=COLL, client=fake,
        operator="ops", known_kbs=["kbA"])
    assert scan.ok
    gates = {n: True for n in authority.cutover_gates({})}
    row = authority.set_authoritative(
        operator="ops", evidence=gates, equivalence_op_id=op_id,
        target_collection=COLL, tenant_id=TENANT)
    assert row["state"] == "authoritative"
    authority.transition(AuthorityState.SUSPENDED, operator="ops")


def test_operator_boolean_without_computed_scan_rejected(knowledge_repo):
    authority = _authority(knowledge_repo)
    with knowledge_repo.transaction(privileged=True) as cur:
        cur.execute("UPDATE knowledge.authority SET state='schema_ready', "
                    "authoritative=FALSE, native_ingestion_available=FALSE "
                    "WHERE singleton=TRUE")
    authority.transition(AuthorityState.ADOPTION_PENDING, operator="ops")
    authority.transition(AuthorityState.ADOPTION_VERIFIED, operator="ops")
    with knowledge_repo.transaction(TENANT, privileged=True) as cur:
        bare = knowledge_repo.record_operation(
            cur, tenant_id=TENANT, op_type="adopt_verify",
            collection_name=COLL, op_state="verified")
    gates = {n: True for n in authority.cutover_gates({})}
    with pytest.raises(AuthorityError):
        authority.set_authoritative(
            operator="ops", evidence=gates, equivalence_op_id=bare,
            target_collection=COLL, tenant_id=TENANT)


def test_collection_and_tenant_scope_mismatch_rejected(knowledge_repo):
    fake = FakeQdrant()
    _pt(fake, "a" * 32, dict(FULL))
    authority = _authority(knowledge_repo)
    op_id, _ = authority.compute_cutover_scan(
        tenant_id=TENANT, collection_name=COLL, client=fake,
        operator="ops", known_kbs=["kbA"])
    with knowledge_repo.transaction(privileged=True) as cur:
        cur.execute("UPDATE knowledge.authority SET state='adoption_verified', "
                    "authoritative=FALSE, native_ingestion_available=FALSE "
                    "WHERE singleton=TRUE")
    gates = {n: True for n in authority.cutover_gates({})}
    with pytest.raises(AuthorityError):
        authority.set_authoritative(
            operator="ops", evidence=gates, equivalence_op_id=op_id,
            target_collection="other_collection", tenant_id=TENANT)
    with pytest.raises(AuthorityError):
        authority.set_authoritative(
            operator="ops", evidence=gates, equivalence_op_id=op_id,
            target_collection=COLL, tenant_id="other-tenant")


def test_stale_scan_evidence_rejected(knowledge_repo):
    fake = FakeQdrant()
    _pt(fake, "a" * 32, dict(FULL))
    authority = _authority(knowledge_repo)
    op_id, _ = authority.compute_cutover_scan(
        tenant_id=TENANT, collection_name=COLL, client=fake,
        operator="ops", known_kbs=["kbA"])
    with knowledge_repo.transaction(privileged=True) as cur:
        cur.execute("UPDATE knowledge.authority SET state='adoption_verified', "
                    "authoritative=FALSE, native_ingestion_available=FALSE "
                    "WHERE singleton=TRUE")
    gates = {n: True for n in authority.cutover_gates({})}
    with pytest.raises(AuthorityError):
        authority.set_authoritative(
            operator="ops", evidence=gates, equivalence_op_id=op_id,
            target_collection=COLL, tenant_id=TENANT,
            max_scan_age_seconds=-1)
