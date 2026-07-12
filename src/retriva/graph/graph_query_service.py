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
Graph query service — storage-neutral graph query operations.

Registered as ``graph_query_service`` capability at priority 100.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from retriva.config import settings
from retriva.graph.contracts import (
    Entity,
    GraphPath,
    GraphQueryRequest,
    GraphRetrievalResult,
)
from retriva.logger import get_logger

logger = get_logger(__name__)


class GraphQueryService:
    """Storage-neutral graph query service.

    Implements the :class:`retriva.graph.protocols.GraphQueryServiceProtocol`.
    """

    def __init__(self):
        self._store = None

    @property
    def store(self):
        if self._store is None:
            from retriva.graph.stores.sqlite_graph_store import SQLiteGraphStore
            self._store = SQLiteGraphStore()
        return self._store

    def search_entities(
        self, query: str, tenant_id: str, kb_id: str, limit: int = 20
    ) -> List[Entity]:
        return self.store.search_entities(query, tenant_id, kb_id, limit)

    def get_neighborhood(self, request: GraphQueryRequest) -> GraphRetrievalResult:
        if not request.entity_ids:
            if request.entity_name:
                entities = self.store.get_entities_by_alias(
                    request.entity_name, request.tenant_id, request.kb_id
                )
                if entities:
                    request.entity_ids = [entities[0].entity_id]
                else:
                    return GraphRetrievalResult(
                        warnings=[f"no entity found for name '{request.entity_name}'"]
                    )
            else:
                return GraphRetrievalResult(
                    warnings=["no entity_ids or entity_name provided"]
                )

        result = GraphRetrievalResult()
        for eid in request.entity_ids:
            partial = self.store.get_neighborhood(
                entity_id=eid,
                max_depth=request.max_depth,
                max_nodes=request.max_nodes,
                max_edges=request.max_edges,
                security_scope=request.security_scope,
                as_of=request.as_of,
            )
            # Merge
            result.entities.extend(partial.entities)
            result.assertions.extend(partial.assertions)
            result.relationships.extend(partial.relationships)
            result.warnings.extend(partial.warnings)
            if partial.truncated:
                result.truncated = True

        # Deduplicate
        seen_e = {e.entity_id for e in result.entities}
        seen_a = {a.assertion_id for a in result.assertions}
        # Keep first occurrence
        result.entities = list({e.entity_id: e for e in result.entities}.values())
        result.assertions = list({a.assertion_id: a for a in result.assertions}.values())

        return result

    def find_paths(
        self,
        source_id: str,
        target_id: str,
        tenant_id: str,
        kb_id: str,
        max_depth: int,
        security_scope: List[str],
    ) -> List[GraphPath]:
        return self.store.find_paths(
            source_id, target_id, max_depth, security_scope
        )

    def get_evidence(
        self, entity_id: str, tenant_id: str, kb_id: str
    ) -> List[Dict[str, Any]]:
        """Retrieve evidence references for an entity."""
        assertions = self.store.get_assertions(entity_id)
        evidence: List[Dict[str, Any]] = []
        for ast in assertions:
            for doc_id in ast.source_document_ids:
                for chunk_id in ast.source_chunk_ids:
                    evidence.append({
                        "document_id": doc_id,
                        "chunk_id": chunk_id,
                        "assertion_id": ast.assertion_id,
                        "predicate": ast.predicate,
                        "confidence": ast.extraction_confidence,
                        "status": ast.status.value,
                    })
        return evidence

    def get_entities_for_chunks(
        self, chunk_ids: List[str], tenant_id: str, kb_id: str
    ) -> List[Entity]:
        """Map chunk IDs to graph entities via provenance.

        Used by the retrieval orchestrator to connect vector results to
        graph entities.
        """
        if not chunk_ids:
            return []
        entities: List[Entity] = []
        with self.store.connect() as conn:
            placeholders = ",".join("?" * len(chunk_ids))
            rows = conn.execute(
                f"""
                SELECT DISTINCT e.* FROM graph_entities e
                INNER JOIN graph_provenance p ON p.entity_id = e.entity_id
                WHERE p.chunk_id IN ({placeholders})
                    AND p.tenant_id = ?
                    AND p.kb_id = ?
                """,
                chunk_ids + [tenant_id, kb_id],
            ).fetchall()
        from retriva.graph.stores.sqlite_graph_store import SQLiteGraphStore
        for row in rows:
            entities.append(SQLiteGraphStore._entity_from_row(row))
        return entities
