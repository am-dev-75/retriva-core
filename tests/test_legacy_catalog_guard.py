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

"""Spec 028 correction: centralized refusal of legacy dedup-catalog
writes once PostgreSQL knowledge metadata is authoritative."""

from __future__ import annotations

import hashlib
import json
import os
import stat

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from retriva.domain.models import DocRecord  # noqa: E402
from retriva.ingestion.dedup import DeduplicationStore  # noqa: E402
from retriva.knowledge.legacy_guard import (  # noqa: E402
    invalidate_authority_state_cache,
    refusal_counts,
    reset_refusals,
)

FORBIDDEN_STATES = ("authoritative", "suspended",
                    "reconciliation_required")


def _set_state(knowledge_repo, state, *, invalidate=True):
    authoritative = state == "authoritative"
    with knowledge_repo.transaction(privileged=True) as cur:
        cur.execute(
            "UPDATE knowledge.authority SET state=%s, authoritative=%s, "
            "native_ingestion_available=%s WHERE singleton=TRUE",
            (state, authoritative, authoritative))
    if invalidate:
        invalidate_authority_state_cache()


@pytest.fixture(autouse=True)
def _reset(knowledge_repo, monkeypatch):
    import retriva.knowledge.legacy_guard as lg
    from retriva.knowledge.repository import KnowledgeRepository

    repo = KnowledgeRepository(knowledge_repo._settings)

    def _probe():
        try:
            with repo.transaction() as cur:
                cur.execute(
                    "SELECT to_regclass('knowledge.authority') IS NOT NULL "
                    "AS present")
                r = cur.fetchone()
                if not r or not r["present"]:
                    return "schema_ready"
                cur.execute("SELECT state FROM knowledge.authority "
                            "WHERE singleton=TRUE")
                a = cur.fetchone()
                return str(a["state"]) if a else None
        except Exception:
            return "schema_ready"

    monkeypatch.setattr(lg, "_probe_authority_state", _probe)
    lg.invalidate_authority_state_cache()
    _set_state(knowledge_repo, "schema_ready")
    reset_refusals()
    yield
    _set_state(knowledge_repo, "schema_ready")
    reset_refusals()


def _catalog(tmp_path, records=None) -> str:
    path = tmp_path / "dedup_catalog.json"
    path.write_text(json.dumps({"records": records or []}, indent=2))
    os.chmod(path, 0o644)
    return str(path)


def _fingerprint(path):
    b = open(path, "rb").read()
    st = os.stat(path)
    return {
        "sha256": hashlib.sha256(b).hexdigest(),
        "size": st.st_size,
        "mode": stat.S_IMODE(st.st_mode),
        "mtime": st.st_mtime_ns,
    }


