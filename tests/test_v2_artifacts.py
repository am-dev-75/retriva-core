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
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.  See the License for the specific language governing
# permissions and limitations under the License.

"""API-compatibility tests for the v2 artifact surface on the
DURABLE lifecycle (Spec 026): the verified v2 contract is preserved
(202 shape, status polling, content download, capabilities,
idempotent DELETE) while the job state is PostgreSQL-authoritative
(no legacy JobManager writes)."""

import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from retriva.ingestion_api.main import app

# Spec 026: the v2 artifact workflow runs the durable job lifecycle
# (PostgreSQL authoritative); bind the durable service to the scratch
# jobs database for every test in this module.
pytestmark = pytest.mark.usefixtures("durable_service")


@pytest.fixture()
def isolated_artifact_storage(monkeypatch, tmp_path):
    """Hermetic artifact storage: the handler and the routes resolve
    the provider through ``retriva.infrastructure.storage``."""
    from functools import partial
    from retriva.infrastructure.storage import LocalStorageProvider
    monkeypatch.setattr(
        "retriva.infrastructure.storage.LocalStorageProvider",
        partial(LocalStorageProvider, base_path=str(tmp_path)))


@pytest.fixture()
def client(monkeypatch):
    with patch("retriva.ingestion_api.main.get_client"), \
            patch("retriva.ingestion_api.main.init_collection"):
        with TestClient(app) as test_client:
            yield test_client


def test_create_markdown_artifact(client, isolated_artifact_storage):
    payload = {
        "artifact_type": "document_list",
        "format": "markdown",
        "parameters": {
            "title": "Test Artifact",
            "content": "This is a test content."
        }
    }
    response = client.post("/api/v2/artifacts", json=payload)
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "accepted"
    artifact_id = data["artifact_id"]
    job_id = data["job_id"]
    assert job_id  # the DURABLE Core job id

    # Poll for completion via metadata endpoint (durable record).
    max_retries = 10
    res = None
    while max_retries > 0:
        res = client.get(f"/api/v2/artifacts/{artifact_id}")
        assert res.status_code == 200
        if res.json()["status"] == "completed":
            break
        time.sleep(0.2)
        max_retries -= 1
    assert res.json()["status"] == "completed"
    assert res.json()["job_id"] == job_id
    # Durable progress phases exposed (bounded, additive fields):
    # the last advanced phase remains current; earlier phases are
    # the completed prefix.
    assert res.json()["current_stage"] == "finalizing"
    assert res.json()["stages_completed"] == [
        "fetching_data", "rendering"]

    # Download content (deterministic safe media type).
    download_res = client.get(f"/api/v2/artifacts/{artifact_id}/content")
    assert download_res.status_code == 200
    assert download_res.headers["content-type"].startswith("text/markdown")
    assert "Document List" in download_res.text


def test_create_pdf_artifact(client, isolated_artifact_storage):
    payload = {
        "artifact_type": "document_list",
        "format": "pdf",
        "parameters": {"title": "PDF Test", "content": "Hello PDF"}
    }
    response = client.post("/api/v2/artifacts", json=payload)
    assert response.status_code == 202
    artifact_id = response.json()["artifact_id"]

    max_retries = 10
    res = None
    while max_retries > 0:
        res = client.get(f"/api/v2/artifacts/{artifact_id}")
        if res.json()["status"] == "completed":
            break
        time.sleep(0.2)
        max_retries -= 1
    assert res.json()["status"] == "completed"

    download_res = client.get(f"/api/v2/artifacts/{artifact_id}/content")
    assert download_res.status_code == 200
    assert download_res.headers["content-type"] == "application/pdf"
    assert download_res.content.startswith(b"%PDF")


def test_create_docx_artifact(client, isolated_artifact_storage):
    payload = {
        "artifact_type": "document_list",
        "format": "docx",
        "parameters": {"title": "Docx Test", "content": "Hello Word"}
    }
    response = client.post("/api/v2/artifacts", json=payload)
    assert response.status_code == 202
    artifact_id = response.json()["artifact_id"]
    max_retries = 10
    res = None
    while max_retries > 0:
        res = client.get(f"/api/v2/artifacts/{artifact_id}")
        if res.json()["status"] == "completed":
            break
        time.sleep(0.2)
        max_retries -= 1
    assert res.json()["status"] == "completed"
    download_res = client.get(f"/api/v2/artifacts/{artifact_id}/content")
    assert download_res.status_code == 200
    assert download_res.content.startswith(b"PK")
    assert download_res.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml")


def test_create_xlsx_artifact(client, isolated_artifact_storage):
    payload = {
        "artifact_type": "document_list",
        "format": "xlsx",
        "parameters": {"title": "Xlsx Test", "content": "Hello Excel"}
    }
    response = client.post("/api/v2/artifacts", json=payload)
    assert response.status_code == 202
    artifact_id = response.json()["artifact_id"]
    max_retries = 10
    res = None
    while max_retries > 0:
        res = client.get(f"/api/v2/artifacts/{artifact_id}")
        if res.json()["status"] == "completed":
            break
        time.sleep(0.2)
        max_retries -= 1
    assert res.json()["status"] == "completed"
    download_res = client.get(f"/api/v2/artifacts/{artifact_id}/content")
    assert download_res.status_code == 200
    assert download_res.content.startswith(b"PK")
    assert download_res.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument."
        "spreadsheetml")


