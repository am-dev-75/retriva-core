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

"""Spec 030 / ADR-035 route-level HTTP contract + temp-file accounting.

Exercises the real ``upload_document_v2`` route through FastAPI's
TestClient with the durable submission and legacy dedup faked (no live
or heavy infrastructure).  Validates: fresh accept (temp transferred),
idempotent reuse (temp released), genuine conflict -> HTTP 409 (temp
released), and no temp-file leak.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from retriva.ingestion_api import durable_jobs as dj  # noqa: E402
from retriva.ingestion_api import routers  # noqa: E402
from retriva.ingestion_api.routers import v2_documents  # noqa: E402
from retriva.ingestion_api.durable_jobs import SubmissionResult  # noqa: E402
from retriva.jobs.errors import IdempotencyConflictError  # noqa: E402


class _FakeDedup:
    def __init__(self, *a, **k):
        pass

    def get_by_hash(self, *a, **k):
        return None

    def legacy_sync_enabled(self):
        return False

    def create_record(self, *a, **k):
        pass

    def delete_by_doc_id(self, *a, **k):
        return 0


def _build_app():
    app = FastAPI()
    app.include_router(v2_documents.router)

    @app.exception_handler(IdempotencyConflictError)
    async def _conflict(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={
            "error_code": "upload_idempotency_conflict",
            "message": "bounded"})
    return app


def _temp_count(root):
    d = os.path.join(root, "tmp")
    return len(os.listdir(d)) if os.path.isdir(d) else 0


def _run(tmp_path, monkeypatch, submission_factory):
    monkeypatch.setattr(v2_documents, "DeduplicationStore", _FakeDedup)
    monkeypatch.setattr(v2_documents, "require_kb_exists", lambda kb: None)
    monkeypatch.setattr(v2_documents, "get_collection_name",
                        lambda: "cust_test")
    monkeypatch.setattr(v2_documents.settings, "storage_path", str(tmp_path))
    monkeypatch.setattr(dj, "resolve_request_tenant",
                        lambda request: "tenant-route")
    monkeypatch.setattr(dj, "submit_upload_job", submission_factory)
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)
    files = {"file": ("specimen.txt", b"hello world", "text/plain")}
    data = {"source_path": "specimen.txt", "kb_id": "default"}
    return client, files, data


def test_route_fresh_accept_transfers_temp(tmp_path, monkeypatch):
    job = SimpleNamespace(id="job-new")
    client, files, data = _run(tmp_path, monkeypatch, lambda **kw:
                               SubmissionResult(job=job, created=True,
                                                local_runner=None,
                                                knowledge=None))
    before = _temp_count(tmp_path)
    resp = client.post("/api/v2/documents/upload", files=files, data=data)
    assert resp.status_code == 202, resp.text
    assert resp.json()["job_id"] == "job-new"
    # fresh accept transfers ownership to the worker: file remains
    assert _temp_count(tmp_path) == before + 1


def test_route_idempotent_reuse_cleans_temp(tmp_path, monkeypatch):
    job = SimpleNamespace(id="job-existing")
    client, files, data = _run(tmp_path, monkeypatch, lambda **kw:
                               SubmissionResult(job=job, created=False,
                                                local_runner=None,
                                                knowledge=None))
    before = _temp_count(tmp_path)
    resp = client.post("/api/v2/documents/upload", files=files, data=data)
    assert resp.status_code == 202, resp.text
    assert resp.json()["job_id"] == "job-existing"
    assert resp.json()["deduplicated"] is True
    assert _temp_count(tmp_path) == before  # no leak


def test_route_conflict_409_and_cleans_temp(tmp_path, monkeypatch):
    def _raise(**kw):
        raise IdempotencyConflictError("different input identity")
    client, files, data = _run(tmp_path, monkeypatch, _raise)
    before = _temp_count(tmp_path)
    resp = client.post("/api/v2/documents/upload", files=files, data=data)
    assert resp.status_code == 409, resp.text
    body = resp.text
    assert "upload_idempotency_conflict" in body
    assert "temp_path" not in body and "Traceback" not in body
    assert _temp_count(tmp_path) == before  # no leak