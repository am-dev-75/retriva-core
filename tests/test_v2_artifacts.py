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

import pytest
import time
from fastapi.testclient import TestClient
from retriva.ingestion_api.main import app
from retriva.ingestion_api.job_manager import JobManager, JobStatus

@pytest.fixture
def client():
    return TestClient(app)

@pytest.fixture(autouse=True)
def reset_job_manager():
    JobManager._reset()
    yield

def test_create_markdown_artifact(client):
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
    artifact_id = data["artifact_id"]
    
    # Poll for completion via metadata endpoint
    max_retries = 10
    while max_retries > 0:
        res = client.get(f"/api/v2/artifacts/{artifact_id}")
        assert res.status_code == 200
        if res.json()["status"] == JobStatus.COMPLETED.value:
            break
        time.sleep(0.5)
        max_retries -= 1
        
    assert res.json()["status"] == JobStatus.COMPLETED.value
    
    # Download content
    download_res = client.get(f"/api/v2/artifacts/{artifact_id}/content")
    assert download_res.status_code == 200
    assert "# Test Artifact" in download_res.text
    assert "This is a test content." in download_res.text

def test_create_pdf_artifact(client):
    payload = {
        "artifact_type": "document_list",
        "format": "pdf",
        "parameters": {
            "title": "PDF Test",
            "content": "Hello PDF"
        }
    }
    response = client.post("/api/v2/artifacts", json=payload)
    assert response.status_code == 202
    artifact_id = response.json()["artifact_id"]
    
    # Poll
    max_retries = 10
    while max_retries > 0:
        res = client.get(f"/api/v2/artifacts/{artifact_id}")
        if res.json()["status"] == JobStatus.COMPLETED.value:
            break
        time.sleep(0.5)
        max_retries -= 1
        
    # Download
    download_res = client.get(f"/api/v2/artifacts/{artifact_id}/content")
    assert download_res.status_code == 200
    assert download_res.headers["content-type"] == "application/octet-stream"
    assert download_res.content.startswith(b"%PDF")

def test_artifact_not_found(client):
    response = client.get("/api/v2/artifacts/nonexistent")
    assert response.status_code == 404
    
    response = client.get("/api/v2/artifacts/nonexistent/content")
    assert response.status_code == 404

def test_create_docx_artifact(client):
    payload = {
        "artifact_type": "basic_report",
        "format": "docx",
        "parameters": {
            "title": "Docx Test",
            "content": "Hello Word"
        }
    }
    response = client.post("/api/v2/artifacts", json=payload)
    assert response.status_code == 202
    artifact_id = response.json()["artifact_id"]
    
    max_retries = 10
    while max_retries > 0:
        res = client.get(f"/api/v2/artifacts/{artifact_id}")
        if res.json()["status"] == JobStatus.COMPLETED.value:
            break
        time.sleep(0.5)
        max_retries -= 1
        
    download_res = client.get(f"/api/v2/artifacts/{artifact_id}/content")
    assert download_res.status_code == 200
    assert download_res.content.startswith(b"PK")

def test_create_xlsx_artifact(client):
    payload = {
        "artifact_type": "document_list",
        "format": "xlsx",
        "parameters": {
            "title": "Xlsx Test",
            "content": "Hello Excel"
        }
    }
    response = client.post("/api/v2/artifacts", json=payload)
    assert response.status_code == 202
    artifact_id = response.json()["artifact_id"]
    
    max_retries = 10
    while max_retries > 0:
        res = client.get(f"/api/v2/artifacts/{artifact_id}")
        if res.json()["status"] == JobStatus.COMPLETED.value:
            break
        time.sleep(0.5)
        max_retries -= 1
        
    download_res = client.get(f"/api/v2/artifacts/{artifact_id}/content")
    assert download_res.status_code == 200
    assert download_res.content.startswith(b"PK")

def test_get_capabilities(client):
    response = client.get("/api/v2/artifacts/capabilities")
    assert response.status_code == 200
    data = response.json()
    expected_formats = ["pdf", "markdown", "docx", "xlsx", "odt", "ods", "odp"]
    for fmt in expected_formats:
        assert fmt in data["supported_formats"]
    assert "document_list" in data["supported_types"]
    assert "basic_report" in data["supported_types"]

def test_delete_artifact(client):
    payload = {"artifact_type": "report", "format": "markdown", "parameters": {"title": "To Delete"}}
    response = client.post("/api/v2/artifacts", json=payload)
    artifact_id = response.json()["artifact_id"]
    
    # Delete (idempotent)
    res1 = client.delete(f"/api/v2/artifacts/{artifact_id}")
    assert res1.status_code == 204
    
    res2 = client.delete(f"/api/v2/artifacts/{artifact_id}")
    assert res2.status_code == 204
    
    # Download should fail
    res3 = client.get(f"/api/v2/artifacts/{artifact_id}/content")
    assert res3.status_code in [404, 410, 202]
