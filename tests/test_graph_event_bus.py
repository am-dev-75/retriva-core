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

"""Unit tests for GraphEventBus."""

import pytest
from retriva.graph.contracts import GraphChangeEvent, GraphEventType
from retriva.graph.event_bus import GraphEventBus


@pytest.fixture(autouse=True)
def reset_event_bus():
    GraphEventBus._reset()
    yield
    GraphEventBus._reset()


class TestGraphEventBus:
    def test_subscribe_and_publish(self):
        bus = GraphEventBus()
        received = []

        def handler(event):
            received.append(event)

        bus.subscribe(GraphEventType.ENTITY_CREATED.value, handler)
        bus.publish(GraphChangeEvent(
            event_type=GraphEventType.ENTITY_CREATED,
            tenant_id="col1", kb_id="kb1",
            entity_ids=["ent_1"],
        ))

        assert len(received) == 1
        assert received[0].event_type == GraphEventType.ENTITY_CREATED

    def test_wildcard_subscription(self):
        bus = GraphEventBus()
        received = []

        def handler(event):
            received.append(event)

        bus.subscribe("*", handler)
        bus.publish(GraphChangeEvent(
            event_type=GraphEventType.ENTITY_CREATED,
            tenant_id="col1", kb_id="kb1",
        ))
        bus.publish(GraphChangeEvent(
            event_type=GraphEventType.ASSERTION_CREATED,
            tenant_id="col1", kb_id="kb1",
        ))

        assert len(received) == 2

    def test_handler_error_does_not_crash(self):
        bus = GraphEventBus()

        def bad_handler(event):
            raise RuntimeError("handler error")

        def good_handler(event):
            good_handler.called = True

        good_handler.called = False

        bus.subscribe(GraphEventType.ENTITY_CREATED.value, bad_handler)
        bus.subscribe(GraphEventType.ENTITY_CREATED.value, good_handler)

        # Should not raise
        bus.publish(GraphChangeEvent(
            event_type=GraphEventType.ENTITY_CREATED,
            tenant_id="col1", kb_id="kb1",
        ))

        assert good_handler.called is True

    def test_no_handlers(self):
        bus = GraphEventBus()
        # Should not raise
        bus.publish(GraphChangeEvent(
            event_type=GraphEventType.ENTITY_CREATED,
            tenant_id="col1", kb_id="kb1",
        ))
