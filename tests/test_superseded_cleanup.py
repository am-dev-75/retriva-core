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

"""Spec 031 / ADR-036: superseded Qdrant-point cleanup + B1-B7 bounds.

Real PostgreSQL (knowledge_repo fixture) + FakeQdrant + injected clock and
latency sampler (deterministic; no arbitrary sleeps).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from retriva.knowledge.superseded_cleanup import (
    CleanupBounds, CleanupRefused, SupersededCleanup,
    candidate_fingerprint,
)
from tests.test_knowledge_qdrant import FakeQdrant, _Rec

COLL = "cust_test"


def _purge_ops(repo):
    with repo.transaction(None, privileged=True) as cur:
        cur.execute("DELETE FROM knowledge.qdrant_operations WHERE "
                    "op_type='delete_points'")


@pytest.fixture(autouse=True)
def _isolate_ops(knowledge_repo):
    _purge_ops(knowledge_repo)
    yield
    _purge_ops(knowledge_repo)


def _clock(dt):
    return lambda: dt


def _settings(repo):
    return repo._settings


def _seed(repo, *, superseded_days=40, status="superseded",
          provenance="native", n_points=1, serving=False,
          current="v2", with_manifest=True, tenant=None, current_self=False):
    """Seed a document with one superseded (or other) version + points."""
    tid = tenant or ("t-" + uuid.uuid4().hex[:8])
    vid = "v" + uuid.uuid4().hex[:20]
    doc = "d" + uuid.uuid4().hex[:20]
    src = "s" + uuid.uuid4().hex[:20]
    conn = psycopg2.connect(**_settings(repo).connection_kwargs("core"))
    conn.autocommit = True
    sup_sql = ("now() - make_interval(days => %s)" % int(superseded_days)) \
        if status == "superseded" else "NULL"
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.current_tenant',%s,false)",
                    (tid,))
        cur.execute("INSERT INTO knowledge.knowledge_bases "
                    "(tenant_id,kb_id,collection_name,config,lifecycle_state,"
                    "provenance,created_at,updated_at) VALUES "
                    "(%s,'default',%s,'{}','active','native',now(),now())",
                    (tid, COLL))
        cur.execute("INSERT INTO knowledge.sources (source_id,tenant_id,"
                    "source_type,namespace,normalized_ref,provenance,"
                    "lifecycle_state,safe_metadata,created_at,updated_at) "
                    "VALUES (%s,%s,'upload','upload','ref','native','active',"
                    "'{}',now(),now())", (src, tid))
        cur.execute("INSERT INTO knowledge.documents (document_id,tenant_id,"
                    "source_id,lifecycle_state,serving_generation,"
                    "user_metadata,created_at,updated_at,current_version_id) "
                    "VALUES (%s,%s,%s,'active',2,'{}',now(),now(),%s)",
                    (doc, tid, src, current))
        cur.execute(f"INSERT INTO knowledge.document_versions (version_id,"
                    "tenant_id,document_id,fingerprint_algorithm,"
                    "parser_contract_version,embedding_contract_version,"
                    "chunk_id_seed,chunk_contract_version,status,provenance,"
                    f"created_at,superseded_at) VALUES "
                    f"(%s,%s,%s,'sha256','v1','v1',%s,'v1',%s,%s,now(),{sup_sql})",
                    (vid, tid, doc, uuid.uuid4().hex, status, provenance))
        if with_manifest:
            for i in range(n_points):
                cur.execute("INSERT INTO knowledge.version_chunks (tenant_id,"
                            "version_id,chunk_ordinal,point_id,"
                            "chunk_contract_version,sync_state) VALUES "
                            "(%s,%s,%s,%s,'v1','verified')",
                            (tid, vid, i, str(uuid.uuid4())))
        cur.execute("INSERT INTO knowledge.kb_memberships (tenant_id,"
                    "document_id,kb_id,collection_name,added_at) VALUES "
                    "(%s,%s,'default',%s,now())", (tid, doc, COLL))
        if current_self:
            cur.execute("UPDATE knowledge.documents SET current_version_id=%s "
                        "WHERE tenant_id=%s AND document_id=%s", (vid, tid, doc))
    conn.close()
    return tid, doc, vid


def _fake_for(repo, vid, *, n_points=1, serving=False, tenant=None):
    fake = FakeQdrant()
    conn = psycopg2.connect(**_settings(repo).connection_kwargs("core"))
    conn.autocommit = True
    with conn.cursor() as cur:
        if tenant:
            cur.execute("SELECT set_config('app.current_tenant',%s,false)",
                        (tenant,))
        cur.execute("SELECT point_id FROM knowledge.version_chunks WHERE "
                    "version_id=%s ORDER BY chunk_ordinal", (vid,))
        ids = [r[0] for r in cur.fetchall()]
    conn.close()
    for pid in ids:
        fake.points[pid] = _Rec(pid, {
            "tenant_id": tenant, "document_id": "doc", "version_id": vid,
            "serving": serving, "provenance_class": "native"})
    return fake


def _svc(repo, fake, **kw):
    kw.setdefault("collection_name_resolver", lambda: COLL)
    return SupersededCleanup(repo, qdrant_client=fake, **kw)


WINDOW = dict(window_start="00:00", window_end="23:59", window_tz="UTC")
NOON = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


# -- eligibility -------------------------------------------------------------

def test_superseded_after_retention_is_eligible(knowledge_repo):
    tid, doc, vid = _seed(knowledge_repo)
    fake = _fake_for(knowledge_repo, vid, tenant=tid)
    rep = _svc(knowledge_repo, fake).dry_run(tid)
    assert rep["eligible_versions"] == 1 and rep["eligible_points"] == 1


def test_young_superseded_is_not_eligible(knowledge_repo):
    tid, doc, vid = _seed(knowledge_repo, superseded_days=5)
    fake = _fake_for(knowledge_repo, vid, tenant=tid)
    rep = _svc(knowledge_repo, fake).dry_run(tid)
    assert rep["eligible_versions"] == 0


def test_current_version_never_eligible(knowledge_repo):
    tid, doc, vid = _seed(knowledge_repo, status="superseded",
                          current_self=True)
    fake = _fake_for(knowledge_repo, vid, tenant=tid)
    rep = _svc(knowledge_repo, fake).dry_run(tid)
    assert rep["eligible_versions"] == 0  # current version is never eligible


def test_adopted_uncertain_retained(knowledge_repo):
    tid, doc, vid = _seed(knowledge_repo, provenance="adopted_uncertain")
    fake = _fake_for(knowledge_repo, vid, tenant=tid)
    rep = _svc(knowledge_repo, fake).dry_run(tid)
    assert rep["eligible_versions"] == 0
    assert rep["blocked_by_reason"].get("uncertain_provenance") == 1


def test_missing_manifest_blocked(knowledge_repo):
    tid, doc, vid = _seed(knowledge_repo, with_manifest=False)
    fake = _fake_for(knowledge_repo, vid, tenant=tid)
    rep = _svc(knowledge_repo, fake).dry_run(tid)
    assert rep["blocked_by_reason"].get("missing_manifest") == 1


def test_superseded_but_serving_is_integrity_incident(knowledge_repo):
    tid, doc, vid = _seed(knowledge_repo)
    fake = _fake_for(knowledge_repo, vid, serving=True, tenant=tid)
    rep = _svc(knowledge_repo, fake).dry_run(tid)
    assert rep["eligible_versions"] == 0
    assert rep["blocked_by_reason"].get("superseded_but_serving") == 1


# -- B1 / B2 -----------------------------------------------------------------

def test_b1_batches_100_and_remainder():
    from retriva.knowledge.superseded_cleanup import Candidate, _batches
    cands = [Candidate(tid_, "d", f"v{i}", None, "native", ["p"])
             for i, tid_ in enumerate(["t"] * 101)]
    assert _batches(cands, CleanupBounds()) == [100, 1]


def test_b2_oversized_version_fails_closed(knowledge_repo):
    tid, doc, vid = _seed(knowledge_repo, n_points=2001)
    fake = _fake_for(knowledge_repo, vid, tenant=tid)
    rep = _svc(knowledge_repo, fake).dry_run(tid)
    assert rep["eligible_versions"] == 0
    assert rep["blocked_by_reason"].get("version_exceeds_max_points") == 1


# -- B3 verification / ambiguity ---------------------------------------------

def test_b3_zero_points_verifies(knowledge_repo):
    tid, doc, vid = _seed(knowledge_repo)
    fake = _fake_for(knowledge_repo, vid, tenant=tid)
    svc = _svc(knowledge_repo, fake, bounds=CleanupBounds(**WINDOW),
               clock=_clock(NOON))
    rep = svc.dry_run(tid)
    out = svc.apply(tid, operator="op", authorization="op",
                    candidate_fingerprint_expected=rep["candidate_fingerprint"])
    assert out["applied"][0]["state"] == "verified"
    assert fake.points == {}


def test_b3_points_remaining_is_reconciliation_required(knowledge_repo):
    tid, doc, vid = _seed(knowledge_repo)
    fake = _fake_for(knowledge_repo, vid, tenant=tid)

    class Stuck(FakeQdrant):
        def delete(self, **kw):
            return None  # simulate delete not applied

    stuck = Stuck()
    stuck.points = dict(fake.points)
    svc = _svc(knowledge_repo, stuck, bounds=CleanupBounds(**WINDOW),
               clock=_clock(NOON))
    rep = svc.dry_run(tid)
    out = svc.apply(tid, operator="op", authorization="op",
                    candidate_fingerprint_expected=rep["candidate_fingerprint"])
    assert out["applied"][0]["state"] == "reconciliation_required"


# -- B4 / B5 -----------------------------------------------------------------

def _insert_op(repo, *, state, executed=False):
    conn = psycopg2.connect(**_settings(repo).connection_kwargs("core"))
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.current_tenant','t',false)")
        cur.execute("INSERT INTO knowledge.qdrant_operations (op_id,"
                    "tenant_id,op_type,collection_name,batch_no,batch_count,"
                    "op_state,attempt_no,prepared_at,executed_at) VALUES "
                    "(%s,'t','delete_points',%s,0,1,%s,1,now(),"
                    + ("now()" if executed else "NULL") + ")",
                    (uuid.uuid4().hex, COLL, state))
    conn.close()


def test_b4_rate_limit_blocks_second_issue(knowledge_repo):
    tid, doc, vid = _seed(knowledge_repo)
    fake = _fake_for(knowledge_repo, vid, tenant=tid)
    _insert_op(knowledge_repo, state="verified", executed=True)
    svc = _svc(knowledge_repo, fake, bounds=CleanupBounds(**WINDOW),
               clock=_clock(NOON))
    rep = svc.dry_run(tid)
    with pytest.raises(CleanupRefused) as e:
        svc.apply(tid, operator="op", authorization="op",
                  candidate_fingerprint_expected=rep["candidate_fingerprint"])
    assert e.value.code == "rate_limited"


def test_b5_global_concurrency_blocks(knowledge_repo):
    tid, doc, vid = _seed(knowledge_repo)
    fake = _fake_for(knowledge_repo, vid, tenant=tid)
    _insert_op(knowledge_repo, state="executing")
    svc = _svc(knowledge_repo, fake, bounds=CleanupBounds(**WINDOW),
               clock=_clock(NOON))
    rep = svc.dry_run(tid)
    with pytest.raises(CleanupRefused) as e:
        svc.apply(tid, operator="op", authorization="op",
                  candidate_fingerprint_expected=rep["candidate_fingerprint"])
    assert e.value.code == "concurrency_limit"


# -- B6 window ---------------------------------------------------------------

def test_b6_unconfigured_window_refuses(knowledge_repo):
    tid, doc, vid = _seed(knowledge_repo)
    fake = _fake_for(knowledge_repo, vid, tenant=tid)
    svc = _svc(knowledge_repo, fake, bounds=CleanupBounds(),
               clock=_clock(NOON))
    rep = svc.dry_run(tid)
    with pytest.raises(CleanupRefused) as e:
        svc.apply(tid, operator="op", authorization="op",
                  candidate_fingerprint_expected=rep["candidate_fingerprint"])
    assert e.value.code == "window_unconfigured"


def test_b6_outside_window_refuses_but_dry_run_works(knowledge_repo):
    tid, doc, vid = _seed(knowledge_repo)
    fake = _fake_for(knowledge_repo, vid, tenant=tid)
    b = CleanupBounds(window_start="01:00", window_end="02:00",
                      window_tz="UTC")
    svc = _svc(knowledge_repo, fake, bounds=b, clock=_clock(NOON))
    rep = svc.dry_run(tid)  # dry-run allowed outside window
    assert rep["eligible_versions"] == 1
    with pytest.raises(CleanupRefused) as e:
        svc.apply(tid, operator="op", authorization="op",
                  candidate_fingerprint_expected=rep["candidate_fingerprint"])
    assert e.value.code == "outside_maintenance_window"


# -- B7 stop conditions ------------------------------------------------------

def test_b7_backlog_or_p95_and_operator_required(knowledge_repo):
    tid, doc, vid = _seed(knowledge_repo)
    fake = _fake_for(knowledge_repo, vid, tenant=tid)
    # operator authorization required
    svc = _svc(knowledge_repo, fake, bounds=CleanupBounds(**WINDOW),
               clock=_clock(NOON))
    rep = svc.dry_run(tid)
    with pytest.raises(CleanupRefused) as e:
        svc.apply(tid, operator="", authorization="",
                  candidate_fingerprint_expected=rep["candidate_fingerprint"])
    assert e.value.code == "operator_required"
    # B7 backlog threshold
    b = CleanupBounds(stop_backlog_points=0, **WINDOW)
    svc2 = _svc(knowledge_repo, fake, bounds=b, clock=_clock(NOON))
    rep2 = svc2.dry_run(tid)
    with pytest.raises(CleanupRefused) as e2:
        svc2.apply(tid, operator="op", authorization="op",
                   candidate_fingerprint_expected=rep2["candidate_fingerprint"])
    assert e2.value.code == "stop_backlog_exceeded"
    # B7 p95 exceeds factor
    samples = [10.0] * 30
    b2 = CleanupBounds(baseline_p95_ms=1.0, min_latency_samples=20, **WINDOW)
    svc3 = _svc(knowledge_repo, fake, bounds=b2, clock=_clock(NOON),
                latency_sampler=lambda: samples)
    rep3 = svc3.dry_run(tid)
    with pytest.raises(CleanupRefused) as e3:
        svc3.apply(tid, operator="op", authorization="op",
                   candidate_fingerprint_expected=rep3["candidate_fingerprint"])
    assert e3.value.code == "stop_p95_exceeded"


def test_b7_missing_baseline_fails_closed(knowledge_repo):
    tid, doc, vid = _seed(knowledge_repo)
    fake = _fake_for(knowledge_repo, vid, tenant=tid)
    b = CleanupBounds(baseline_p95_ms=None, **WINDOW)
    svc = _svc(knowledge_repo, fake, bounds=b, clock=_clock(NOON),
               latency_sampler=lambda: [5.0] * 30)
    rep = svc.dry_run(tid)
    with pytest.raises(CleanupRefused) as e:
        svc.apply(tid, operator="op", authorization="op",
                  candidate_fingerprint_expected=rep["candidate_fingerprint"])
    assert e.value.code == "latency_baseline_missing"


# -- dry-run non-mutation + fingerprint --------------------------------------

def test_dry_run_is_non_mutating(knowledge_repo):
    tid, doc, vid = _seed(knowledge_repo)
    fake = _fake_for(knowledge_repo, vid, tenant=tid)
    svc = _svc(knowledge_repo, fake, bounds=CleanupBounds(**WINDOW),
               clock=_clock(NOON))
    before_points = dict(fake.points)
    svc.dry_run(tid)
    assert fake.points == before_points
    conn = psycopg2.connect(**_settings(knowledge_repo).connection_kwargs(
        "core"))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM knowledge.qdrant_operations WHERE "
                    "op_type='delete_points'")
        assert cur.fetchone()[0] == 0
    conn.close()


def test_changed_fingerprint_blocks_apply(knowledge_repo):
    tid, doc, vid = _seed(knowledge_repo)
    fake = _fake_for(knowledge_repo, vid, tenant=tid)
    svc = _svc(knowledge_repo, fake, bounds=CleanupBounds(**WINDOW),
               clock=_clock(NOON))
    with pytest.raises(CleanupRefused) as e:
        svc.apply(tid, operator="op", authorization="op",
                  candidate_fingerprint_expected="deadbeef")
    assert e.value.code == "candidate_changed"