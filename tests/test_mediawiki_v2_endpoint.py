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
from fastapi.testclient import TestClient
from pathlib import Path

from retriva.ingestion_api.main import app
from retriva.ingestion_api.deps import require_kb_exists

# Spec 025: the v2 submission endpoints run the durable job lifecycle
# (PostgreSQL authoritative); bind the durable service to the scratch
# jobs database for every test in this module.
pytestmark = pytest.mark.usefixtures("durable_service")

client = TestClient(app)

@pytest.fixture
def mock_kb(monkeypatch):
    # Bypass KB existence check
    def mock_require_kb_exists(kb_id):
        pass
    monkeypatch.setattr("retriva.ingestion_api.routers.v2_documents.require_kb_exists", mock_require_kb_exists)

@pytest.fixture
def mock_background_task(monkeypatch):
    tasks = []
    def mock_add_task(*args, **kwargs):
        tasks.append((args, kwargs))
    monkeypatch.setattr("fastapi.BackgroundTasks.add_task", mock_add_task)
    return tasks

def test_mediawiki_v2_endpoint_accepts_valid_request(
        mock_kb, mock_background_task, tmp_path, durable_service):
    staged_dir = tmp_path / "staged"
    staged_dir.mkdir()

    payload = {
        "staged_dir": str(staged_dir),
        "kb_id": "test_kb",
        "user_metadata": {"author": "admin"}
    }

    response = client.post("/api/v2/documents/mediawiki", json=payload)

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "accepted"
    assert "job_id" in data

    # Spec 025: exactly ONE durable runner is scheduled on
    # BackgroundTasks (the local executor callable; the pipeline input
    # lives in the durable job record, not in the task args).
    assert len(mock_background_task) == 1
    func_args = mock_background_task[0][0]
    func = func_args[1]
    assert func.__name__ == "run_durable_job_local"

    # The durable job record carries the submission inputs.
    job = durable_service.get_job(
        tenant_id=durable_service.settings.default_tenant,
        job_id=data["job_id"])
    assert job.job_type == "v2_mediawiki"
    assert job.execution_transport.value == "local"
    assert job.input_metadata["staged_dir"] == str(staged_dir)
    assert job.input_metadata["kb_id"] == "test_kb"
    assert job.input_metadata["user_metadata"] == {"author": "admin"}

def test_mediawiki_v2_endpoint_missing_staged_dir(mock_kb):
    payload = {
        "kb_id": "test_kb"
    }
    
    response = client.post("/api/v2/documents/mediawiki", json=payload)
    
    assert response.status_code == 422 # Validation error
    data = response.json()
    assert "staged_dir" in str(data["detail"])
