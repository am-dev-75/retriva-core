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
Protocol (interface) definitions for the GraphRAG platform service.

These protocols define the minimal contract that any implementation —
whether the built-in OSS default or a proprietary extension — must
satisfy.  No graph-database-specific types are exposed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from retriva.graph.contracts import (
    Assertion,
    AssertionStatus,
    Entity,
    GraphMutationRequest,
    GraphPath,
    GraphQueryRequest,
    GraphRetrievalResult,
    GraphEvent,
    Relationship,
)


@runtime_checkable
class GraphStore(Protocol):
    """Storage-neutral graph store port.

    Implementations must enforce tenant and knowledge-space isolation:
    no query, traversal, or retrieval may cross a
    ``(tenant_id, kb_id)`` boundary.
    """

    # -- Mutations ---------------------------------------------------------

    def apply_mutation(self, mutation: GraphMutationRequest) -> None:
        """Apply a batch mutation (entities, assertions, relationships, events, evidence)."""
        ...

    def upsert_entities(self, entities: List[Entity]) -> None: ...

    def upsert_assertions(self, assertions: List[Assertion]) -> None: ...

    def upsert_relationships(self, relationships: List[Relationship]) -> None: ...

    def store_provenance(
        self,
        document_id: str,
        chunk_ids: List[str],
        entity_ids: List[str],
        assertion_ids: List[str],
        tenant_id: str,
        kb_id: str,
    ) -> None:
        """Persist source-to-graph provenance links."""
        ...

    # -- Retrieval ---------------------------------------------------------

    def get_entity(self, entity_id: str) -> Optional[Entity]: ...

    def get_entities_by_alias(
        self, name: str, tenant_id: str, kb_id: str
    ) -> List[Entity]:
        """Return entities matching *name* (exact or alias) within the scope."""
        ...

    def get_entities_by_external_id(
        self, system: str, external_id: str, tenant_id: str
    ) -> Optional[Entity]: ...

    def get_assertions(
        self,
        entity_id: str,
        status: Optional[AssertionStatus] = None,
        as_of: Optional[str] = None,
    ) -> List[Assertion]: ...

    def get_neighborhood(
        self,
        entity_id: str,
        max_depth: int,
        max_nodes: int,
        max_edges: int,
        security_scope: List[str],
        as_of: Optional[str] = None,
    ) -> GraphRetrievalResult:
        """Expand a bounded neighborhood around *entity_id*.

        Results are filtered by *security_scope* before return.
        """
        ...

    def find_paths(
        self,
        source_id: str,
        target_id: str,
        max_depth: int,
        security_scope: List[str],
    ) -> List[GraphPath]: ...

    def search_entities(
        self,
        query: str,
        tenant_id: str,
        kb_id: str,
        limit: int = 20,
    ) -> List[Entity]: ...

    def get_impacted_entities(self, entity_id: str) -> List[str]:
        """Return entity IDs that are directly connected to *entity_id*."""
        ...

    # -- Deletion & invalidation ------------------------------------------

    def delete_by_source(self, document_id: str) -> int:
        """Delete or invalidate all graph data derived from *document_id*.

        Returns the number of affected objects.  Idempotent.
        """
        ...

    def invalidate_by_source(self, document_id: str) -> int:
        """Invalidate (but do not delete) assertions from *document_id*."""
        ...


@runtime_checkable
class EntityExtractor(Protocol):
    """Extract candidate entities and assertions from chunk text."""

    def extract(
        self,
        chunks: List[Dict[str, Any]],
        profile_id: str,
        tenant_id: str,
        kb_id: str,
        source_document_id: str,
    ) -> GraphMutationRequest:
        """
        Args:
            chunks: List of chunk dicts (as stored in Qdrant payloads).
            profile_id: Extraction profile to use.
            tenant_id: Collection name.
            kb_id: Knowledge-space ID.
            source_document_id: The doc_id of the source document.

        Returns:
            A :class:`GraphMutationRequest` with candidate entities and
            assertions.  Entity IDs are temporary; the
            :class:`EntityResolutionService` will assign canonical IDs.
        """
        ...


@runtime_checkable
class GraphQueryServiceProtocol(Protocol):
    """Storage-neutral graph query operations."""

    def search_entities(
        self, query: str, tenant_id: str, kb_id: str, limit: int = 20
    ) -> List[Entity]: ...

    def get_neighborhood(
        self, request: GraphQueryRequest
    ) -> GraphRetrievalResult: ...

    def find_paths(
        self,
        source_id: str,
        target_id: str,
        tenant_id: str,
        kb_id: str,
        max_depth: int,
        security_scope: List[str],
    ) -> List[GraphPath]: ...

    def get_evidence(
        self, entity_id: str, tenant_id: str, kb_id: str
    ) -> List[Dict[str, Any]]: ...
