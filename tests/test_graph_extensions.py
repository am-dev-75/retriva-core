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

"""Unit tests for VocabularyRegistry and GraphExtensionRegistry."""

import pytest
from retriva.graph.contracts import (
    Entity,
    GraphChangeEvent,
    GraphEventType,
    VocabularyRegistration,
)
from retriva.graph.vocabulary import VocabularyRegistry
from retriva.graph_ext import GraphExtensionRegistry, register_graph_extension


@pytest.fixture(autouse=True)
def reset_registries():
    VocabularyRegistry._reset()
    GraphExtensionRegistry._reset()
    yield
    VocabularyRegistry._reset()
    GraphExtensionRegistry._reset()


class TestVocabularyRegistry:
    def test_register_and_lookup(self):
        reg = VocabularyRegistry()
        v = VocabularyRegistration(
            namespace="crm",
            entity_types=["crm:Opportunity"],
            predicates=["crm:blockedBy"],
        )
        reg.register(v)
        assert reg.is_registered_type("crm:Opportunity")
        assert reg.is_registered_predicate("crm:blockedBy")
        assert "crm" in reg.list_namespaces()

    def test_merge_registration(self):
        reg = VocabularyRegistry()
        reg.register(VocabularyRegistration(
            namespace="crm",
            entity_types=["crm:Opportunity"],
            predicates=["crm:blockedBy"],
        ))
        reg.register(VocabularyRegistration(
            namespace="crm",
            entity_types=["crm:Contact"],
            predicates=["crm:ownedBy"],
        ))
        types = reg.get_namespaced_types("crm")
        assert "crm:Opportunity" in types
        assert "crm:Contact" in types
        assert reg.is_registered_predicate("crm:ownedBy")

    def test_neutral_example_namespace(self):
        reg = VocabularyRegistry()
        reg.register(VocabularyRegistration(
            namespace="example",
            entity_types=["example:ProjectRisk"],
            predicates=["example:dependsOn"],
        ))
        assert reg.is_registered_type("example:ProjectRisk")
        assert reg.is_registered_predicate("example:dependsOn")

    def test_not_registered(self):
        reg = VocabularyRegistry()
        assert not reg.is_registered_type("nonexistent:Type")
        assert not reg.is_registered_predicate("nonexistent:predicate")


class TestGraphExtensionRegistry:
    def test_register_extension(self):
        class MyExtension:
            def register_vocabulary(self, reg):
                reg.register(VocabularyRegistration(
                    namespace="test",
                    entity_types=["test:Entity"],
                ))

        ext = MyExtension()
        register_graph_extension(ext)

        graph_reg = GraphExtensionRegistry()
        assert len(graph_reg.extensions) == 1
        assert VocabularyRegistry().is_registered_type("test:Entity")

    def test_validate_entity(self):
        class MyExtension:
            def register_vocabulary(self, reg):
                pass

            def validate_entity(self, entity):
                if not entity.name:
                    return ["name is required"]
                return []

        register_graph_extension(MyExtension())
        graph_reg = GraphExtensionRegistry()

        errors = graph_reg.validate_entity(
            Entity(tenant_id="col1", kb_id="kb1", name="Test")
        )
        assert errors == []

    def test_missing_methods_are_noops(self):
        class MinimalExtension:
            def register_vocabulary(self, reg):
                reg.register(VocabularyRegistration(
                    namespace="minimal",
                    entity_types=["minimal:Type"],
                ))

        register_graph_extension(MinimalExtension())
        graph_reg = GraphExtensionRegistry()

        # These should not raise
        graph_reg.validate_entity(
            Entity(tenant_id="col1", kb_id="kb1", name="Test")
        )
        graph_reg.get_extraction_profiles()
        graph_reg.select_profile("text/plain")
        graph_reg.notify_graph_change(GraphChangeEvent(
            event_type=GraphEventType.ENTITY_CREATED,
            tenant_id="col1", kb_id="kb1",
        ))

    def test_graph_change_notification(self):
        received_events = []

        class EventHandlerExtension:
            def register_vocabulary(self, reg):
                pass

            def on_graph_change(self, event):
                received_events.append(event)

        register_graph_extension(EventHandlerExtension())
        graph_reg = GraphExtensionRegistry()

        event = GraphChangeEvent(
            event_type=GraphEventType.ENTITY_CREATED,
            tenant_id="col1", kb_id="kb1",
            entity_ids=["ent_1"],
        )
        graph_reg.notify_graph_change(event)

        assert len(received_events) == 1
        assert received_events[0].event_type == GraphEventType.ENTITY_CREATED