def test_get_capabilities(client):
    response = client.get("/api/v2/artifacts/capabilities")
    assert response.status_code == 200
    data = response.json()
    expected_formats = ["pdf", "markdown", "docx", "xlsx", "odt", "ods",
                        "odp"]
    for fmt in expected_formats:
        assert fmt in data["supported_formats"]
    assert "document_list" in data["supported_types"]
    assert "basic_report" in data["supported_types"]


def test_artifact_not_found(client):
    response = client.get("/api/v2/artifacts/nonexistent")
    assert response.status_code == 404

    response = client.get("/api/v2/artifacts/nonexistent/content")
    assert response.status_code == 404


def test_each_post_creates_a_new_artifact_and_durable_job(
        client, isolated_artifact_storage):
    payload = {"artifact_type": "document_list", "format": "markdown",
               "parameters": {"title": "T"}}
    first = client.post("/api/v2/artifacts", json=payload)
    second = client.post("/api/v2/artifacts", json=payload)
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["artifact_id"] != second.json()["artifact_id"]
    assert first.json()["job_id"] != second.json()["job_id"]


def test_no_legacy_jobmanager_write(client, isolated_artifact_storage):
    payload = {"artifact_type": "document_list", "format": "markdown",
               "parameters": {"title": "T"}}
    response = client.post("/api/v2/artifacts", json=payload)
    assert response.status_code == 202
    # The integrated v2 artifact workflow NEVER writes the legacy
    # in-memory manager (PostgreSQL is the sole authoritative store).
    import sys
    assert "retriva.ingestion_api.job_manager" not in sys.modules


def test_unsupported_format_rejected(client):
    response = client.post("/api/v2/artifacts", json={
        "artifact_type": "document_list", "format": "doc",
        "parameters": {}})
    assert response.status_code == 400


def test_no_public_retry_route():
    paths = app.openapi()["paths"]
    artifact_paths = [p for p in paths
                      if p.startswith("/api/v2/artifacts")]
    assert not any("/retry" in p for p in artifact_paths)


def test_cross_tenant_artifact_denied(client, isolated_artifact_storage,
                                      monkeypatch):
    payload = {"artifact_type": "document_list", "format": "markdown",
               "parameters": {"title": "X"}}
    # Dev override (loopback TestClient) submits for ANOTHER tenant.
    monkeypatch.setenv("RETRIVA_JOBS_TENANT_HEADER_OVERRIDE", "1")
    from retriva.jobs.config import reset_jobs_settings
    from retriva.jobs.tenant import reset_tenant_resolver
    reset_jobs_settings()
    reset_tenant_resolver()
    other = client.post("/api/v2/artifacts", json=payload,
                        headers={"x-retriva-tenant": "other-tenant"})
    assert other.status_code == 202
    other_artifact = other.json()["artifact_id"]
    reset_jobs_settings()
    reset_tenant_resolver()
    # The fixed tenant cannot see another tenant's artifact.
    assert client.get(
        f"/api/v2/artifacts/{other_artifact}").status_code == 404
    assert client.get(
        f"/api/v2/artifacts/{other_artifact}/content").status_code == 404
    # DELETE is idempotent even cross-tenant (204; no state touched).
    assert client.delete(
        f"/api/v2/artifacts/{other_artifact}").status_code == 204


def test_delete_artifact_idempotent(client, isolated_artifact_storage):
    payload = {"artifact_type": "document_list", "format": "markdown",
               "parameters": {"title": "To Delete"}}
    response = client.post("/api/v2/artifacts", json=payload)
    artifact_id = response.json()["artifact_id"]

    # Wait for completion, then delete (the finalized artifact goes).
    max_retries = 10
    while max_retries > 0:
        res = client.get(f"/api/v2/artifacts/{artifact_id}")
        if res.json()["status"] == "completed":
            break
        time.sleep(0.2)
        max_retries -= 1

    res1 = client.delete(f"/api/v2/artifacts/{artifact_id}")
    assert res1.status_code == 204

    res2 = client.delete(f"/api/v2/artifacts/{artifact_id}")
    assert res2.status_code == 204

    # Content after deletion: succeeded job, missing file → 404.
    res3 = client.get(f"/api/v2/artifacts/{artifact_id}/content")
    assert res3.status_code in (404, 410, 202)


def test_sanitized_error_on_failed_generation(
        client, isolated_artifact_storage, monkeypatch):
    class Failing:
        def render(self, **kwargs):
            raise RuntimeError("secret internal detail /tmp/xyz")

    monkeypatch.setattr("retriva.rendering.get_renderer",
                        lambda fmt: Failing())
    response = client.post("/api/v2/artifacts", json={
        "artifact_type": "document_list", "format": "markdown",
        "parameters": {"title": "F"}})
    artifact_id = response.json()["artifact_id"]
    max_retries = 10
    while max_retries > 0:
        res = client.get(f"/api/v2/artifacts/{artifact_id}")
        if res.json()["status"] in ("failed", "completed"):
            break
        time.sleep(0.2)
        max_retries -= 1
    assert res.json()["status"] == "failed"
    assert res.json()["error"] == "RuntimeError"  # sanitized class only
    content = client.get(f"/api/v2/artifacts/{artifact_id}/content")
    assert content.status_code == 410
    assert "secret internal detail" not in content.json()["detail"]
    assert "/tmp/xyz" not in content.json()["detail"]
