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

"""Regression (2026-11-06): GraphIndexer must not store assertions that
reference entities absent from the resolved mutation.

The observed failure was ``graph_assertions.subject_entity_id`` ->
``graph_entities`` FOREIGN KEY.  Validation can skip an extracted entity
while assertions still reference it; the indexer now filters those
assertions deterministically instead of letting SQLite FK-fail.
"""

from __future__ import annotations

import pytest

from retriva.config import settings
from retriva.graph.contracts import (
    Assertion,
    Entity,
    GraphMutationRequest,
)
from retriva.graph.graph_indexer import GraphIndexer
from retriva.graph.stores.sqlite_graph_store import SQLiteGraphStore
from retriva.indexing import qdrant_store


class _FakeExtractor:
    last_metrics = {"warnings": []}

    def extract(self, **kwargs):
        return GraphMutationRequest(
            tenant_id="col1",
            kb_id="kb1",
            entities=[Entity(
                entity_id="tmp_a", tenant_id="col1", kb_id="kb1",
                name="A", name_normalized="a")],
            assertions=[
                # Subject entity is absent from the mutation.
                Assertion(
                    assertion_id="ast_bad", tenant_id="col1", kb_id="kb1",
                    subject_entity_id="tmp_missing", predicate="retriva:p",
                    extraction_confidence=0.9),
                # Object entity is absent, but the assertion carries a value:
                # it must be kept with a null object_entity_id.
                Assertion(
                    assertion_id="ast_value", tenant_id="col1", kb_id="kb1",
                    subject_entity_id="tmp_a", predicate="retriva:p",
                    object_entity_id="tmp_missing", object_value="value",
                    extraction_confidence=0.9),
            ],
            source_document_id="doc_1",
        )


def test_indexer_filters_assertions_with_missing_entities(
        tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "graph_enabled", True)
    monkeypatch.setattr(qdrant_store, "get_collection_name", lambda: "col1")
    store = SQLiteGraphStore(db_path=str(tmp_path / "graph.db"))
    indexer = GraphIndexer()
    indexer._store = store
    indexer._extractor = _FakeExtractor()

    result = indexer.index_document(
        doc_id="doc_1", kb_id="kb1",
        chunks=[{"text": "hello", "chunk_id": "chunk_1"}])

    assert result["skipped"] is False
    assert result["assertions"] == 1
    assert result["skipped_assertions"] == 1
    entities = store.get_entities_by_alias("A", "col1", "kb1")
    assert entities
    stored = store.get_assertions(entities[0].entity_id)
    assert len(stored) == 1
    assert stored[0].assertion_id == "ast_value"
    assert stored[0].object_entity_id is None
    assert stored[0].object_value == "value"
