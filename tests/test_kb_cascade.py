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
Phase 3 tests — KB cascade-on-delete.

Verifies:
- ``delete_chunks_by_kb_id`` issues a filtered Qdrant delete and returns
  the observed point count.
- ``DeduplicationStore.delete_by_kb_id`` removes only matching records and
  is idempotent.
- ``DELETE /api/v2/kbs/{kb_id}`` cascades in the SDD-specified order
  (points → dedup → registry row) and surfaces 5xx with partial-state
  details on intermediate failures.
- The ``default`` KB is refused before any cascade work runs (invariant
  enforced *before* the destructive steps).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# Ensure default registrations before the FastAPI app is imported, matching
# the convention used by other v2 test modules.
import retriva.ingestion.chunker              # noqa: F401
import retriva.ingestion.html_parser          # noqa: F401
import retriva.ingestion.parser_router        # noqa: F401
import retriva.ingestion.tika_client          # noqa: F401
import retriva.ingestion.ocrmypdf_preprocessor  # noqa: F401
import retriva.ingestion.docling_parser       # noqa: F401


# ---------------------------------------------------------------------------
# delete_chunks_by_kb_id — unit
# ---------------------------------------------------------------------------

class TestQdrantDeleteByKbId:
    def test_issues_filtered_delete_with_kb_id_payload_match(self):
        from retriva.indexing.qdrant_store import (
            delete_chunks_by_kb_id,
            COLLECTION_NAME,
        )
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        client = MagicMock()
        client.scroll.return_value = ([MagicMock(), MagicMock(), MagicMock()], None)

        observed = delete_chunks_by_kb_id(client, "kb_x")

        # Pre-count scroll observed 3 records.
        assert observed == 3

        # Exactly one delete call against the canonical collection.
        client.delete.assert_called_once()
        kwargs = client.delete.call_args.kwargs
        assert kwargs["collection_name"] == COLLECTION_NAME

        # The selector must be a Filter(must=[FieldCondition(key='kb_id', ...)])
        selector = kwargs["points_selector"]
        assert isinstance(selector, Filter)
        assert len(selector.must) == 1
        cond = selector.must[0]
        assert isinstance(cond, FieldCondition)
        assert cond.key == "kb_id"
        assert isinstance(cond.match, MatchValue)
        assert cond.match.value == "kb_x"

    def test_returns_zero_when_no_points_match(self):
        from retriva.indexing.qdrant_store import delete_chunks_by_kb_id

        client = MagicMock()
        client.scroll.return_value = ([], None)

        observed = delete_chunks_by_kb_id(client, "empty_kb")

        assert observed == 0
        # Delete still runs (idempotent — Qdrant treats no-match as no-op).
        client.delete.assert_called_once()

    def test_precount_failure_does_not_block_delete(self):
        from retriva.indexing.qdrant_store import delete_chunks_by_kb_id

        client = MagicMock()
        client.scroll.side_effect = RuntimeError("qdrant transient error")

        # Even if scroll fails, delete must still run (best-effort precount).
        observed = delete_chunks_by_kb_id(client, "kb_y")

        assert observed == 0
        client.delete.assert_called_once()


# ---------------------------------------------------------------------------
# DeduplicationStore.delete_by_kb_id — unit
# ---------------------------------------------------------------------------

