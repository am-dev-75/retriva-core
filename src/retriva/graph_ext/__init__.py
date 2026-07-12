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
Extension SDK/SPI for GraphRAG.

Extensions register through the existing ``RETRIVA_EXTENSIONS`` mechanism
(``module.register(registry)`` hook).  Graph-aware extensions additionally
call :func:`register_graph_extension` to contribute vocabularies,
extraction profiles, validators, normalizers, entity-resolution hints,
retrieval hints, graph-change event handlers, and domain projection
handlers.

The :class:`GraphExtensionRegistry` is a thread-safe singleton separate
from :class:`CapabilityRegistry` but loaded via the same discovery
mechanism.  Missing methods on an extension are treated as no-ops, so
an extension that only needs vocabulary registration can implement a
minimal subset.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from retriva.graph.contracts import (
    Entity,
    ExtractionProfile,
    GraphChangeEvent,
    VocabularyRegistration,
)
from retriva.graph.vocabulary import VocabularyRegistry
from retriva.logger import get_logger

logger = get_logger(__name__)


@runtime_checkable
class GraphExtension(Protocol):
    """SPI for graph-aware extensions.

    Extensions may implement any subset of these methods.  The
    :class:`GraphExtensionRegistry` treats missing methods as no-ops
    or returns empty defaults.
    """

    def register_vocabulary(self, reg: VocabularyRegistry) -> None:
        """Register namespaced entity types, predicates, and event types."""
        ...

    def get_extraction_profiles(self) -> List[ExtractionProfile]:
        """Return extraction profiles this extension provides."""
        ...

    def select_profile(
        self,
        source_type: str,
        user_metadata: Optional[Dict[str, str]] = None,
    ) -> Optional[str]:
        """Select an extraction profile for a given source type.

        Returns the profile_id or None to defer to the default.
        """
        ...

    def validate_entity(self, entity: Entity) -> List[str]:
        """Validate an entity.  Returns a list of error messages (empty = valid)."""
        ...

    def normalize_entity(self, entity: Entity) -> Entity:
        """Normalize an entity (e.g. canonical name formatting)."""
        ...

    def provide_resolution_hints(self, entity: Entity) -> Dict[str, Any]:
        """Provide identity hints for entity resolution."""
        ...

    def provide_retrieval_hints(
        self, query: str, entities: List[Entity]
    ) -> Dict[str, Any]:
        """Provide retrieval hints based on the query and found entities."""
        ...

    def on_graph_change(self, event: GraphChangeEvent) -> None:
        """Handle a graph-change event."""
        ...

    def build_projection(self, event: GraphChangeEvent) -> None:
        """Maintain an extension-owned materialized projection."""
        ...


