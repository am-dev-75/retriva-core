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
Graph indexer — post-indexing stage for the v2 ingestion pipeline.

Integrates into ``process_document_v2`` as a ``GRAPH_INDEXING`` stage
after the existing ``INDEXING`` stage.  When ``settings.graph_enabled``
is ``False`` (the default), the stage is skipped entirely.

Flow:
1. Select extraction profile (Core default or extension-provided).
2. Extract candidate entities and assertions from chunk text.
3. Validate and normalize via registered extensions.
4. Resolve candidates to canonical entities (EntityResolutionService).
5. Build and apply GraphMutationRequest.
6. Persist provenance.
7. Publish graph-change events.

Failure isolation: errors are logged but do NOT fail the ingestion job
or affect the vector index.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from retriva.config import settings
from retriva.graph.contracts import (
    Assertion,
    Entity,
    GraphChangeEvent,
    GraphEventType,
    GraphMutationRequest,
    Relationship,
)
from retriva.graph.entity_resolution import EntityResolutionService
from retriva.graph.event_bus import GraphEventBus
from retriva.graph_ext import GraphExtensionRegistry
from retriva.logger import get_logger

logger = get_logger(__name__)


class GraphIndexer:
    """Post-indexing graph extraction and mutation.

    Registered as ``graph_indexer`` capability at priority 100.
    """

    def __init__(self):
        self._store = None
        self._resolver = None
        self._extractor = None

    @property
    def store(self):
        if self._store is None:
            from retriva.graph.stores.sqlite_graph_store import SQLiteGraphStore
            self._store = SQLiteGraphStore()
        return self._store

    @property
    def resolver(self):
        if self._resolver is None:
            self._resolver = EntityResolutionService(store=self.store)
        return self._resolver

    @property
    def extractor(self):
        if self._extractor is None:
            from retriva.registry import CapabilityRegistry
            try:
                self._extractor = CapabilityRegistry().get_instance("entity_extractor")
            except KeyError:
                from retriva.graph.extraction import DefaultEntityExtractor
                self._extractor = DefaultEntityExtractor()
        return self._extractor

    def index_document(
        self,
        doc_id: str,
        kb_id: str,
        chunks: List[Dict[str, Any]],
        source_type: Optional[str] = None,
        user_metadata: Optional[Dict[str, str]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """Index a document's chunks into the graph.

        Returns a summary dict with counts.  Never raises — failures are
        logged and returned in the summary.
        """
        from retriva.indexing.qdrant_store import get_collection_name

        tenant_id = get_collection_name()

        if not settings.graph_enabled:
            return {"skipped": True, "reason": "graph_enabled is False"}

        if not chunks:
            return {"skipped": True, "reason": "no chunks"}

        try:
            # 1. Select extraction profile
            profile_id = "retriva:default"
            ext_reg = GraphExtensionRegistry()
            ext_profile = ext_reg.select_profile(source_type or "", user_metadata)
            if ext_profile:
                profile_id = ext_profile

            if cancel_check and cancel_check():
                return {"skipped": True, "reason": "cancelled"}

            # 2. Extract candidates
            mutation = self.extractor.extract(
                chunks=chunks,
                profile_id=profile_id,
                tenant_id=tenant_id,
                kb_id=kb_id,
                source_document_id=doc_id,
            )

            if cancel_check and cancel_check():
                return {"skipped": True, "reason": "cancelled"}

            # 3. Validate & normalize via extensions
            validated_entities: List[Entity] = []
            for entity in mutation.entities:
                errors = ext_reg.validate_entity(entity)
                if errors:
                    logger.warning(
                        f"GraphIndexer: entity '{entity.name}' validation "
                        f"errors: {errors} — skipping"
                    )
                    continue
                entity = ext_reg.normalize_entity(entity)
                validated_entities.append(entity)

            # 4. Resolve to canonical entities
            resolved_entities: List[Entity] = []
            temp_to_canonical: Dict[str, str] = {}
            for entity in validated_entities:
                canonical = self.resolver.resolve(entity)
                temp_to_canonical[entity.entity_id] = canonical.entity_id
                resolved_entities.append(canonical)

            # Remap assertion subject/object IDs to canonical IDs
            resolved_assertions: List[Assertion] = []
            for ast in mutation.assertions:
                ast.subject_entity_id = temp_to_canonical.get(
                    ast.subject_entity_id, ast.subject_entity_id
                )
                if ast.object_entity_id:
                    ast.object_entity_id = temp_to_canonical.get(
                        ast.object_entity_id, ast.object_entity_id
                    )
                # Filter by confidence threshold
                if ast.extraction_confidence < settings.graph_extraction_confidence_threshold:
                    continue
                resolved_assertions.append(ast)

            # 5. Build relationships from assertions
            relationships: List[Relationship] = []
            for ast in resolved_assertions:
                if ast.object_entity_id:
                    rel = Relationship(
                        tenant_id=ast.tenant_id,
                        kb_id=ast.kb_id,
                        source_entity_id=ast.subject_entity_id,
                        target_entity_id=ast.object_entity_id,
                        predicate=ast.predicate,
                        assertion_ids=[ast.assertion_id],
                        security_scope=ast.security_scope,
                    )
                    relationships.append(rel)

            # 6. Apply mutation
            final_mutation = GraphMutationRequest(
                tenant_id=tenant_id,
                kb_id=kb_id,
                entities=resolved_entities,
                assertions=resolved_assertions,
                relationships=relationships,
                source_document_id=doc_id,
                source_chunk_ids=mutation.source_chunk_ids,
            )
            self.store.apply_mutation(final_mutation)

            # 7. Publish events
            event_bus = GraphEventBus()
            now = datetime.now(timezone.utc).isoformat()
            entity_ids = [e.entity_id for e in resolved_entities]
            assertion_ids = [a.assertion_id for a in resolved_assertions]

            event_bus.publish(GraphChangeEvent(
                event_type=GraphEventType.ENTITY_CREATED,
                tenant_id=tenant_id,
                kb_id=kb_id,
                entity_ids=entity_ids,
                source_document_id=doc_id,
                timestamp=now,
            ))
            event_bus.publish(GraphChangeEvent(
                event_type=GraphEventType.ASSERTION_CREATED,
                tenant_id=tenant_id,
                kb_id=kb_id,
                assertion_ids=assertion_ids,
                source_document_id=doc_id,
                timestamp=now,
            ))
            event_bus.publish(GraphChangeEvent(
                event_type=GraphEventType.NEIGHBORHOOD_CHANGED,
                tenant_id=tenant_id,
                kb_id=kb_id,
                entity_ids=entity_ids,
                source_document_id=doc_id,
                timestamp=now,
            ))

            # Notify extensions
            ext_reg.notify_graph_change(GraphChangeEvent(
                event_type=GraphEventType.ENTITY_CREATED,
                tenant_id=tenant_id,
                kb_id=kb_id,
                entity_ids=entity_ids,
                source_document_id=doc_id,
                timestamp=now,
            ))

            logger.info(
                f"GraphIndexer: indexed doc={doc_id} — "
                f"{len(resolved_entities)} entities, "
                f"{len(resolved_assertions)} assertions, "
                f"{len(relationships)} relationships"
            )

            return {
                "skipped": False,
                "entities": len(resolved_entities),
                "assertions": len(resolved_assertions),
                "relationships": len(relationships),
                "profile": profile_id,
            }

        except Exception as e:
            logger.error(
                f"GraphIndexer: failed to index doc={doc_id}: {e}",
                exc_info=True,
            )
            return {"skipped": False, "error": str(e)}

    def delete_document(self, doc_id: str) -> int:
        """Delete graph data for a document (called on document deletion)."""
        if not settings.graph_enabled:
            return 0
        return self.store.delete_by_source(doc_id)