def _record(n=0):
    return DocRecord(
        doc_id=f"doc_{n:032x}", kb_id="default",
        content_hash="sha256:" + f"{n:064x}",
        collection_name="retriva_chunks",
        source_paths=[f"/synthetic/{n}.txt"], filename=f"{n}.txt",
        content_size=10, user_metadata={}, chunk_count=1,
        ingestion_status="completed", created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z")


def test_schema_ready_allows_writes(tmp_path):
    path = _catalog(tmp_path)
    store = DeduplicationStore(catalog_path=path)
    store.create_record(_record(1))
    assert _fingerprint(path)["size"] > 0
    assert store.last_write_refused is False


@pytest.mark.parametrize("state", FORBIDDEN_STATES)
def test_forbidden_states_refuse_all_writes_byte_for_byte(
        knowledge_repo, tmp_path, state):
    _set_state(knowledge_repo, state)
    path = _catalog(tmp_path, records=[_record(0).model_dump()])
    before = _fingerprint(path)
    before_dir = set(os.listdir(str(tmp_path)))

    store = DeduplicationStore(catalog_path=path)
    store.create_record(_record(2))
    store.update_record("doc_" + "0" * 32, {"x": 1}, ["/p"])
    store.finalize_record("doc_" + "0" * 32, 5)
    store.delete_by_doc_id("doc_" + "0" * 32)
    store.delete_by_kb_id("default")
    store.clear_all()

    after = _fingerprint(path)
    assert after == before  # bytes, size, mode, mtime unchanged
    assert set(os.listdir(str(tmp_path))) == before_dir  # no .tmp
    assert store.last_write_refused is True
    assert refusal_counts()  # bounded refusal evidence


@pytest.mark.parametrize("state", FORBIDDEN_STATES)
def test_forbidden_states_refuse_reads(knowledge_repo, tmp_path, state):
    _set_state(knowledge_repo, state)
    path = _catalog(tmp_path, records=[_record(0).model_dump()])
    store = DeduplicationStore(catalog_path=path)
    assert store.get_by_hash("default", "sha256:" + "0" * 64) is None
    assert store.get_by_doc_id("doc_" + "0" * 32) is None
    assert store.last_read_refused is True


@pytest.mark.parametrize("state", FORBIDDEN_STATES)
def test_write_raw_defense_in_depth(knowledge_repo, tmp_path, state):
    _set_state(knowledge_repo, state)
    path = _catalog(tmp_path, records=[])
    store = DeduplicationStore(catalog_path=path)
    before = _fingerprint(path)
    ok = store._write_raw({"records": [_record(9).model_dump()]})
    assert ok is False
    assert _fingerprint(path) == before


@pytest.mark.parametrize("state", FORBIDDEN_STATES)
def test_missing_file_is_not_created(knowledge_repo, tmp_path, state):
    _set_state(knowledge_repo, state)
    path = str(tmp_path / "absent" / "dedup_catalog.json")
    store = DeduplicationStore(catalog_path=path)
    assert not os.path.exists(path)  # constructor does not create it
    store.create_record(_record(7))  # attempted write is refused
    assert not os.path.exists(path)
    assert store.last_write_refused is True


@pytest.mark.parametrize("state", FORBIDDEN_STATES)
def test_readonly_file_still_refused(knowledge_repo, tmp_path, state):
    _set_state(knowledge_repo, state)
    path = _catalog(tmp_path, records=[_record(0).model_dump()])
    os.chmod(path, 0o444)
    before = _fingerprint(path)
    store = DeduplicationStore(catalog_path=path)
    store.create_record(_record(3))
    assert _fingerprint(path) == before


@pytest.mark.parametrize("state", FORBIDDEN_STATES)
def test_restart_does_not_weaken(knowledge_repo, tmp_path, state):
    _set_state(knowledge_repo, state)
    path = _catalog(tmp_path, records=[])
    # A fresh instance (equivalent to a process restart) must still refuse.
    DeduplicationStore(catalog_path=path).create_record(_record(4))
    assert _fingerprint(path)["size"] == len(
        json.dumps({"records": []}, indent=2))


def test_privileged_operations_are_readonly(knowledge_repo, tmp_path):
    _set_state(knowledge_repo, "authoritative")
    path = _catalog(tmp_path, records=[_record(0).model_dump()])
    before = _fingerprint(path)
    store = DeduplicationStore(catalog_path=path, operation="adoption_apply")
    store.create_record(_record(5))  # privileged context never writes
    assert _fingerprint(path) == before
    # Bounded read-only inspection is permitted.
    assert store.get_by_hash("default", "sha256:" + "0" * 64) is not None


def test_adoption_dry_run_can_read_legacy_evidence(knowledge_repo,
                                                   tmp_path):
    _set_state(knowledge_repo, "adoption_pending")
    path = _catalog(tmp_path, records=[_record(0).model_dump()])
    store = DeduplicationStore(catalog_path=path,
                               operation="adoption_dry_run")
    assert store.get_by_doc_id("doc_" + "0" * 32) is not None
    # ...but privileged contexts still never write.
    before = _fingerprint(path)
    store.delete_by_doc_id("doc_" + "0" * 32)
    assert _fingerprint(path) == before


def test_fail_closed_when_state_undeterminable(knowledge_repo, tmp_path,
                                               monkeypatch):
    import retriva.knowledge.legacy_guard as lg
    monkeypatch.setattr(lg, "_probe_authority_state", lambda: None)
    lg.invalidate_authority_state_cache()
    path = _catalog(tmp_path, records=[])
    before = _fingerprint(path)
    store = DeduplicationStore(catalog_path=path)
    store.create_record(_record(6))
    assert _fingerprint(path) == before
    assert store.get_by_doc_id("x") is None
    lg.invalidate_authority_state_cache()


# -- Cross-process stale-permissive-cache safety (Spec 028 correction) ------

def test_permissive_state_is_never_cached(knowledge_repo, tmp_path):
    import retriva.knowledge.legacy_guard as lg
    _set_state(knowledge_repo, "schema_ready")
    path = _catalog(tmp_path, records=[])
    DeduplicationStore(catalog_path=path)._writes_allowed()
    assert "state" not in lg._cache  # permissive decisions are not cached


def test_allowed_write_reconfirms_durable_state_cross_process(
        knowledge_repo, tmp_path):
    """Simulate another process transitioning authority WITHOUT touching
    this process's cache; an allowed write must re-confirm durably and be
    refused (no stale permissive authorization)."""
    _set_state(knowledge_repo, "schema_ready")
    path = _catalog(tmp_path, records=[_record(0).model_dump()])
    store = DeduplicationStore(catalog_path=path)
    # Pre-cutover: allowed.
    store.create_record(_record(1))
    assert store.last_write_refused is False
    before = _fingerprint(path)
    # Another process cuts over; NO cache invalidation in this process.
    _set_state(knowledge_repo, "authoritative", invalidate=False)
    # Ordinary write must now be refused despite the earlier permissive
    # decision having been observed in this process.
    store.create_record(_record(2))
    store.finalize_record("doc_" + "0" * 32, 9)
    assert store.last_write_refused is True
    assert _fingerprint(path) == before
    assert store.get_by_hash("default", "sha256:" + "0" * 64) is None


def test_denied_state_may_be_cached_and_is_fail_closed(knowledge_repo,
                                                       tmp_path):
    import retriva.knowledge.legacy_guard as lg
    _set_state(knowledge_repo, "authoritative")
    path = _catalog(tmp_path, records=[])
    store = DeduplicationStore(catalog_path=path)
    store._writes_allowed()
    assert lg._cache.get("state", (0, None))[1] == "authoritative"
    before = _fingerprint(path)
    store.create_record(_record(3))
    assert _fingerprint(path) == before
