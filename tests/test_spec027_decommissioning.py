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

"""Spec 027 / ADR-032 acceptance gates: Retriva API v1 decommissioning
and legacy job-state retirement.

Covers:
- every removed Retriva API v1 path returns ordinary 404 with no side
  effect;
- OpenAPI and the root discovery payload no longer advertise Retriva
  API v1 while the OpenAI-compatible ``/v1`` surface remains clearly
  distinct;
- the CLI makes no Retriva API v1 HTTP call anywhere (image ingestion
  fails locally with bounded guidance; reindex fails locally);
- the legacy JobManager module, its importers, the Redis legacy
  job-state helpers, and the raw ``AsyncResult`` fallback are retired.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

_SRC = Path(__file__).resolve().parent.parent / "src" / "retriva"

REMOVED_ROUTES = [
    ("POST", "/api/v1/ingest/chunks"),
    ("POST", "/api/v1/ingest/html"),
    ("POST", "/api/v1/ingest/text"),
    ("POST", "/api/v1/ingest/markdown"),
    ("POST", "/api/v1/ingest/pdf"),
    ("POST", "/api/v1/ingest/upload/pdf"),
    ("POST", "/api/v1/ingest/image"),
    ("POST", "/api/v1/ingest/mediawiki"),
    ("DELETE", "/api/v1/ingest/collection"),
    ("GET", "/api/v1/jobs"),
    ("GET", "/api/v1/jobs/legacy-id"),
    ("POST", "/api/v1/jobs/legacy-id/cancel"),
    ("DELETE", "/api/v1/documents/doc-123"),
    ("DELETE", "/api/v1/documents/metadata/filter"),
]


@pytest.fixture(autouse=True)
def _env_and_mocks(monkeypatch):
    monkeypatch.setenv("RETRIVA_JOBS_DEFAULT_TENANT", "test-tenant")
    with patch("retriva.ingestion_api.main.get_client"), \
            patch("retriva.ingestion_api.main.init_collection"):
        yield


@pytest.fixture()
def client():
    from retriva.ingestion_api.main import app
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Route removal
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path", REMOVED_ROUTES)
def test_removed_route_404(client, method, path):
    kwargs = {"json": {}} if method in ("POST", "PUT", "PATCH") else {}
    response = getattr(client, method.lower())(path, **kwargs)
    assert response.status_code == 404, f"{method} {path}"


def test_removed_routes_absent_from_openapi(client):
    spec = client.get("/openapi.json").json()
    paths = list(spec["paths"].keys())
    assert not [p for p in paths if p.startswith("/api/v1")], paths


def test_discovery_omits_api_v1(client):
    payload = client.get("/").json()
    assert "api_v1" not in payload
    assert payload["api_v2"] == "/api/v2"


def test_openapi_keeps_v2_surface(client):
    spec = client.get("/openapi.json").json()
    v2_paths = [p for p in spec["paths"] if p.startswith("/api/v2")]
    assert v2_paths, "v2 surface must remain advertised"


def test_health_endpoint_unchanged(client):
    assert client.get("/health").json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# CLI: no Retriva API v1 HTTP call anywhere
# ---------------------------------------------------------------------------

def test_cli_source_has_no_v1_http_call():
    text = (_SRC / "cli.py").read_text()
    assert "/api/v1" not in text
    assert "api_version" not in text


def test_cli_image_handler_makes_no_http_call():
    from retriva import cli

    called = []
    with patch.object(
            cli.requests, "post",
            side_effect=lambda *a, **k: called.append(a)), \
            patch.object(
                cli.requests, "delete",
                side_effect=lambda *a, **k: called.append(a)):
        cli.ingest_image_file("/tmp/image.png", "http://127.0.0.1:8000")
    assert called == [], "image ingestion must fail locally without HTTP"


def test_cli_reindex_fails_locally_without_http(monkeypatch, caplog):
    from retriva import cli

    called = []
    with patch.object(
            cli.requests, "post",
            side_effect=lambda *a, **k: called.append(a)), \
            patch.object(
                cli.requests, "delete",
                side_effect=lambda *a, **k: called.append(a)):
        monkeypatch.setattr(
            "sys.argv",
            ["retriva", "reindex", "--path", "/tmp"])
        cli.main()  # must return without raising and without HTTP
    assert called == [], "reindex must fail locally before any HTTP call"


def test_cli_retained_commands_use_v2_only():
    text = (_SRC / "cli.py").read_text()
    assert text.count("/api/v2/documents") >= 3
    assert "/api/v2/documents/mediawiki" in text
    assert "/api/v2/jobs/{job_id}" in text


def test_cli_v2_only_sources_are_intact():
    from retriva import cli
    assert cli.ingest_html_file.__doc__ is not None
    assert "v2" in cli.ingest_html_file.__doc__
    assert "retired" in (cli.ingest_image_file.__doc__ or "")


# ---------------------------------------------------------------------------
# Legacy retirement gates
# ---------------------------------------------------------------------------

def test_job_manager_module_is_retired():
    import sys
    assert not (_SRC / "ingestion_api" / "job_manager.py").exists()
    assert "retriva.ingestion_api.job_manager" not in sys.modules


def test_no_job_manager_import_remains():
    offenders = []
    for py in _SRC.rglob("*.py"):
        text = py.read_text()
        for i, line in enumerate(text.splitlines(), 1):
            if "job_manager" in line and "import" in line:
                offenders.append(f"{py.relative_to(_SRC)}:{i}: {line.strip()}")
    assert offenders == [], offenders


def test_no_legacy_redis_job_state_code_remains():
    offenders = []
    for py in _SRC.rglob("*.py"):
        text = py.read_text()
        for i, line in enumerate(text.splitlines(), 1):
            for key in ("retriva:job:", "retriva:retry:", "retriva:cancel:"):
                if key in line:
                    offenders.append(f"{py.relative_to(_SRC)}:{i}: {line.strip()}")
    assert offenders == [], offenders


def test_no_raw_asyncresult_status_fallback_remains():
    from retriva.ingestion_api import tasks as tasks_module
    assert not hasattr(tasks_module, "get_task_status")
    assert not hasattr(tasks_module, "request_task_cancellation")
    assert not hasattr(tasks_module, "_set_job_state")
    assert not hasattr(tasks_module, "_get_job_state")
    assert not hasattr(tasks_module, "_set_cancel_flag")
    source = (_SRC / "ingestion_api" / "tasks.py").read_text()
    assert "AsyncResult(" not in source
    assert "app.AsyncResult" not in source


def test_no_v1_backgroundtasks_handlers_remain():
    offenders = []
    for py in (_SRC / "ingestion_api").rglob("*.py"):
        text = py.read_text()
        for i, line in enumerate(text.splitlines(), 1):
            if "process_" in line and "_in_background" in line and "def " in line:
                offenders.append(f"{py.name}:{i}: {line.strip()}")
    assert offenders == [], offenders


def test_v1_router_files_are_gone():
    for name in ("ingest.py", "ingest_HTML.py", "ingest_image.py",
                 "ingest_text.py", "ingest_mediawiki.py", "ingest_pdf.py",
                 "ingest_markdown.py", "jobs.py", "documents.py"):
        assert not (_SRC / "ingestion_api" / "routers" / name).exists(), name


def test_relocated_shared_symbols_importable_and_behavioral():
    from retriva.ingestion_api.execution import (
        CancellationError,
        JobStatus,
        TERMINAL_STATES,
    )
    assert JobStatus.COMPLETED == "completed"
    assert JobStatus.COMPLETED in TERMINAL_STATES
    assert issubclass(CancellationError, Exception)


def test_relocated_metadata_validation_behavioral():
    from retriva.ingestion_api.metadata_validation import (
        UserMetadataValidationError,
        validate_user_metadata,
    )
    assert validate_user_metadata(None) is None
    assert validate_user_metadata({"a": "b"}) == {"a": "b"}
    with pytest.raises(UserMetadataValidationError):
        validate_user_metadata({1: "v"})


def test_schemas_v1_module_is_gone():
    assert not (_SRC / "ingestion_api" / "schemas.py").exists()
