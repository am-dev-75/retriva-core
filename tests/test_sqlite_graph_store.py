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

"""Unit tests for SQLiteGraphStore."""

import pytest
from retriva.graph.contracts import (
    Assertion,
    AssertionStatus,
    Entity,
    EntityCategory,
    GraphMutationRequest,
    Relationship,
)
from retriva.graph.stores.sqlite_graph_store import SQLiteGraphStore


@pytest.fixture
def store(tmp_path):
    """Provide a fresh SQLiteGraphStore for each test."""
    db_path = str(tmp_path / "test_graph.db")
    return SQLiteGraphStore(db_path=db_path)


class TestEntityCRUD:
    def test_upsert_and_get(self, store):
        e = Entity(
            entity_id="ent_1", tenant_id="col1", kb_id="kb1",
            name="Acme Corp", name_normalized="acme corp",
            category=EntityCategory.ORGANIZATION,
            aliases=["Acme", "ACME"],
            external_ids={"salesforce": "001A"},
            security_scope=["kb1"],
        )
        store.upsert_entities([e])
        result = store.get_entity("ent_1")
        assert result is not None
        assert result.name == "Acme Corp"
        assert result.category == EntityCategory.ORGANIZATION
        assert "Acme" in result.aliases
        assert result.external_ids["salesforce"] == "001A"

    def test_upsert_is_idempotent(self, store):
        e = Entity(
            entity_id="ent_1", tenant_id="col1", kb_id="kb1",
            name="Acme Corp", name_normalized="acme corp",
        )
        store.upsert_entities([e])
        store.upsert_entities([e])
        result = store.get_entity("ent_1")
        assert result is not None
        assert result.name == "Acme Corp"

    def test_get_nonexistent(self, store):
        assert store.get_entity("nonexistent") is None

    def test_get_by_alias(self, store):
        e = Entity(
            entity_id="ent_1", tenant_id="col1", kb_id="kb1",
            name="Acme Corp", name_normalized="acme corp",
            aliases=["ACME Inc"],
        )
        store.upsert_entities([e])
        # Search by canonical name
        results = store.get_entities_by_alias("Acme Corp", "col1", "kb1")
        assert len(results) == 1
        # Search by alias
        results = store.get_entities_by_alias("ACME Inc", "col1", "kb1")
        assert len(results) == 1
        # Case insensitive
        results = store.get_entities_by_alias("acme corp", "col1", "kb1")
        assert len(results) == 1

    def test_get_by_external_id(self, store):
        e = Entity(
            entity_id="ent_1", tenant_id="col1", kb_id="kb1",
            name="Acme Corp", name_normalized="acme corp",
            external_ids={"salesforce": "001A"},
        )
        store.upsert_entities([e])
        result = store.get_entities_by_external_id("salesforce", "001A", "col1")
        assert result is not None
        assert result.entity_id == "ent_1"

    def test_tenant_isolation(self, store):
        e1 = Entity(
            entity_id="ent_1", tenant_id="col1", kb_id="kb1",
            name="Acme", name_normalized="acme",
        )
        e2 = Entity(
            entity_id="ent_2", tenant_id="col2", kb_id="kb1",
            name="Acme", name_normalized="acme",
        )
        store.upsert_entities([e1, e2])
        # col1 should only find ent_1
        results = store.get_entities_by_alias("Acme", "col1", "kb1")
        assert len(results) == 1
        assert results[0].entity_id == "ent_1"
        # col2 should only find ent_2
        results = store.get_entities_by_alias("Acme", "col2", "kb1")
        assert len(results) == 1
        assert results[0].entity_id == "ent_2"


