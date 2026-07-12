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
Graph event bus — in-process pub/sub with optional Redis adapter.

Events are published after graph mutations.  Extensions subscribe to
graph-change events to maintain materialized projections or trigger
domain-specific workflows.

When ``settings.celery_broker_url`` is set, events are also published
to a Redis channel (``retriva:graph:events``) for cross-process
subscribers (e.g. extension projection builders running in workers).
"""

from __future__ import annotations

import json
import threading
from typing import Callable, Dict, List, Optional
from uuid import uuid4

from retriva.graph.contracts import GraphChangeEvent, GraphEventType
from retriva.logger import get_logger

logger = get_logger(__name__)

# Redis channel for cross-process event publishing
REDIS_GRAPH_CHANNEL = "retriva:graph:events"


class GraphEventBus:
    """In-process pub/sub with optional Redis adapter.

    Follows the same singleton pattern as :class:`CapabilityRegistry`.
    """

    _instance: Optional["GraphEventBus"] = None
    _init_lock = threading.Lock()

    def __new__(cls) -> "GraphEventBus":
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._handlers: Dict[str, List[Callable]] = {}
                    instance._lock = threading.Lock()
                    instance._redis_adapter: Optional[_RedisAdapter] = None
                    cls._instance = instance
        return cls._instance

    def subscribe(
        self,
        event_type: str,
        handler: Callable[[GraphChangeEvent], None],
    ) -> None:
        """Subscribe a handler for a specific event type.

        Use ``"*"`` as *event_type* to subscribe to all events.
        """
        with self._lock:
            if event_type not in self._handlers:
                self._handlers[event_type] = []
            self._handlers[event_type].append(handler)
        logger.debug(f"GraphEventBus: handler subscribed for '{event_type}'")

    def publish(self, event: GraphChangeEvent) -> None:
        """Publish an event to all matching handlers."""
        logger.debug(
            f"GraphEventBus: publishing {event.event_type} "
            f"(entities={event.entity_ids}, assertions={event.assertion_ids})"
        )

        # In-process handlers
        handlers: List[Callable] = []
        with self._lock:
            handlers = list(self._handlers.get(event.event_type.value, []))
            handlers.extend(self._handlers.get("*", []))

        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                logger.error(
                    f"GraphEventBus: handler error for '{event.event_type}': {e}",
                    exc_info=True,
                )

        # Redis adapter (cross-process)
        if self._redis_adapter:
            try:
                self._redis_adapter.publish(event)
            except Exception as e:
                logger.warning(f"GraphEventBus: Redis publish failed: {e}")

    def init_redis(self, redis_url: str) -> None:
        """Initialize the Redis adapter for cross-process event publishing."""
        if self._redis_adapter is None:
            self._redis_adapter = _RedisAdapter(redis_url)
            logger.info(f"GraphEventBus: Redis adapter initialized ({redis_url})")

    @classmethod
    def _reset(cls) -> None:
        """Reset the singleton — for testing only."""
        with cls._init_lock:
            if cls._instance is not None:
                cls._instance._handlers.clear()
                cls._instance._redis_adapter = None
                cls._instance = None


class _RedisAdapter:
    """Minimal Redis pub/sub adapter for cross-process events."""

    def __init__(self, redis_url: str):
        self._redis_url = redis_url
        self._client = None
        try:
            import redis
            self._client = redis.Redis.from_url(redis_url, decode_responses=True)
            self._client.ping()
        except ImportError:
            logger.warning(
                "GraphEventBus: redis package not installed — "
                "cross-process events disabled"
            )
        except Exception as e:
            logger.warning(f"GraphEventBus: Redis connection failed: {e}")
            self._client = None

    def publish(self, event: GraphChangeEvent) -> None:
        if self._client is None:
            return
        payload = json.dumps(event.model_dump())
        self._client.publish(REDIS_GRAPH_CHANNEL, payload)
