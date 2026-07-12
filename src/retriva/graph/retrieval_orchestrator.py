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
Graph retrieval orchestrator — combines vector and graph retrieval.

Modes:
- ``VECTOR``: backward-compatible default (delegates to existing retriever).
- ``GRAPH_LOCAL``: entity-centric graph expansion.
- ``HYBRID``: vector + graph, with reranking and provenance-aware context.

``GRAPH_GLOBAL`` and ``AUTO`` are designed but not implemented in Phase 1.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from retriva.config import settings
from retriva.graph.contracts import (
    Entity,
    GraphQueryRequest,
    GraphRetrievalResult,
    RetrievalMode,
)
from retriva.logger import get_logger

logger = get_logger(__name__)


class GraphRetrievalOrchestrator:
    """Orchestrates vector and graph retrieval.

    Registered as ``retrieval_orchestrator`` capability at priority 100.
    """

    def __init__(self):
        self._graph_query = None

    @property
    def graph_query(self):
        if self._graph_query is None:
            from retriva.graph.graph_query_service import GraphQueryService
            self._graph_query = GraphQueryService()
        return self._graph_query

    def retrieve(
        self,
        query: str,
        top_k: int = 20,
        metadata_filters: Optional[List[Dict[str, Any]]] = None,
        metadata_filter_mode: str = "soft",
        kb_ids: Optional[List[str]] = None,
        retrieval_mode: Optional[RetrievalMode] = None,
        vector_chunks: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        """Run retrieval in the specified mode.

        When *vector_chunks* are provided (already retrieved by the
        existing pipeline), they are reused instead of re-running vector
        search.

        Returns a dict with ``chunks`` (backward-compatible) and optional
        graph fields (``entities``, ``assertions``, ``paths``, etc.).
        """
        from retriva.indexing.qdrant_store import get_collection_name

        mode = retrieval_mode or RetrievalMode(
            settings.graph_default_retrieval_mode
        )

        if mode == RetrievalMode.VECTOR:
            return self._vector_only(query, top_k, metadata_filters,
                                     metadata_filter_mode, kb_ids, vector_chunks)

        if not settings.graph_enabled:
            logger.debug(
                "GraphRetrievalOrchestrator: graph_enabled=False, "
                "falling back to VECTOR mode"
            )
            return self._vector_only(query, top_k, metadata_filters,
                                     metadata_filter_mode, kb_ids, vector_chunks)

        tenant_id = get_collection_name()
        kb_id = (kb_ids[0] if kb_ids else "default")

        if mode == RetrievalMode.GRAPH_LOCAL:
            return self._graph_local(
                query, top_k, tenant_id, kb_id, vector_chunks
            )
        elif mode == RetrievalMode.HYBRID:
            return self._hybrid(
                query, top_k, metadata_filters, metadata_filter_mode,
                kb_ids, tenant_id, kb_id, vector_chunks
            )
        elif mode == RetrievalMode.GRAPH_GLOBAL:
            logger.warning(
                "GraphRetrievalOrchestrator: GRAPH_GLOBAL not implemented "
                "in Phase 1 — falling back to HYBRID"
            )
            return self._hybrid(
                query, top_k, metadata_filters, metadata_filter_mode,
                kb_ids, tenant_id, kb_id, vector_chunks
            )
        elif mode == RetrievalMode.AUTO:
            logger.warning(
                "GraphRetrievalOrchestrator: AUTO not implemented "
                "in Phase 1 — falling back to HYBRID"
            )
            return self._hybrid(
                query, top_k, metadata_filters, metadata_filter_mode,
                kb_ids, tenant_id, kb_id, vector_chunks
            )
        else:
            return self._vector_only(query, top_k, metadata_filters,
                                     metadata_filter_mode, kb_ids, vector_chunks)

    # ------------------------------------------------------------------ VECTOR

    def _vector_only(
        self,
        query: str,
        top_k: int,
        metadata_filters: Optional[List[Dict[str, Any]]],
        metadata_filter_mode: str,
        kb_ids: Optional[List[str]],
        vector_chunks: Optional[List[Dict]],
    ) -> Dict[str, Any]:
        if vector_chunks is not None:
            return {"chunks": vector_chunks, "retrieval_mode": RetrievalMode.VECTOR.value}
        from retriva.registry import CapabilityRegistry
        retriever = CapabilityRegistry().get_instance("retriever")
        chunks = retriever.retrieve(
            query=query, top_k=top_k,
            metadata_filters=metadata_filters,
            metadata_filter_mode=metadata_filter_mode,
            kb_ids=kb_ids,
        )
        return {"chunks": chunks, "retrieval_mode": RetrievalMode.VECTOR.value}

    # ------------------------------------------------------------------ GRAPH_LOCAL

    def _graph_local(
        self,
        query: str,
        top_k: int,
        tenant_id: str,
        kb_id: str,
        vector_chunks: Optional[List[Dict]],
    ) -> Dict[str, Any]:
        """Entity-centric graph expansion.

        1. Search entities by query text.
        2. Expand bounded neighborhood.
        3. Retrieve supporting chunks for expanded entities.
        """
        # 1. Search entities
        entities = self.graph_query.search_entities(
            query, tenant_id, kb_id, limit=top_k
        )
        if not entities:
            return self._vector_only(query, top_k, None, "soft", [kb_id], vector_chunks)

        # 2. Expand neighborhood
        security_scope = [kb_id]
        result = GraphRetrievalResult()
        for entity in entities[:5]:  # limit seed entities
            req = GraphQueryRequest(
                tenant_id=tenant_id,
                kb_id=kb_id,
                entity_ids=[entity.entity_id],
                max_depth=settings.graph_traversal_max_depth,
                max_nodes=settings.graph_traversal_max_nodes,
                max_edges=settings.graph_traversal_max_edges,
                security_scope=security_scope,
            )
            partial = self.graph_query.get_neighborhood(req)
            result.entities.extend(partial.entities)
            result.assertions.extend(partial.assertions)
            result.relationships.extend(partial.relationships)

        # Deduplicate
        result.entities = list(
            {e.entity_id: e for e in result.entities}.values()
        )
        result.assertions = list(
            {a.assertion_id: a for a in result.assertions}.values()
        )

        # 3. Retrieve supporting chunks
        chunk_ids: set[str] = set()
        for ast in result.assertions:
            chunk_ids.update(ast.source_chunk_ids)

        supporting_chunks = self._fetch_chunks_by_ids(list(chunk_ids), tenant_id)

        return {
            "chunks": supporting_chunks[:top_k],
            "entities": [e.model_dump() for e in result.entities],
            "assertions": [a.model_dump() for a in result.assertions],
            "relationships": [r.model_dump() for r in result.relationships],
            "retrieval_mode": RetrievalMode.GRAPH_LOCAL.value,
            "warnings": result.warnings,
            "truncated": result.truncated,
        }

    # ------------------------------------------------------------------ HYBRID

    def _hybrid(
        self,
        query: str,
        top_k: int,
        metadata_filters: Optional[List[Dict[str, Any]]],
        metadata_filter_mode: str,
        kb_ids: Optional[List[str]],
        tenant_id: str,
        kb_id: str,
        vector_chunks: Optional[List[Dict]],
    ) -> Dict[str, Any]:
        """Hybrid vector + graph retrieval.

        1. Run existing vector + rerank + hybrid-select pipeline.
        2. Map retrieved chunks to graph entities via provenance.
        3. Expand through bounded, authorized graph neighborhood.
        4. Retrieve supporting chunks for expanded entities/assertions.
        5. Deduplicate and normalize candidates.
        6. Apply reranking to merged candidates.
        7. Assemble provenance-aware context.
        """
        # 1. Vector retrieval (reuse if provided)
        if vector_chunks is None:
            from retriva.registry import CapabilityRegistry
            retriever = CapabilityRegistry().get_instance("retriever")
            vector_chunks = retriever.retrieve(
                query=query, top_k=top_k,
                metadata_filters=metadata_filters,
                metadata_filter_mode=metadata_filter_mode,
                kb_ids=kb_ids,
            )

        if not vector_chunks:
            return {"chunks": [], "retrieval_mode": RetrievalMode.HYBRID.value}

        # 2. Map chunks to graph entities
        chunk_ids = [c.get("chunk_id") for c in vector_chunks if c.get("chunk_id")]
        entities = self.graph_query.get_entities_for_chunks(
            chunk_ids, tenant_id, kb_id
        )

        if not entities:
            # No graph data for these chunks — return vector results
            return {
                "chunks": vector_chunks,
                "retrieval_mode": RetrievalMode.HYBRID.value,
                "warnings": ["no graph entities found for vector results"],
            }

        # 3. Expand neighborhood
        security_scope = [kb_id]
        graph_result = GraphRetrievalResult()
        for entity in entities[:10]:  # limit seed entities
            req = GraphQueryRequest(
                tenant_id=tenant_id,
                kb_id=kb_id,
                entity_ids=[entity.entity_id],
                max_depth=settings.graph_traversal_max_depth,
                max_nodes=settings.graph_traversal_max_nodes,
                max_edges=settings.graph_traversal_max_edges,
                security_scope=security_scope,
            )
            partial = self.graph_query.get_neighborhood(req)
            graph_result.entities.extend(partial.entities)
            graph_result.assertions.extend(partial.assertions)
            graph_result.relationships.extend(partial.relationships)

        # Deduplicate
        graph_result.entities = list(
            {e.entity_id: e for e in graph_result.entities}.values()
        )
        graph_result.assertions = list(
            {a.assertion_id: a for a in graph_result.assertions}.values()
        )

        # 4. Retrieve supporting chunks for expanded entities
        expanded_chunk_ids: set[str] = set()
        for ast in graph_result.assertions:
            expanded_chunk_ids.update(ast.source_chunk_ids)
        # Remove chunk IDs already in vector results
        new_chunk_ids = expanded_chunk_ids - set(chunk_ids)
        supporting_chunks = self._fetch_chunks_by_ids(list(new_chunk_ids), tenant_id)

        # 5. Deduplicate and merge
        all_chunks = list(vector_chunks)
        existing_ids = {c.get("chunk_id") for c in all_chunks}
        for chunk in supporting_chunks:
            cid = chunk.get("chunk_id")
            if cid and cid not in existing_ids:
                all_chunks.append(chunk)
                existing_ids.add(cid)

        # 6. Apply per-entity contribution limit
        max_per_entity = settings.graph_max_chunks_per_entity
        # (simplified: just cap total additional chunks)
        if len(supporting_chunks) > max_per_entity * len(entities):
            all_chunks = list(vector_chunks) + supporting_chunks[
                :max_per_entity * len(entities)
            ]

        # 7. Sort by score and limit
        all_chunks.sort(key=lambda x: x.get("_score", 0.0), reverse=True)
        all_chunks = all_chunks[:top_k]

        return {
            "chunks": all_chunks,
            "entities": [e.model_dump() for e in graph_result.entities],
            "assertions": [a.model_dump() for a in graph_result.assertions],
            "relationships": [r.model_dump() for r in graph_result.relationships],
            "retrieval_mode": RetrievalMode.HYBRID.value,
            "warnings": graph_result.warnings,
            "truncated": graph_result.truncated,
        }

    # ------------------------------------------------------------------ Helpers

    def _fetch_chunks_by_ids(
        self, chunk_ids: List[str], collection_name: str
    ) -> List[Dict[str, Any]]:
        """Fetch chunks from Qdrant by ID."""
        if not chunk_ids:
            return []
        from qdrant_client.models import PointIdsList
        from retriva.indexing.qdrant_store import get_client, get_collection_name
        try:
            client = get_client()
            col = get_collection_name()
            points = client.retrieve(
                collection_name=col,
                ids=chunk_ids,
                with_payload=True,
                with_vectors=False,
            )
            chunks = []
            for p in points:
                payload = p.payload or {}
                chunks.append({
                    "chunk_id": str(p.id),
                    "text": payload.get("text", ""),
                    "doc_id": payload.get("doc_id"),
                    "source_path": payload.get("source_path"),
                    "page_title": payload.get("page_title"),
                    "kb_id": payload.get("kb_id"),
                    "_score": 0.0,  # graph-retrieved chunks have no vector score
                })
            return chunks
        except Exception as e:
            logger.warning(f"GraphRetrievalOrchestrator: failed to fetch chunks: {e}")
            return []
