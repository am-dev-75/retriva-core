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

"""Unit tests for GraphRAG contracts (Pydantic models)."""

import pytest
from retriva.graph.contracts import (
    Assertion,
    AssertionClass,
    AssertionStatus,
    Entity,
    EntityCategory,
    GraphChangeEvent,
    GraphEventType,
    GraphMutationRequest,
    GraphPath,
    GraphQueryRequest,
    GraphRetrievalResult,
    Relationship,
    RetrievalMode,
    VocabularyRegistration,
)


class TestEntity:
    def test_default_values(self):
        e = Entity(tenant_id="col1", kb_id="kb1", name="Acme Corp")
        assert e.entity_id.startswith("ent_")
        assert e.category == EntityCategory.UNKNOWN
        assert e.namespace == "retriva"
        assert e.aliases == []
        assert e.external_ids == {}
        assert e.security_scope == []
        assert e.created_at is not None
        assert e.updated_at is not None

    def test_with_all_fields(self):
        e = Entity(
            tenant_id="col1",
            kb_id="kb1",
            name="Acme Corp",
            name_normalized="acme corp",
            category=EntityCategory.ORGANIZATION,
            entity_type="crm:Account",
            namespace="crm",
            aliases=["Acme", "ACME"],
            external_ids={"salesforce": "001A"},
            description="A company",
            security_scope=["kb1"],
        )
        assert e.category == EntityCategory.ORGANIZATION
        assert e.entity_type == "crm:Account"
        assert "Acme" in e.aliases
        assert e.external_ids["salesforce"] == "001A"

    def test_serialization_roundtrip(self):
        e = Entity(
            tenant_id="col1", kb_id="kb1", name="Test",
            category=EntityCategory.PERSON,
        )
        d = e.model_dump()
        e2 = Entity(**d)
        assert e2.entity_id == e.entity_id
        assert e2.category == e.category


class TestAssertion:
    def test_default_values(self):
        a = Assertion(
            tenant_id="col1", kb_id="kb1",
            subject_entity_id="ent_1", predicate="retriva:worksFor",
        )
        assert a.assertion_id.startswith("ast_")
        assert a.assertion_class == AssertionClass.EXTRACTED
        assert a.status == AssertionStatus.ACTIVE
        assert a.extraction_confidence == 0.0
        assert a.extractor_profile == "retriva:default"

    def test_literal_assertion(self):
        a = Assertion(
            tenant_id="col1", kb_id="kb1",
            subject_entity_id="ent_1", predicate="retriva:hasRevenue",
            object_value="1000000",
        )
        assert a.object_entity_id is None
        assert a.object_value == "1000000"

    def test_entity_assertion(self):
        a = Assertion(
            tenant_id="col1", kb_id="kb1",
            subject_entity_id="ent_1", predicate="retriva:worksFor",
            object_entity_id="ent_2",
        )
        assert a.object_entity_id == "ent_2"
        assert a.object_value is None

    def test_temporal_fields(self):
        a = Assertion(
            tenant_id="col1", kb_id="kb1",
            subject_entity_id="ent_1", predicate="retriva:ceo",
            valid_from="2026-01-01T00:00:00Z",
            valid_to="2026-06-30T00:00:00Z",
            observed_at="2026-01-15T00:00:00Z",
        )
        assert a.valid_from == "2026-01-01T00:00:00Z"
        assert a.valid_to == "2026-06-30T00:00:00Z"

    def test_supersession(self):
        a = Assertion(
            tenant_id="col1", kb_id="kb1",
            subject_entity_id="ent_1", predicate="retriva:ceo",
            status=AssertionStatus.SUPERSEDED,
            superseded_by="ast_new",
        )
        assert a.status == AssertionStatus.SUPERSEDED
        assert a.superseded_by == "ast_new"


class TestRetrievalMode:
    def test_values(self):
        assert RetrievalMode.VECTOR.value == "vector"
        assert RetrievalMode.GRAPH_LOCAL.value == "graph_local"
        assert RetrievalMode.GRAPH_GLOBAL.value == "graph_global"
        assert RetrievalMode.HYBRID.value == "hybrid"
        assert RetrievalMode.AUTO.value == "auto"


class TestGraphChangeEvent:
    def test_default_values(self):
        e = GraphChangeEvent(
            event_type=GraphEventType.ENTITY_CREATED,
            tenant_id="col1", kb_id="kb1",
        )
        assert e.event_id.startswith("gce_")
        assert e.event_type == GraphEventType.ENTITY_CREATED
        assert e.entity_ids == []
        assert e.timestamp is not None

    def test_all_event_types(self):
        for et in GraphEventType:
            e = GraphChangeEvent(
                event_type=et, tenant_id="col1", kb_id="kb1",
            )
            assert e.event_type == et


class TestVocabularyRegistration:
    def test_crm_namespace(self):
        v = VocabularyRegistration(
            namespace="crm",
            entity_types=["crm:Opportunity", "crm:Contact"],
            predicates=["crm:blockedBy", "crm:ownedBy"],
        )
        assert "crm:Opportunity" in v.entity_types
        assert "crm:blockedBy" in v.predicates

    def test_neutral_example_namespace(self):
        v = VocabularyRegistration(
            namespace="example",
            entity_types=["example:ProjectRisk"],
            predicates=["example:dependsOn"],
        )
        assert "example:ProjectRisk" in v.entity_types
        assert "example:dependsOn" in v.predicates


class TestGraphMutationRequest:
    def test_empty_mutation(self):
        m = GraphMutationRequest(
            tenant_id="col1", kb_id="kb1",
            source_document_id="doc_1",
        )
        assert m.entities == []
        assert m.assertions == []
        assert m.relationships == []

    def test_with_data(self):
        e = Entity(tenant_id="col1", kb_id="kb1", name="Test")
        a = Assertion(
            tenant_id="col1", kb_id="kb1",
            subject_entity_id=e.entity_id, predicate="retriva:test",
        )
        m = GraphMutationRequest(
            tenant_id="col1", kb_id="kb1",
            entities=[e], assertions=[a],
            source_document_id="doc_1",
            source_chunk_ids=["chunk_1"],
        )
        assert len(m.entities) == 1
        assert len(m.assertions) == 1


class TestGraphQueryRequest:
    def test_defaults(self):
        q = GraphQueryRequest(tenant_id="col1", kb_id="kb1")
        assert q.max_depth == 2
        assert q.max_nodes == 50
        assert q.max_edges == 100
        assert q.as_of is None

    def test_with_security_scope(self):
        q = GraphQueryRequest(
            tenant_id="col1", kb_id="kb1",
            entity_ids=["ent_1"],
            security_scope=["kb1"],
        )
        assert q.security_scope == ["kb1"]
