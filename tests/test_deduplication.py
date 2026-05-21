# Copyright (C) 2026 Andrea Marson (am.dev.75@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Content-hash deduplication tests — Cases A through F.

A: New file — full pipeline runs, doc record created, Qdrant payload correct.
B: Same file, same path, same metadata — pipeline skipped, already_exists.
C: Same file, different path — source_paths merged, no duplicate chunks.
D: Same file, new metadata key — metadata merged, Qdrant payload patched.
E: Same file, conflicting metadata key — overwrite logged, new value wins.
F: Same file, different KB — treated as independent document.
"""

import json
import tempfile
import os

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock, call

import retriva.ingestion.chunker
import retriva.ingestion.html_parser
import retriva.ingestion.parser_router
import retriva.ingestion.tika_client
import retriva.ingestion.ocrmypdf_preprocessor
import retriva.ingestion.docling_parser


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_qdrant_startup():
    with patch("retriva.ingestion_api.main.get_client"), \
         patch("retriva.ingestion_api.main.init_collection"):
        yield


@pytest.fixture(autouse=True)
def reset_capabilities():
    import importlib
    importlib.reload(retriva.ingestion.chunker)
    importlib.reload(retriva.ingestion.html_parser)
    importlib.reload(retriva.ingestion.parser_router)
    importlib.reload(retriva.ingestion.tika_client)
    importlib.reload(retriva.ingestion.ocrmypdf_preprocessor)
    importlib.reload(retriva.ingestion.docling_parser)
    with patch("retriva.ingestion.tika_client.TikaClient.health_check", return_value=False):
        yield
    from retriva.ingestion_api.job_manager import JobManager
    JobManager._reset()


@pytest.fixture(autouse=True)
def clean_dedup_store(tmp_path):
    """Give each test an isolated DeduplicationStore backed by a tmp file."""
    catalog = tmp_path / "dedup_catalog.json"
    with patch(
        "retriva.ingestion.dedup.DeduplicationStore.__init__",
        lambda self, catalog_path=None: _init_store(self, str(catalog))
    ):
        yield


def _init_store(store_instance, catalog_path):
    """Replicate __init__ with a custom path (needed by the autouse fixture)."""
    import json, os, threading
    from pathlib import Path
    store_instance._path = Path(catalog_path)
    store_instance._path.parent.mkdir(parents=True, exist_ok=True)
    store_instance._lock = threading.Lock()
    if not store_instance._path.exists():
        with open(store_instance._path, "w") as f:
            json.dump({"records": []}, f)


FILE_CONTENT = b"Hello, this is the canonical content of costs.txt for dedup tests."


def _upload(client, content=FILE_CONTENT, source_path="costs.txt",
            metadata=None, kb_id="default"):
    data = {"source_path": source_path, "kb_id": kb_id}
    if metadata:
        data["user_metadata"] = json.dumps(metadata)
    return client.post(
        "/api/v2/documents/upload",
        files={"file": ("costs.txt", content, "text/plain")},
        data=data,
    )


# ---------------------------------------------------------------------------
# Case A — New file
# ---------------------------------------------------------------------------

@patch("retriva.ingestion_api.routers.v2_documents.upsert_chunks")
@patch("retriva.ingestion_api.routers.v2_documents.get_client")
def test_case_a_new_file(mock_get_client, mock_upsert):
    """New upload: 202 accepted, doc_id + content_hash returned, chunks upserted."""
    from retriva.ingestion_api.main import app

    with TestClient(app) as client:
        resp = _upload(client, metadata={"project": "apollo"})

    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["deduplicated"] is False
    assert data["chunks_reused"] is False
    assert data["metadata_updated"] is False
    assert data["doc_id"] is not None
    assert data["content_hash"].startswith("sha256:")
    assert data["job_id"] is not None

    # Chunks must have been upserted
    assert mock_upsert.called
    chunks = mock_upsert.call_args[0][1]
    assert len(chunks) >= 1

    # Chunk payloads carry the new dedup fields
    chunk = chunks[0]
    assert chunk.metadata.content_hash == data["content_hash"]
    assert chunk.metadata.content_hash_algorithm == "sha256"
    assert "costs.txt" in (chunk.metadata.source_paths or [])


# ---------------------------------------------------------------------------
# Case B — Same file, same KB, same metadata
# ---------------------------------------------------------------------------

@patch("retriva.ingestion_api.routers.v2_documents.upsert_chunks")
@patch("retriva.ingestion_api.routers.v2_documents.get_client")
def test_case_b_same_file_same_metadata(mock_get_client, mock_upsert):
    """Duplicate upload with no metadata change → already_exists, pipeline skipped."""
    from retriva.ingestion_api.main import app

    with TestClient(app) as client:
        r1 = _upload(client, metadata={"project": "apollo"})
        assert r1.status_code == 202

        # Reset upsert spy
        mock_upsert.reset_mock()

        r2 = _upload(client, metadata={"project": "apollo"})

    assert r2.status_code == 202
    data = r2.json()
    assert data["status"] == "already_exists"
    assert data["deduplicated"] is True
    assert data["chunks_reused"] is True
    assert data["metadata_updated"] is False

    # Pipeline must NOT have run again
    mock_upsert.assert_not_called()

    # doc_id must be the same
    assert r1.json()["doc_id"] == r2.json()["doc_id"]


# ---------------------------------------------------------------------------
# Case C — Same file, same KB, different path
# ---------------------------------------------------------------------------

@patch("retriva.ingestion_api.routers.v2_documents.upsert_chunks")
@patch("retriva.ingestion_api.routers.v2_documents.update_payload_by_doc_id")
@patch("retriva.ingestion_api.routers.v2_documents.get_client")
def test_case_c_same_file_different_path(mock_get_client, mock_update_payload, mock_upsert):
    """Duplicate from different path → source_paths merged, no re-indexing."""
    from retriva.ingestion_api.main import app

    with TestClient(app) as client:
        r1 = _upload(client, source_path="prj_apollo/costs.txt")
        assert r1.status_code == 202

        mock_upsert.reset_mock()

        r2 = _upload(client, source_path="costs.txt")

    assert r2.status_code == 202
    data = r2.json()
    assert data["status"] in ("already_exists", "metadata_updated")
    assert data["deduplicated"] is True
    assert data["chunks_reused"] is True

    # Payload update must have been called with merged source_paths
    assert mock_update_payload.called
    patch_payload = mock_update_payload.call_args[0][2]  # payload_patch arg
    assert "source_paths" in patch_payload
    paths = patch_payload["source_paths"]
    assert "prj_apollo/costs.txt" in paths
    assert "costs.txt" in paths

    # No new Qdrant points
    mock_upsert.assert_not_called()


# ---------------------------------------------------------------------------
# Case D — Same file, same KB, new metadata key
# ---------------------------------------------------------------------------

@patch("retriva.ingestion_api.routers.v2_documents.upsert_chunks")
@patch("retriva.ingestion_api.routers.v2_documents.update_payload_by_doc_id")
@patch("retriva.ingestion_api.routers.v2_documents.get_client")
def test_case_d_new_metadata_key(mock_get_client, mock_update_payload, mock_upsert):
    """Second upload adds new metadata key → merged, Qdrant patched, no re-embedding."""
    from retriva.ingestion_api.main import app

    with TestClient(app) as client:
        r1 = _upload(client, metadata={"project": "apollo"})
        assert r1.status_code == 202

        mock_upsert.reset_mock()

        r2 = _upload(client, metadata={"department": "r&d"})

    assert r2.status_code == 202
    data = r2.json()
    assert data["status"] == "metadata_updated"
    assert data["metadata_updated"] is True

    # Check merged payload sent to Qdrant
    patch_payload = mock_update_payload.call_args[0][2]
    merged = patch_payload["user_metadata"]
    assert merged.get("project") == "apollo"
    assert merged.get("department") == "r&d"

    # No re-embedding
    mock_upsert.assert_not_called()


# ---------------------------------------------------------------------------
# Case E — Same file, same KB, conflicting metadata key (overwrite)
# ---------------------------------------------------------------------------

@patch("retriva.ingestion_api.routers.v2_documents.upsert_chunks")
@patch("retriva.ingestion_api.routers.v2_documents.update_payload_by_doc_id")
@patch("retriva.ingestion_api.routers.v2_documents.get_client")
def test_case_e_conflicting_metadata_overwrite(mock_get_client, mock_update_payload, mock_upsert, caplog):
    """Conflicting key → new value wins, overwrite is logged."""
    import logging
    from retriva.ingestion_api.main import app

    with TestClient(app) as client:
        _upload(client, metadata={"project": "apollo"})
        mock_upsert.reset_mock()

        with caplog.at_level(logging.INFO):
            r2 = _upload(client, metadata={"project": "zeus"})

    assert r2.status_code == 202
    assert r2.json()["metadata_updated"] is True

    patch_payload = mock_update_payload.call_args[0][2]
    assert patch_payload["user_metadata"]["project"] == "zeus"

    # Overwrite must be logged
    assert any("overwrite" in rec.message for rec in caplog.records)

    mock_upsert.assert_not_called()


# ---------------------------------------------------------------------------
# Case F — Same file, different KB
# ---------------------------------------------------------------------------

@patch("retriva.ingestion_api.routers.v2_documents.upsert_chunks")
@patch("retriva.ingestion_api.routers.v2_documents.get_client")
def test_case_f_same_file_different_kb(mock_get_client, mock_upsert):
    """Same bytes in different KB → separate doc records, no cross-KB dedup."""
    from retriva.ingestion_api.main import app

    with TestClient(app) as client:
        # Phase 2: KB enforcement requires the target KBs to exist before
        # ingestion; create them via the public API.
        for kb_id in ("kb_alpha", "kb_beta"):
            create_resp = client.post("/api/v2/kbs", json={"kb_id": kb_id, "name": kb_id})
            assert create_resp.status_code in (201, 409), create_resp.text
        r1 = _upload(client, kb_id="kb_alpha")
        r2 = _upload(client, kb_id="kb_beta")

    assert r1.status_code == 202
    assert r2.status_code == 202

    assert r1.json()["status"] == "accepted"
    assert r2.json()["status"] == "accepted"

    # Different doc_ids (scoped by KB)
    assert r1.json()["doc_id"] != r2.json()["doc_id"]

    # Both should have triggered the pipeline
    assert mock_upsert.call_count == 2


# ---------------------------------------------------------------------------
# Unit tests — hashing and merge helpers
# ---------------------------------------------------------------------------

def test_compute_content_hash_deterministic():
    from retriva.ingestion.dedup import compute_content_hash
    h1 = compute_content_hash(b"hello")
    h2 = compute_content_hash(b"hello")
    assert h1 == h2
    assert h1.startswith("sha256:")
    assert len(h1) == len("sha256:") + 64


def test_derive_doc_id_scoped_by_kb():
    from retriva.ingestion.dedup import derive_doc_id
    d1 = derive_doc_id("kb_alpha", "abc123")
    d2 = derive_doc_id("kb_beta", "abc123")
    assert d1 != d2
    assert d1.startswith("doc_")
    assert d2.startswith("doc_")


def test_merge_metadata_add_key():
    from retriva.ingestion.dedup import merge_metadata
    merged, changed = merge_metadata({"a": "1"}, {"b": "2"}, "doc1", "kb1")
    assert merged == {"a": "1", "b": "2"}
    assert changed is True


def test_merge_metadata_overwrite_key():
    from retriva.ingestion.dedup import merge_metadata
    merged, changed = merge_metadata({"a": "old"}, {"a": "new"}, "doc1", "kb1")
    assert merged["a"] == "new"
    assert changed is True


def test_merge_metadata_no_change():
    from retriva.ingestion.dedup import merge_metadata
    merged, changed = merge_metadata({"a": "1"}, {"a": "1"}, "doc1", "kb1")
    assert changed is False


def test_merge_source_paths_new_path():
    from retriva.ingestion.dedup import merge_source_paths
    merged, changed = merge_source_paths(["a/costs.txt"], "costs.txt", "d", "k")
    assert "costs.txt" in merged
    assert "a/costs.txt" in merged
    assert changed is True


def test_merge_source_paths_duplicate():
    from retriva.ingestion.dedup import merge_source_paths
    merged, changed = merge_source_paths(["costs.txt"], "costs.txt", "d", "k")
    assert merged == ["costs.txt"]
    assert changed is False