class GraphExtensionRegistry:
    """Thread-safe singleton registry for graph extensions.

    Loaded via the same ``RETRIVA_EXTENSIONS`` mechanism as
    :class:`CapabilityRegistry`.  Extensions call
    :func:`register_graph_extension` in their ``register()`` hook.
    """

    _instance: Optional["GraphExtensionRegistry"] = None
    _init_lock = threading.Lock()

    def __new__(cls) -> "GraphExtensionRegistry":
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._extensions: List[Any] = []
                    instance._lock = threading.Lock()
                    cls._instance = instance
        return cls._instance

    def register(self, extension: Any) -> None:
        """Register a graph extension.

        The *extension* should implement (a subset of) the
        :class:`GraphExtension` protocol.  After registration, its
        ``register_vocabulary`` method (if present) is called immediately.
        """
        with self._lock:
            self._extensions.append(extension)

        # Register vocabulary immediately
        if hasattr(extension, "register_vocabulary"):
            try:
                extension.register_vocabulary(VocabularyRegistry())
            except Exception as e:
                logger.error(
                    f"GraphExtensionRegistry: register_vocabulary failed for "
                    f"{type(extension).__name__}: {e}",
                    exc_info=True,
                )

        logger.info(
            f"GraphExtensionRegistry: registered {type(extension).__name__}"
        )

    @property
    def extensions(self) -> List[Any]:
        with self._lock:
            return list(self._extensions)

    def get_extraction_profiles(self) -> List[ExtractionProfile]:
        profiles: List[ExtractionProfile] = []
        for ext in self.extensions:
            if hasattr(ext, "get_extraction_profiles"):
                try:
                    profiles.extend(ext.get_extraction_profiles())
                except Exception as e:
                    logger.warning(
                        f"GraphExtensionRegistry: get_extraction_profiles "
                        f"failed for {type(ext).__name__}: {e}"
                    )
        return profiles

    def select_profile(
        self,
        source_type: str,
        user_metadata: Optional[Dict[str, str]] = None,
    ) -> Optional[str]:
        for ext in self.extensions:
            if hasattr(ext, "select_profile"):
                try:
                    profile_id = ext.select_profile(source_type, user_metadata)
                    if profile_id:
                        return profile_id
                except Exception as e:
                    logger.warning(
                        f"GraphExtensionRegistry: select_profile failed "
                        f"for {type(ext).__name__}: {e}"
                    )
        return None

    def validate_entity(self, entity: Entity) -> List[str]:
        errors: List[str] = []
        for ext in self.extensions:
            if hasattr(ext, "validate_entity"):
                try:
                    errors.extend(ext.validate_entity(entity))
                except Exception as e:
                    logger.warning(
                        f"GraphExtensionRegistry: validate_entity failed "
                        f"for {type(ext).__name__}: {e}"
                    )
        return errors

    def normalize_entity(self, entity: Entity) -> Entity:
        for ext in self.extensions:
            if hasattr(ext, "normalize_entity"):
                try:
                    entity = ext.normalize_entity(entity)
                except Exception as e:
                    logger.warning(
                        f"GraphExtensionRegistry: normalize_entity failed "
                        f"for {type(ext).__name__}: {e}"
                    )
        return entity

    def provide_resolution_hints(self, entity: Entity) -> Dict[str, Any]:
        hints: Dict[str, Any] = {}
        for ext in self.extensions:
            if hasattr(ext, "provide_resolution_hints"):
                try:
                    hints.update(ext.provide_resolution_hints(entity))
                except Exception as e:
                    logger.warning(
                        f"GraphExtensionRegistry: provide_resolution_hints "
                        f"failed for {type(ext).__name__}: {e}"
                    )
        return hints

    def provide_retrieval_hints(
        self, query: str, entities: List[Entity]
    ) -> Dict[str, Any]:
        hints: Dict[str, Any] = {}
        for ext in self.extensions:
            if hasattr(ext, "provide_retrieval_hints"):
                try:
                    hints.update(ext.provide_retrieval_hints(query, entities))
                except Exception as e:
                    logger.warning(
                        f"GraphExtensionRegistry: provide_retrieval_hints "
                        f"failed for {type(ext).__name__}: {e}"
                    )
        return hints

    def notify_graph_change(self, event: GraphChangeEvent) -> None:
        for ext in self.extensions:
            for method_name in ("on_graph_change", "build_projection"):
                if hasattr(ext, method_name):
                    try:
                        getattr(ext, method_name)(event)
                    except Exception as e:
                        logger.warning(
                            f"GraphExtensionRegistry: {method_name} failed "
                            f"for {type(ext).__name__}: {e}"
                        )

    @classmethod
    def _reset(cls) -> None:
        """Reset the singleton — for testing only."""
        with cls._init_lock:
            if cls._instance is not None:
                cls._instance._extensions.clear()
                cls._instance = None


# ---------------------------------------------------------------------------
# Public registration function (called by extensions in their register() hook)
# ---------------------------------------------------------------------------

def register_graph_extension(extension: Any) -> None:
    """Register a graph extension with the :class:`GraphExtensionRegistry`.

    Extensions call this in their ``register(registry)`` hook:

    .. code-block:: python

        def register(registry):
            from retriva.graph_ext.spi import register_graph_extension
            register_graph_extension(MyGraphExtension())
    """
    GraphExtensionRegistry().register(extension)
