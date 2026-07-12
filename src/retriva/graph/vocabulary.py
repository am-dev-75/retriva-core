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
Vocabulary registry for GraphRAG extensions.

Extensions register namespaced domain vocabularies (entity types,
predicates, event types) so that the Core can validate and route
graph data without knowing domain-specific concepts.
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional, Set

from retriva.graph.contracts import VocabularyRegistration
from retriva.logger import get_logger

logger = get_logger(__name__)


class VocabularyRegistry:
    """Thread-safe singleton registry for extension vocabularies.

    Follows the same singleton pattern as :class:`CapabilityRegistry`.
    """

    _instance: Optional["VocabularyRegistry"] = None
    _init_lock = threading.Lock()

    def __new__(cls) -> "VocabularyRegistry":
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._registrations: Dict[str, VocabularyRegistration] = {}
                    instance._entity_types: Set[str] = set()
                    instance._predicates: Set[str] = set()
                    instance._event_types: Set[str] = set()
                    instance._lock = threading.Lock()
                    cls._instance = instance
        return cls._instance

    def register(self, registration: VocabularyRegistration) -> None:
        with self._lock:
            existing = self._registrations.get(registration.namespace)
            if existing:
                # Merge: add new types to existing registration
                existing.entity_types = list(
                    set(existing.entity_types) | set(registration.entity_types)
                )
                existing.predicates = list(
                    set(existing.predicates) | set(registration.predicates)
                )
                existing.event_types = list(
                    set(existing.event_types) | set(registration.event_types)
                )
            else:
                self._registrations[registration.namespace] = registration

            self._entity_types.update(registration.entity_types)
            self._predicates.update(registration.predicates)
            self._event_types.update(registration.event_types)

        logger.debug(
            f"Vocabulary registered: namespace='{registration.namespace}', "
            f"entity_types={registration.entity_types}, "
            f"predicates={registration.predicates}"
        )

    def get_namespaced_types(self, namespace: str) -> List[str]:
        with self._lock:
            reg = self._registrations.get(namespace)
            return reg.entity_types if reg else []

    def get_namespaced_predicates(self, namespace: str) -> List[str]:
        with self._lock:
            reg = self._registrations.get(namespace)
            return reg.predicates if reg else []

    def is_registered_type(self, type_name: str) -> bool:
        with self._lock:
            return type_name in self._entity_types

    def is_registered_predicate(self, predicate: str) -> bool:
        with self._lock:
            return predicate in self._predicates

    def is_registered_event_type(self, event_type: str) -> bool:
        with self._lock:
            return event_type in self._event_types

    def list_namespaces(self) -> List[str]:
        with self._lock:
            return list(self._registrations.keys())

    def list_all(self) -> List[VocabularyRegistration]:
        with self._lock:
            return list(self._registrations.values())

    @classmethod
    def _reset(cls) -> None:
        """Reset the singleton — for testing only."""
        with cls._init_lock:
            if cls._instance is not None:
                cls._instance._registrations.clear()
                cls._instance._entity_types.clear()
                cls._instance._predicates.clear()
                cls._instance._event_types.clear()
                cls._instance = None
