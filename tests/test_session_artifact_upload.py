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

"""Session artifact upload/download used by the campaign export tool."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from retriva.config import settings
from retriva.ingestion_api.routers import v2_sessions


def test_create_and_download_session_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_path", str(tmp_path))
    app = FastAPI()
    app.include_router(v2_sessions.router)
    content = b"PK\x03\x04campaign-export-bytes"
    with TestClient(app) as client:
        created = client.post(
            "/api/v2/sessions/sess_export/artifacts",
            files={"file": (
                "campaign-companies-2026-CRA-IT.xlsx", content,
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet")},
            data={"artifact_kind": "campaign_export",
                  "metadata": "{\"campaign_code\": \"2026-CRA-IT\"}"},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        artifact_id = body["artifact_id"]
        assert body["artifact_kind"] == "campaign_export"
        assert body["session_id"] == "sess_export"
        assert body["media_type"].startswith(
            "application/vnd.openxmlformats-officedocument")
        assert body["metadata"]["campaign_code"] == "2026-CRA-IT"
        downloaded = client.get(
            f"/api/v2/sessions/sess_export/artifacts/{artifact_id}/content")
        assert downloaded.status_code == 200
        assert downloaded.content == content
        meta = client.get(
            f"/api/v2/sessions/sess_export/artifacts/{artifact_id}")
        assert meta.status_code == 200
        assert meta.json()["artifact_id"] == artifact_id


def test_create_artifact_rejects_non_object_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_path", str(tmp_path))
    app = FastAPI()
    app.include_router(v2_sessions.router)
    with TestClient(app) as client:
        response = client.post(
            "/api/v2/sessions/sess_export/artifacts",
            files={"file": ("x.xlsx", b"PK", "application/octet-stream")},
            data={"metadata": "[]"},
        )
        assert response.status_code == 400
