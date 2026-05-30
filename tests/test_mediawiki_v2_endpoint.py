import pytest
from fastapi.testclient import TestClient
from pathlib import Path

from retriva.ingestion_api.main import app
from retriva.ingestion_api.deps import require_kb_exists

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

def test_mediawiki_v2_endpoint_accepts_valid_request(mock_kb, mock_background_task, tmp_path):
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
    
    # Check that the background task was scheduled
    assert len(mock_background_task) == 1
    func_args = mock_background_task[0][0]
    # The actual arguments passed to the function (args[0] is self, args[1] is func)
    args_passed = func_args[2:]
    assert args_passed[0] == str(staged_dir) # staged_dir
    assert args_passed[1] == {"author": "admin"} # user_metadata
    assert args_passed[2] == "test_kb" # kb_id
    assert args_passed[4] == data["job_id"] # job_id

def test_mediawiki_v2_endpoint_missing_staged_dir(mock_kb):
    payload = {
        "kb_id": "test_kb"
    }
    
    response = client.post("/api/v2/documents/mediawiki", json=payload)
    
    assert response.status_code == 422 # Validation error
    data = response.json()
    assert "staged_dir" in str(data["detail"])
