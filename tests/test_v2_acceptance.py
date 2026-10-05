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
Acceptance tests for Retriva Core API v2.

These tests map 1:1 to the acceptance criteria in:
  docs/sdd/APIv2_ingestion_pipeline/tests/acceptance-tests.md

  1. Coexistence — v1 still works identically
  2. Metadata    — user_metadata propagates to chunks
  3. Jobs        — v2 job returns stage data
"""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

# Spec 025: the v2 submission endpoints run the durable job lifecycle
# (PostgreSQL authoritative); bind the durable service to the scratch
# jobs database for every test in this module.
pytestmark = pytest.mark.usefixtures("durable_service")

# Ensure default implementations are registered
import retriva.ingestion.chunker              # noqa: F401
import retriva.ingestion.html_parser          # noqa: F401
import retriva.ingestion.parser_router        # noqa: F401
import retriva.ingestion.tika_client          # noqa: F401
import retriva.ingestion.ocrmypdf_preprocessor  # noqa: F401
import retriva.ingestion.docling_parser       # noqa: F401


@pytest.fixture(autouse=True)
def mock_qdrant_startup():
    with patch("retriva.ingestion_api.main.get_client"), \
         patch("retriva.ingestion_api.main.init_collection"):
        yield


@pytest.fixture(autouse=True)
def ensure_registrations():
    """Re-register capabilities in case other tests cleared the registry."""
    import importlib
    importlib.reload(retriva.ingestion.chunker)
    importlib.reload(retriva.ingestion.html_parser)
    importlib.reload(retriva.ingestion.parser_router)
    importlib.reload(retriva.ingestion.tika_client)
    importlib.reload(retriva.ingestion.ocrmypdf_preprocessor)
    importlib.reload(retriva.ingestion.docling_parser)

    # Mock Tika health_check — no running Tika server required
    with patch("retriva.ingestion.tika_client.TikaClient.health_check", return_value=False):
        yield

    from retriva.ingestion_api.job_manager import JobManager
    JobManager._reset()


from retriva.ingestion_api.main import app


# ═══════════════════════════════════════════════════════════════════════════
# AC-1: Coexistence
# "Requests to /api/v1/ingest/text still succeed and return identical v1
#  responses."
# ═══════════════════════════════════════════════════════════════════════════

@patch("retriva.ingestion_api.routers.ingest_text.upsert_chunks")
def test_ac1_coexistence_v1_text_endpoint(mock_upsert):
    """AC-1: POST /api/v1/ingest/text returns 202 with the v1 response shape."""
    payload = {
        "source_path": "test://coexistence",
        "page_title": "Coexistence Test",
        "content_text": "This verifies v1 is untouched by v2 changes.",
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/ingest/text", json=payload)

    # ── v1 contract ──
    assert response.status_code == 202, f"Expected 202, got {response.status_code}"

    data = response.json()
    assert data["status"] == "accepted"
    assert "message" in data
    assert "job_id" in data

    # v1 responses must NOT contain v2-specific fields
    assert "current_stage" not in data
    assert "stages_completed" not in data

    # Background task ran and produced chunks
    assert mock_upsert.called
    chunks = mock_upsert.call_args[0][1]
    assert len(chunks) >= 1
    assert chunks[0].text == "This verifies v1 is untouched by v2 changes."


# ═══════════════════════════════════════════════════════════════════════════
# AC-2: Metadata
# "Ingesting via v2 with {"user_metadata": {"tenant": "A"}} results in
#  chunks containing "tenant": "A"."
# ═══════════════════════════════════════════════════════════════════════════

@patch("retriva.ingestion_api.routers.v2_documents.upsert_chunks")
def test_ac2_metadata_propagation(mock_upsert):
    """AC-2: user_metadata with {"tenant": "A"} is present on every chunk."""
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.write(fd, b"Document content for metadata acceptance test.")
    os.close(fd)

    try:
        payload = {
            "source_uri": path,
            "user_metadata": {"tenant": "A"},
        }

        with TestClient(app) as client:
            response = client.post("/api/v2/documents", json=payload)

        assert response.status_code == 202

        # Verify chunks were produced
        assert mock_upsert.called, "upsert_chunks was never called"
        chunks = mock_upsert.call_args[0][1]
        assert len(chunks) >= 1, "No chunks produced"

        # Every chunk must carry the metadata
        for i, chunk in enumerate(chunks):
            assert chunk.metadata.user_metadata is not None, \
                f"Chunk {i} has no user_metadata"
            assert chunk.metadata.user_metadata.get("tenant") == "A", \
                f"Chunk {i} missing tenant=A: {chunk.metadata.user_metadata}"
    finally:
        os.unlink(path)


# ═══════════════════════════════════════════════════════════════════════════
# AC-3: Jobs
# "Fetching a v2 job returns stage data (e.g., current_stage: "PARSING")."
# ═══════════════════════════════════════════════════════════════════════════

@patch("retriva.ingestion_api.routers.v2_documents.upsert_chunks")
def test_ac3_job_stage_data(mock_upsert):
    """AC-3: GET /api/v2/jobs/{id} returns current_stage and stages_completed."""
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.write(fd, b"Document content for job stage acceptance test.")
    os.close(fd)

    try:
        with TestClient(app) as client:
            # Ingest a document
            ingest_resp = client.post("/api/v2/documents", json={
                "source_uri": path,
                "content_type": "text/plain",
            })
            assert ingest_resp.status_code == 202
            job_id = ingest_resp.json()["job_id"]

            # Fetch job status (background task runs synchronously in TestClient)
            job_resp = client.get(f"/api/v2/jobs/{job_id}")

        assert job_resp.status_code == 200
        job_data = job_resp.json()

        # Must contain stage fields
        assert "current_stage" in job_data, "Response missing 'current_stage'"
        assert "stages_completed" in job_data, "Response missing 'stages_completed'"

        # After completion, we expect stage data to be populated
        assert job_data["status"] == "completed"
        assert job_data["current_stage"] is not None, \
            "current_stage should be set after completion"
        assert isinstance(job_data["stages_completed"], list)
        assert len(job_data["stages_completed"]) > 0, \
            "stages_completed should have entries after completion"

        # Verify known stage names appear
        valid_stages = {"DETECTING", "PREPROCESSING", "PARSING",
                        "NORMALIZATION", "CHUNKING", "INDEXING"}
        assert job_data["current_stage"] in valid_stages, \
            f"current_stage '{job_data['current_stage']}' not a valid stage"
        for stage in job_data["stages_completed"]:
            assert stage in valid_stages, \
                f"Unknown stage in stages_completed: '{stage}'"
    finally:
        os.unlink(path)