class TestAssertionCRUD:
    def test_upsert_and_get(self, store):
        e = Entity(entity_id="ent_1", tenant_id="col1", kb_id="kb1", name="Test")
        store.upsert_entities([e])
        a = Assertion(
            assertion_id="ast_1", tenant_id="col1", kb_id="kb1",
            subject_entity_id="ent_1", predicate="retriva:worksFor",
            object_entity_id="ent_2",
            source_document_ids=["doc_1"],
            source_chunk_ids=["chunk_1"],
        )
        store.upsert_assertions([a])
        results = store.get_assertions("ent_1")
        assert len(results) == 1
        assert results[0].predicate == "retriva:worksFor"

    def test_status_filter(self, store):
        e = Entity(entity_id="ent_1", tenant_id="col1", kb_id="kb1", name="Test")
        store.upsert_entities([e])
        a1 = Assertion(
            assertion_id="ast_1", tenant_id="col1", kb_id="kb1",
            subject_entity_id="ent_1", predicate="retriva:p1",
            status=AssertionStatus.ACTIVE,
        )
        a2 = Assertion(
            assertion_id="ast_2", tenant_id="col1", kb_id="kb1",
            subject_entity_id="ent_1", predicate="retriva:p2",
            status=AssertionStatus.INVALIDATED,
        )
        store.upsert_assertions([a1, a2])
        active = store.get_assertions("ent_1", status=AssertionStatus.ACTIVE)
        assert len(active) == 1
        assert active[0].assertion_id == "ast_1"


class TestNeighborhood:
    def test_simple_neighborhood(self, store):
        # Create: A → B → C
        entities = [
            Entity(entity_id="A", tenant_id="col1", kb_id="kb1", name="A"),
            Entity(entity_id="B", tenant_id="col1", kb_id="kb1", name="B"),
            Entity(entity_id="C", tenant_id="col1", kb_id="kb1", name="C"),
        ]
        rels = [
            Relationship(
                relationship_id="r1", tenant_id="col1", kb_id="kb1",
                source_entity_id="A", target_entity_id="B",
                predicate="retriva:knows",
            ),
            Relationship(
                relationship_id="r2", tenant_id="col1", kb_id="kb1",
                source_entity_id="B", target_entity_id="C",
                predicate="retriva:knows",
            ),
        ]
        store.upsert_entities(entities)
        store.upsert_relationships(rels)

        result = store.get_neighborhood(
            "A", max_depth=2, max_nodes=50, max_edges=100,
            security_scope=["kb1"],
        )
        entity_ids = {e.entity_id for e in result.entities}
        assert "A" in entity_ids
        assert "B" in entity_ids
        assert "C" in entity_ids

    def test_depth_limit(self, store):
        entities = [
            Entity(entity_id="A", tenant_id="col1", kb_id="kb1", name="A"),
            Entity(entity_id="B", tenant_id="col1", kb_id="kb1", name="B"),
            Entity(entity_id="C", tenant_id="col1", kb_id="kb1", name="C"),
        ]
        rels = [
            Relationship(
                relationship_id="r1", tenant_id="col1", kb_id="kb1",
                source_entity_id="A", target_entity_id="B",
                predicate="retriva:knows",
            ),
            Relationship(
                relationship_id="r2", tenant_id="col1", kb_id="kb1",
                source_entity_id="B", target_entity_id="C",
                predicate="retriva:knows",
            ),
        ]
        store.upsert_entities(entities)
        store.upsert_relationships(rels)

        # Depth 1: A → B (but not C)
        result = store.get_neighborhood(
            "A", max_depth=1, max_nodes=50, max_edges=100,
            security_scope=["kb1"],
        )
        entity_ids = {e.entity_id for e in result.entities}
        assert "A" in entity_ids
        assert "B" in entity_ids
        assert "C" not in entity_ids

    def test_max_nodes_truncation(self, store):
        entities = [Entity(entity_id=f"E{i}", tenant_id="col1", kb_id="kb1", name=f"E{i}") for i in range(10)]
        rels = [
            Relationship(
                relationship_id=f"r{i}", tenant_id="col1", kb_id="kb1",
                source_entity_id="E0", target_entity_id=f"E{i}",
                predicate="retriva:knows",
            )
            for i in range(1, 10)
        ]
        store.upsert_entities(entities)
        store.upsert_relationships(rels)

        result = store.get_neighborhood(
            "E0", max_depth=1, max_nodes=3, max_edges=100,
            security_scope=["kb1"],
        )
        assert result.truncated is True
        assert len(result.entities) <= 3