class TestDedupDeleteByKbId:
    def _make_store(self, tmp_path: Path):
        from retriva.ingestion.dedup import DeduplicationStore

        catalog = tmp_path / "dedup.json"
        return DeduplicationStore(catalog_path=str(catalog)), catalog

    def _seed(self, catalog: Path, records):
        catalog.write_text(json.dumps({"records": records}))

    def test_removes_only_matching_records(self, tmp_path):
        store, catalog = self._make_store(tmp_path)
        self._seed(
            catalog,
            [
                {"doc_id": "d1", "kb_id": "alpha", "content_hash": "h1"},
                {"doc_id": "d2", "kb_id": "beta", "content_hash": "h2"},
                {"doc_id": "d3", "kb_id": "alpha", "content_hash": "h3"},
                {"doc_id": "d4", "kb_id": "gamma", "content_hash": "h4"},
            ],
        )

        removed = store.delete_by_kb_id("alpha")
        assert removed == 2

        remaining = json.loads(catalog.read_text())["records"]
        kb_ids = sorted(r["kb_id"] for r in remaining)
        assert kb_ids == ["beta", "gamma"]

    def test_idempotent_on_unknown_kb(self, tmp_path):
        store, catalog = self._make_store(tmp_path)
        self._seed(
            catalog,
            [{"doc_id": "d1", "kb_id": "beta", "content_hash": "h1"}],
        )

        assert store.delete_by_kb_id("nope") == 0
        # Catalog is unchanged.
        remaining = json.loads(catalog.read_text())["records"]
        assert len(remaining) == 1
        assert remaining[0]["kb_id"] == "beta"

    def test_empty_catalog_returns_zero(self, tmp_path):
        store, _ = self._make_store(tmp_path)
        assert store.delete_by_kb_id("anything") == 0


# ---------------------------------------------------------------------------
# DELETE /api/v2/kbs/{kb_id} — end-to-end cascade
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_qdrant_startup():
    """Stub Qdrant during FastAPI lifespan startup."""
    with patch("retriva.ingestion_api.main.get_client"), \
         patch("retriva.ingestion_api.main.init_collection"):
        yield


@pytest.fixture
def app():
    from retriva.ingestion_api.main import app as _app
    return _app


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def isolated_dedup_catalog(tmp_path, monkeypatch):
    """Redirect ``DeduplicationStore`` default catalog to a per-test file.

    The store reads ``settings.storage_dir`` lazily inside ``__init__``; we
    monkey-patch the constructor's default-path logic via a thin wrapper
    that all router code already routes through.
    """
    from retriva.ingestion import dedup

    catalog = tmp_path / "dedup.json"
    catalog.write_text(json.dumps({"records": []}))

    real_init = dedup.DeduplicationStore.__init__

    def isolated_init(self, catalog_path=None):
        if catalog_path is None:
            catalog_path = str(catalog)
        real_init(self, catalog_path=catalog_path)

    monkeypatch.setattr(dedup.DeduplicationStore, "__init__", isolated_init)
    return catalog


@pytest.fixture(autouse=True)
def isolated_registry_per_test():
    """Per-test fresh registry singleton (same pattern as test_v2_kbs_api.py)."""
    from retriva.infrastructure import registry_db as _registry_db_mod
    import os
    saved = _registry_db_mod._default_db
    tmp_dir = tempfile.mkdtemp(prefix="retriva-cascade-")
    isolated = _registry_db_mod.RegistryDB(db_path=os.path.join(tmp_dir, "registry.db"))
    _registry_db_mod._default_db = isolated
    try:
        yield isolated
    finally:
        _registry_db_mod._default_db = saved


