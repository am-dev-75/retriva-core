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
Tests for the Retriva Core API v2 ingestion pipeline.

Covers:
- JSON body ingestion (POST /api/v2/documents)
- File upload ingestion (POST /api/v2/documents/upload)
- Stage-aware job status (GET /api/v2/jobs/{id})
- Metadata propagation and validation
- v1 regression (coexistence)
- MIME detection precedence
"""

import json
import time

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

# Spec 025: the v2 submission endpoints run the durable job lifecycle
# (PostgreSQL authoritative); bind the durable service to the scratch
# jobs database for every test in this module.
pytestmark = pytest.mark.usefixtures("durable_service")

# Ensure default implementations are registered before app is imported
import retriva.ingestion.chunker              # noqa: F401
import retriva.ingestion.html_parser          # noqa: F401
import retriva.ingestion.parser_router        # noqa: F401
import retriva.ingestion.tika_client          # noqa: F401
import retriva.ingestion.ocrmypdf_preprocessor  # noqa: F401
import retriva.ingestion.docling_parser       # noqa: F401


# Mock Qdrant connection during app startup lifespan
@pytest.fixture(autouse=True)
def mock_qdrant_startup():
    with patch("retriva.ingestion_api.main.get_client"), \
         patch("retriva.ingestion_api.main.init_collection"):
        yield


from retriva.ingestion_api.main import app


@pytest.fixture(autouse=True)
def reset_capabilities():
    """Re-register capabilities for each test.

    Other test modules (test_registry, test_extension_loading) may call
    ``CapabilityRegistry._reset()``, clearing all registrations.  We
    re-register the defaults here so v2 tests always find all capabilities.
    Also mocks TikaClient.health_check to return False so tests use the
    extension-based fallback (no Tika server needed).
    """
    import importlib
    importlib.reload(retriva.ingestion.chunker)
    importlib.reload(retriva.ingestion.html_parser)
    importlib.reload(retriva.ingestion.parser_router)
    importlib.reload(retriva.ingestion.tika_client)
    importlib.reload(retriva.ingestion.ocrmypdf_preprocessor)
    importlib.reload(retriva.ingestion.docling_parser)

    # Mock Tika health_check so the v2 pipeline falls back to extension-based
    # MIME detection — no running Tika server required for unit tests
    with patch("retriva.ingestion.tika_client.TikaClient.health_check", return_value=False):
        yield


@pytest.fixture()
def isolated_dedup_store(monkeypatch, tmp_path):
    """Point the pipeline's dedup catalog at a temp path so upload
    tests stay hermetic across runs (the checkout catalog otherwise
    accumulates records from previous runs and content dedup turns
    them into duplicates)."""
    from retriva.ingestion.dedup import DeduplicationStore
    from retriva.ingestion_api.routers import v2_documents
    monkeypatch.setattr(
        v2_documents, "DeduplicationStore",
        lambda: DeduplicationStore(
            catalog_path=str(tmp_path / "dedup_catalog.json")))


# ---------------------------------------------------------------------------
# POST /api/v2/documents — JSON body
# ---------------------------------------------------------------------------

@patch("retriva.ingestion_api.routers.v2_documents.upsert_chunks")
def test_v2_ingest_returns_202(mock_upsert):
    """POST /api/v2/documents returns 202 with a job_id."""
    # Create a temporary text file for ingestion
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.write(fd, b"Hello, this is a test document for v2 ingestion.")
    os.close(fd)

    try:
        payload = {
            "source_uri": path,
            "content_type": "text/plain",
        }
        with TestClient(app) as client:
            response = client.post("/api/v2/documents", json=payload)

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        assert "job_id" in data
        assert len(data["job_id"]) > 0
    finally:
        os.unlink(path)


@patch("retriva.ingestion_api.routers.v2_documents.upsert_chunks")
def test_v2_metadata_propagation(mock_upsert):
    """user_metadata from the v2 request is propagated to all chunks."""
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.write(fd, b"Content for metadata propagation test.")
    os.close(fd)

    try:
        payload = {
            "source_uri": path,
            "content_type": "text/plain",
            "user_metadata": {"tenant": "A", "project": "retriva"},
        }
        with TestClient(app) as client:
            response = client.post("/api/v2/documents", json=payload)

        assert response.status_code == 202
        assert mock_upsert.called

        chunks = mock_upsert.call_args[0][1]
        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk.metadata.user_metadata == {"tenant": "A", "project": "retriva"}
    finally:
        os.unlink(path)


@patch("retriva.ingestion_api.routers.v2_documents.upsert_chunks")
def test_v2_metadata_validation_422(mock_upsert):
    """Invalid metadata is rejected with a 422 response."""
    payload = {
        "source_uri": "/tmp/nonexistent.txt",
        "user_metadata": {"key": 12345},  # value must be string
    }
    with TestClient(app) as client:
        response = client.post("/api/v2/documents", json=payload)

    assert response.status_code == 422
    assert not mock_upsert.called


# ---------------------------------------------------------------------------
# POST /api/v2/documents/upload — file upload
# ---------------------------------------------------------------------------

@patch("retriva.ingestion_api.routers.v2_documents.upsert_chunks")
def test_v2_upload_returns_202(mock_upsert, isolated_dedup_store):
    """POST /api/v2/documents/upload returns 202 with a job_id."""
    content = b"Uploaded text file content for v2."

    with TestClient(app) as client:
        response = client.post(
            "/api/v2/documents/upload",
            files={"file": ("test.txt", content, "text/plain")},
            data={"source_path": "uploads/test.txt"},
        )

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "accepted"
    assert "job_id" in data


@patch("retriva.ingestion_api.routers.v2_documents.upsert_chunks")
def test_v2_upload_with_metadata(mock_upsert, isolated_dedup_store):
    """File upload with JSON-encoded user_metadata propagates to chunks."""
    content = b"Uploaded content with metadata."

    with TestClient(app) as client:
        response = client.post(
            "/api/v2/documents/upload",
            files={"file": ("test.txt", content, "text/plain")},
            data={
                "source_path": "uploads/test.txt",
                "user_metadata": json.dumps({"department": "engineering"}),
            },
        )

    assert response.status_code == 202
    assert mock_upsert.called

    chunks = mock_upsert.call_args[0][1]
    assert len(chunks) >= 1
    for chunk in chunks:
        assert chunk.metadata.user_metadata == {"department": "engineering"}


def test_v2_upload_invalid_metadata_422():
    """Upload with invalid JSON metadata returns 422."""
    content = b"Content"

    with TestClient(app) as client:
        response = client.post(
            "/api/v2/documents/upload",
            files={"file": ("test.txt", content, "text/plain")},
            data={
                "source_path": "uploads/test.txt",
                "user_metadata": "not-valid-json{{{",
            },
        )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v2/jobs — stage tracking
# ---------------------------------------------------------------------------

@patch("retriva.ingestion_api.routers.v2_documents.upsert_chunks")
def test_v2_job_has_stages(mock_upsert):
    """Job status endpoint returns current_stage and stages_completed."""
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.write(fd, b"Content for job stage test.")
    os.close(fd)

    try:
        payload = {"source_uri": path, "content_type": "text/plain"}
        with TestClient(app) as client:
            ingest_resp = client.post("/api/v2/documents", json=payload)
            job_id = ingest_resp.json()["job_id"]

            # Background task runs synchronously in TestClient
            job_resp = client.get(f"/api/v2/jobs/{job_id}")

        assert job_resp.status_code == 200
        job_data = job_resp.json()
        assert job_data["job_id"] == job_id

        # After completion, current_stage should be set and stages_completed should be populated
        assert job_data["status"] == "completed"
        assert job_data["current_stage"] is not None
        assert isinstance(job_data["stages_completed"], list)
        assert len(job_data["stages_completed"]) > 0
    finally:
        os.unlink(path)


def test_v2_job_not_found_404():
    """GET /api/v2/jobs/{id} returns 404 for unknown job_id."""
    with TestClient(app) as client:
        response = client.get("/api/v2/jobs/nonexistent-id")

    assert response.status_code == 404


@patch("retriva.ingestion_api.routers.v2_documents.upsert_chunks")
def test_v2_list_jobs_filters_v2(mock_upsert):
    """GET /api/v2/jobs only returns durable v2-typed jobs."""
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.write(fd, b"Content for list jobs test.")
    os.close(fd)

    try:
        with TestClient(app) as client:
            # Create a v2 job (Retriva API v1 was removed; every job on
            # this surface is durable and v2-typed by construction).
            client.post("/api/v2/documents", json={
                "source_uri": path,
                "content_type": "text/plain",
            })

            # v2 jobs list should not contain v1 jobs
            list_resp = client.get("/api/v2/jobs")

        assert list_resp.status_code == 200
        jobs = list_resp.json()
        for job in jobs:
            assert job["job_type"].startswith("v2_")
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# MIME detection precedence
# ---------------------------------------------------------------------------

def test_mime_explicit_precedence():
    """Explicit content_type overrides extension-based detection."""
    from retriva.ingestion.parser_router import DefaultParserRouter

    router = DefaultParserRouter()

    # Extension says .txt → text/plain, but explicit hint says application/pdf
    result = router.detect_content_type("document.txt", hint="application/pdf")
    assert result == "application/pdf"

    # No hint → uses extension
    result = router.detect_content_type("document.pdf")
    assert result == "application/pdf"

    # Unknown extension, no hint → fallback
    result = router.detect_content_type("document.xyz")
    assert result == "application/octet-stream"


# ---------------------------------------------------------------------------
# v1 Coexistence (regression)
# ---------------------------------------------------------------------------

@patch("retriva.ingestion_api.routers.v2_documents.upsert_chunks")
def test_v1_removed_404(mock_upsert):
    """Retriva API v1 was removed (Spec 027): /api/v1/ingest/text
    returns ordinary 404 with no side effect."""
    payload = {
        "source_path": "test://v1-regression",
        "page_title": "V1 Regression Test",
        "content_text": "Verifying that v1 was removed by Spec 027.",
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/ingest/text", json=payload)

    assert response.status_code == 404
    assert not mock_upsert.called