class TestPathFinding:
    def test_direct_path(self, store):
        entities = [
            Entity(entity_id="A", tenant_id="col1", kb_id="kb1", name="A"),
            Entity(entity_id="B", tenant_id="col1", kb_id="kb1", name="B"),
        ]
        rels = [Relationship(
            relationship_id="r1", tenant_id="col1", kb_id="kb1",
            source_entity_id="A", target_entity_id="B",
            predicate="retriva:knows",
        )]
        store.upsert_entities(entities)
        store.upsert_relationships(rels)

        paths = store.find_paths("A", "B", max_depth=3, security_scope=["kb1"])
        assert len(paths) >= 1
        assert paths[0].start_entity_id == "A"
        assert paths[0].end_entity_id == "B"

    def test_no_path(self, store):
        entities = [
            Entity(entity_id="A", tenant_id="col1", kb_id="kb1", name="A"),
            Entity(entity_id="B", tenant_id="col1", kb_id="kb1", name="B"),
        ]
        store.upsert_entities(entities)
        paths = store.find_paths("A", "B", max_depth=3, security_scope=["kb1"])
        assert len(paths) == 0

    def test_same_entity(self, store):
        e = Entity(entity_id="A", tenant_id="col1", kb_id="kb1", name="A")
        store.upsert_entities([e])
        paths = store.find_paths("A", "A", max_depth=3, security_scope=["kb1"])
        assert len(paths) == 1
        assert paths[0].entity_ids == ["A"]


class TestDeleteBySource:
    def test_delete_invalidates_assertions(self, store):
        e = Entity(entity_id="ent_1", tenant_id="col1", kb_id="kb1", name="Test")
        store.upsert_entities([e])
        a = Assertion(
            assertion_id="ast_1", tenant_id="col1", kb_id="kb1",
            subject_entity_id="ent_1", predicate="retriva:test",
            source_document_ids=["doc_1"],
        )
        store.upsert_assertions([a])
        store.store_provenance("doc_1", ["chunk_1"], ["ent_1"], ["ast_1"], "col1", "kb1")

        affected = store.delete_by_source("doc_1")
        assert affected > 0

        # Assertion should be invalidated
        results = store.get_assertions("ent_1")
        assert all(r.status == AssertionStatus.INVALIDATED for r in results)

    def test_idempotent(self, store):
        e = Entity(entity_id="ent_1", tenant_id="col1", kb_id="kb1", name="Test")
        store.upsert_entities([e])
        a = Assertion(
            assertion_id="ast_1", tenant_id="col1", kb_id="kb1",
            subject_entity_id="ent_1", predicate="retriva:test",
            source_document_ids=["doc_1"],
        )
        store.upsert_assertions([a])

        store.delete_by_source("doc_1")
        # Second call should not raise
        store.delete_by_source("doc_1")


class TestSearch:
    def test_search_by_name(self, store):
        entities = [
            Entity(entity_id="ent_1", tenant_id="col1", kb_id="kb1",
                   name="Acme Corporation", name_normalized="acme corporation"),
            Entity(entity_id="ent_2", tenant_id="col1", kb_id="kb1",
                   name="Globex", name_normalized="globex"),
        ]
        store.upsert_entities(entities)

        results = store.search_entities("acme", "col1", "kb1")
        assert len(results) == 1
        assert results[0].name == "Acme Corporation"

    def test_search_tenant_isolation(self, store):
        e1 = Entity(entity_id="ent_1", tenant_id="col1", kb_id="kb1",
                    name="Acme", name_normalized="acme")
        e2 = Entity(entity_id="ent_2", tenant_id="col2", kb_id="kb1",
                    name="Acme", name_normalized="acme")
        store.upsert_entities([e1, e2])

        results = store.search_entities("acme", "col1", "kb1")
        assert len(results) == 1
        assert results[0].entity_id == "ent_1"