class TestCascadeEndToEnd:
    def test_happy_path_deletes_points_then_dedup_then_registry(
        self, client, isolated_dedup_catalog
    ):
        # Arrange: create KB and seed a dedup record under that KB.
        client.post("/api/v2/kbs", json={"kb_id": "kb_drop", "name": "Drop"})
        catalog = isolated_dedup_catalog
        catalog.write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "doc_id": "d1",
                            "kb_id": "kb_drop",
                            "content_hash": "h1",
                            "collection_name": "retriva_chunks",
                        },
                        {
                            "doc_id": "d2",
                            "kb_id": "default",
                            "content_hash": "h2",
                            "collection_name": "retriva_chunks",
                        },
                    ]
                }
            )
        )

        # Mock the Qdrant delete helper so we can observe the call without
        # needing a running cluster, and assert the cascade *order*.
        call_log = []

        def fake_delete_points(client_, kb_id_):
            call_log.append(("points", kb_id_))
            return 7

        with patch(
            "retriva.ingestion_api.routers.v2_kbs.delete_chunks_by_kb_id",
            side_effect=fake_delete_points,
        ):
            # Wrap dedup.delete_by_kb_id only to record ordering.
            from retriva.ingestion.dedup import DeduplicationStore

            real_dedup_delete = DeduplicationStore.delete_by_kb_id

            def wrapped(self, kb_id_):
                call_log.append(("dedup", kb_id_))
                return real_dedup_delete(self, kb_id_)

            with patch.object(DeduplicationStore, "delete_by_kb_id", wrapped):
                resp = client.delete("/api/v2/kbs/kb_drop")

        assert resp.status_code == 204, resp.text

        # Order: points first, then dedup.
        assert [c[0] for c in call_log] == ["points", "dedup"]
        assert all(c[1] == "kb_drop" for c in call_log)

        # Registry row is gone.
        assert client.get("/api/v2/kbs/kb_drop").status_code == 404

        # Dedup catalog: kb_drop record removed; default record kept.
        remaining = json.loads(catalog.read_text())["records"]
        assert [r["kb_id"] for r in remaining] == ["default"]

    def test_default_kb_refused_before_any_cascade_work(
        self, client, isolated_dedup_catalog
    ):
        # Seed dedup catalog with a 'default'-tagged record to prove it
        # survives the refused delete.
        catalog = isolated_dedup_catalog
        catalog.write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "doc_id": "d_default",
                            "kb_id": "default",
                            "content_hash": "h",
                            "collection_name": "retriva_chunks",
                        }
                    ]
                }
            )
        )

        with patch(
            "retriva.ingestion_api.routers.v2_kbs.delete_chunks_by_kb_id"
        ) as mock_points_delete:
            resp = client.delete("/api/v2/kbs/default")

        assert resp.status_code == 409
        # Critical invariant: no destructive call was issued.
        mock_points_delete.assert_not_called()

        # Dedup record still present.
        remaining = json.loads(catalog.read_text())["records"]
        assert any(r["kb_id"] == "default" for r in remaining)

    def test_unknown_kb_returns_404_before_any_cascade_work(self, client):
        with patch(
            "retriva.ingestion_api.routers.v2_kbs.delete_chunks_by_kb_id"
        ) as mock_points_delete:
            resp = client.delete("/api/v2/kbs/never-existed")

        assert resp.status_code == 404
        mock_points_delete.assert_not_called()

    def test_qdrant_failure_aborts_cascade_with_500(
        self, client, isolated_dedup_catalog
    ):
        client.post("/api/v2/kbs", json={"kb_id": "kb_x", "name": "X"})
        catalog = isolated_dedup_catalog
        catalog.write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "doc_id": "d1",
                            "kb_id": "kb_x",
                            "content_hash": "h1",
                            "collection_name": "retriva_chunks",
                        }
                    ]
                }
            )
        )

        from retriva.ingestion.dedup import DeduplicationStore

        with patch(
            "retriva.ingestion_api.routers.v2_kbs.delete_chunks_by_kb_id",
            side_effect=RuntimeError("qdrant down"),
        ), patch.object(
            DeduplicationStore, "delete_by_kb_id"
        ) as mock_dedup_delete:
            resp = client.delete("/api/v2/kbs/kb_x")

        assert resp.status_code == 500
        # Cascade aborted: dedup never touched, registry row preserved.
        mock_dedup_delete.assert_not_called()
        assert client.get("/api/v2/kbs/kb_x").status_code == 200

        # Dedup record still present (Qdrant step failed first).
        remaining = json.loads(catalog.read_text())["records"]
        assert any(r["kb_id"] == "kb_x" for r in remaining)

    def test_dedup_failure_after_points_deleted_reports_partial_state(
        self, client
    ):
        client.post("/api/v2/kbs", json={"kb_id": "kb_y", "name": "Y"})

        from retriva.ingestion.dedup import DeduplicationStore

        with patch(
            "retriva.ingestion_api.routers.v2_kbs.delete_chunks_by_kb_id",
            return_value=4,
        ), patch.object(
            DeduplicationStore,
            "delete_by_kb_id",
            side_effect=RuntimeError("disk full"),
        ):
            resp = client.delete("/api/v2/kbs/kb_y")

        assert resp.status_code == 500
        # The error detail must mention the partial-state (points already gone).
        detail = resp.json()["detail"]
        assert "kb_y" in detail
        assert "dedup" in detail.lower()

        # Registry row still present — only step 4 (registry delete) is
        # skipped on partial-failure.
        assert client.get("/api/v2/kbs/kb_y").status_code == 200
