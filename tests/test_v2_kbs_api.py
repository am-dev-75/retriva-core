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
Phase 2 integration tests — KB HTTP API + require_kb(s)_exist dependency.

Covers:
- /api/v2/kbs CRUD endpoints (list/create/get/patch/delete).
- 201 / 404 / 409 / 422 mappings from typed domain exceptions.
- Default KB seeded on app startup (via lifespan).
- Default KB cannot be deleted.
- Existing endpoints enforce KB existence:
    * POST /api/v2/documents       — JSON body kb_id validated.
    * POST /api/v2/documents/upload — Form kb_id validated.
    * POST /api/v2/documents/search — every kb_ids[] entry validated.
    * POST /api/v2/retrieval/query  — non-empty kb_ids required.

The Qdrant client is mocked at startup; tests focus on the validation layer
rather than end-to-end ingestion.
"""

from __future__ import annotations

import io
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

# Ensure default implementations are registered before app is imported,
# matching the convention in test_v2_ingestion.py.
import retriva.ingestion.chunker              # noqa: F401
import retriva.ingestion.html_parser          # noqa: F401
import retriva.ingestion.parser_router        # noqa: F401
import retriva.ingestion.tika_client          # noqa: F401
import retriva.ingestion.ocrmypdf_preprocessor  # noqa: F401
import retriva.ingestion.docling_parser       # noqa: F401


@pytest.fixture(autouse=True)
def mock_qdrant_startup():
    """Stub Qdrant during FastAPI startup. KB seeding still runs against
    the session-scoped temp registry (see tests/conftest.py)."""
    with patch("retriva.ingestion_api.main.get_client"), \
         patch("retriva.ingestion_api.main.init_collection"):
        yield


@pytest.fixture
def app():
    from retriva.ingestion_api.main import app as _app
    return _app


@pytest.fixture
def client(app):
    # Using TestClient as a context manager triggers the lifespan, which
    # seeds the default KB into the (temp) registry singleton.
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def isolated_registry_per_test():
    """Replace the registry singleton with a per-test fresh DB so KB CRUD
    tests don't interfere with each other.

    Note: the session-scoped fixture in conftest.py already redirects the
    singleton to a temp dir; here we override it per-function so each test
    starts from a clean slate (no leftover KBs from a previous test).
    """
    from retriva.infrastructure import registry_db as _registry_db_mod
    saved = _registry_db_mod._default_db

    # Point at a brand-new file under the system temp dir.
    import tempfile, os
    tmp_dir = tempfile.mkdtemp(prefix="retriva-kbtest-")
    isolated = _registry_db_mod.RegistryDB(db_path=os.path.join(tmp_dir, "registry.db"))
    _registry_db_mod._default_db = isolated
    try:
        yield isolated
    finally:
        _registry_db_mod._default_db = saved


# ---------------------------------------------------------------------------
# Health: seeding via lifespan
# ---------------------------------------------------------------------------

class TestSeeding:
    def test_default_kb_present_after_startup(self, client):
        resp = client.get("/api/v2/kbs")
        assert resp.status_code == 200
        kbs = resp.json()["kbs"]
        ids = [kb["kb_id"] for kb in kbs]
        assert "default" in ids
        default = next(kb for kb in kbs if kb["kb_id"] == "default")
        assert default["name"] == "default"


# ---------------------------------------------------------------------------
# CRUD happy paths
# ---------------------------------------------------------------------------

class TestCRUD:
    def test_create_with_explicit_kb_id(self, client):
        resp = client.post(
            "/api/v2/kbs",
            json={"kb_id": "eng", "name": "Engineering", "description": "Docs"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["kb_id"] == "eng"
        assert body["name"] == "Engineering"
        assert body["description"] == "Docs"
        assert body["document_count"] == 0
        assert body["settings"] == {}

    def test_create_derives_kb_id_from_name(self, client):
        resp = client.post("/api/v2/kbs", json={"name": "Customer Support"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["kb_id"] == "customer-support"

    def test_create_duplicate_conflicts(self, client):
        client.post("/api/v2/kbs", json={"kb_id": "dup", "name": "first"})
        resp = client.post("/api/v2/kbs", json={"kb_id": "dup", "name": "second"})
        assert resp.status_code == 409

    def test_create_invalid_kb_id_unprocessable(self, client):
        resp = client.post("/api/v2/kbs", json={"kb_id": "UPPER", "name": "x"})
        assert resp.status_code == 422

    def test_create_empty_name_unprocessable(self, client):
        resp = client.post("/api/v2/kbs", json={"name": "   "})
        assert resp.status_code == 422

    def test_get_existing(self, client):
        client.post("/api/v2/kbs", json={"kb_id": "x", "name": "X"})
        resp = client.get("/api/v2/kbs/x")
        assert resp.status_code == 200
        assert resp.json()["kb_id"] == "x"

    def test_get_missing(self, client):
        resp = client.get("/api/v2/kbs/missing")
        assert resp.status_code == 404

    def test_get_invalid_kb_id(self, client):
        # Invalid character in path → 422 from the registry validator.
        resp = client.get("/api/v2/kbs/UPPER")
        assert resp.status_code == 422

    def test_patch_name(self, client):
        client.post("/api/v2/kbs", json={"kb_id": "x", "name": "X"})
        resp = client.patch("/api/v2/kbs/x", json={"name": "X v2"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "X v2"

    def test_patch_missing(self, client):
        resp = client.patch("/api/v2/kbs/missing", json={"name": "X"})
        assert resp.status_code == 404

    def test_delete_existing(self, client):
        client.post("/api/v2/kbs", json={"kb_id": "tmp", "name": "T"})
        resp = client.delete("/api/v2/kbs/tmp")
        assert resp.status_code == 204
        assert client.get("/api/v2/kbs/tmp").status_code == 404

    def test_delete_missing(self, client):
        resp = client.delete("/api/v2/kbs/missing")
        assert resp.status_code == 404

    def test_delete_default_refused(self, client):
        # The 'default' KB is seeded by lifespan and must be undeletable.
        resp = client.delete("/api/v2/kbs/default")
        assert resp.status_code == 409
        # Still listed.
        listed = [kb["kb_id"] for kb in client.get("/api/v2/kbs").json()["kbs"]]
        assert "default" in listed


# ---------------------------------------------------------------------------
# KB enforcement on existing endpoints
# ---------------------------------------------------------------------------

class TestKBEnforcementOnExistingEndpoints:
    def test_ingest_unknown_kb_returns_404(self, client):
        resp = client.post(
            "/api/v2/documents",
            json={"source_uri": "file:///nonexistent.txt", "kb_id": "no-such-kb"},
        )
        assert resp.status_code == 404
        assert "no-such-kb" in resp.json()["detail"]

    def test_ingest_default_kb_accepted(self, client):
        # 'default' is seeded by lifespan; the endpoint should accept the
        # ingest request and return 202 (the background task may fail later
        # in the docling pipeline, but that is irrelevant to this test).
        resp = client.post(
            "/api/v2/documents",
            json={"source_uri": "file:///irrelevant.txt", "kb_id": "default"},
        )
        assert resp.status_code == 202, resp.text

    def test_upload_unknown_kb_returns_404(self, client):
        resp = client.post(
            "/api/v2/documents/upload",
            data={"source_path": "irrelevant.txt", "kb_id": "no-such-kb"},
            files={"file": ("irrelevant.txt", io.BytesIO(b"hello"), "text/plain")},
        )
        assert resp.status_code == 404

    def test_search_unknown_kb_in_list_returns_404(self, client):
        resp = client.post(
            "/api/v2/documents/search",
            json={"query": "anything", "kb_ids": ["default", "no-such-kb"]},
        )
        assert resp.status_code == 404
        assert "no-such-kb" in resp.json()["detail"]

    def test_search_empty_kb_ids_allowed(self, client):
        # Empty kb_ids means "all KBs" — must not 404 just because the list
        # is empty. We mock out the search_documents helper to avoid the
        # full Qdrant path; the assertion is that validation lets the call
        # proceed (no 4xx).
        with patch("retriva.indexing.qdrant_store.search_documents", return_value=[]):
            resp = client.post(
                "/api/v2/documents/search",
                json={"query": "anything", "kb_ids": []},
            )
        assert resp.status_code == 200, resp.text

    def test_retrieval_unknown_kb_returns_404(self, client):
        resp = client.post(
            "/api/v2/retrieval/query",
            json={"query": "anything", "kb_ids": ["no-such-kb"]},
        )
        assert resp.status_code == 404

    def test_retrieval_empty_kb_ids_rejected(self, client):
        resp = client.post(
            "/api/v2/retrieval/query",
            json={"query": "anything", "kb_ids": []},
        )
        assert resp.status_code == 422
