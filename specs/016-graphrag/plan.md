# Implementation Plan — 016 GraphRAG

## Phases Overview

| Phase | Scope | Status |
|---|---|---|
| **Phase 1** | Contracts, store, SPI, indexer, query service, hybrid retrieval, events | **This implementation** |
| Phase 2 | Community detection, community reports, global GraphRAG | Designed, not implemented |
| Phase 3 | Probabilistic entity resolution, automatic merges | Designed, not implemented |
| Phase 4 | Per-document ACLs (beyond kb_id scoping), inference-leakage hardening | Designed, not implemented |
| Phase 5 | Second graph backend (if needed), graph embeddings | Future |

---

## Phase 1 — Detailed Tasks

### Task 1: Graph Contracts (`graph/contracts.py`)
- Pydantic models: `Entity`, `Assertion`, `Relationship`, `GraphEvent`,
  `EvidenceReference`, `Community`, `GraphMutationRequest`,
  `GraphQueryRequest`, `GraphPath`, `GraphRetrievalResult`,
  `ExtractionProfile`, `VocabularyRegistration`, `GraphChangeEvent`.
- Enums: `EntityCategory`, `AssertionClass`, `AssertionStatus`,
  `RetrievalMode`.
- `GraphEnhancedResult` in `domain/models.py` (backward-compatible wrapper).

### Task 2: Graph Protocols (`graph/protocols.py`)
- `GraphStore` protocol (upsert, retrieve, neighborhood, paths, search,
  delete-by-source, invalidate-by-source).
- `EntityExtractor` protocol.
- `GraphExtension` SPI protocol (in `graph_ext/spi.py`).

### Task 3: SQLite Graph Store (`graph/stores/sqlite_graph_store.py`)
- Schema: `graph_entities`, `graph_assertions`, `graph_relationships`,
  `graph_events`, `graph_provenance`, `graph_aliases`, `graph_external_ids`,
  `graph_merge_candidates`.
- Connection-per-call + write lock (mirrors `RegistryDB`).
- Bounded BFS traversal in Python.
- Idempotent upsert (ON CONFLICT DO UPDATE).
- Delete/invalidate by source document.

### Task 4: Entity Resolution (`graph/entity_resolution.py`)
- `EntityResolutionService` with conservative matching:
  exact → external_id → alias → new entity.
- Merge candidate recording (no auto-merge).
- `approve_merge` / `reverse_merge` (human-approved).

### Task 5: Vocabulary Registry (`graph/vocabulary.py`)
- `VocabularyRegistry` singleton (thread-safe, like `CapabilityRegistry`).
- `register(VocabularyRegistration)`.
- `get_namespaced_types(namespace)`.
- `is_registered(type_name)`.

### Task 6: Default Entity Extractor (`graph/extraction.py`)
- `DefaultEntityExtractor` implementing `EntityExtractor` protocol.
- Uses the chat LLM (via existing `settings.chat_*` config) to extract
  entities and assertions from chunk text.
- Generic extraction profile (`retriva:default`).
- Confidence scoring.
- No CRM-specific logic.

### Task 7: Graph Indexer (`graph/graph_indexer.py`)
- `GraphIndexer` class registered as `graph_indexer` capability.
- Hooks into `process_document_v2` as `GRAPH_INDEXING` stage.
- Flow: select profile → extract → validate/normalize (extensions) →
  resolve → mutate → persist provenance → publish events.
- Failure isolation: logs error, does not fail the ingestion job.
- Idempotent: reprocessing a document invalidates prior assertions from
  that document before inserting new ones.

### Task 8: Graph Query Service (`graph/graph_query_service.py`)
- `GraphQueryService` registered as `graph_query_service` capability.
- Entity search, neighborhood expansion, bounded traversal, path
  discovery, evidence retrieval, temporal filtering.
- Security-scope filtering before returning results.
- Configurable limits (depth, nodes, edges, timeout).

### Task 9: Retrieval Orchestrator (`graph/retrieval_orchestrator.py`)
- `GraphRetrievalOrchestrator` registered as `retrieval_orchestrator`.
- Modes: `VECTOR` (default), `GRAPH_LOCAL`, `HYBRID`.
- HYBRID flow: vector → map to entities → expand → retrieve supporting
  chunks → deduplicate → rerank → assemble `GraphEnhancedResult`.
- Strict limits enforcement.

### Task 10: Graph Event Bus (`graph/event_bus.py`)
- `GraphEventBus` singleton.
- In-process pub/sub (dict of event_type → handlers).
- Redis pub/sub adapter (when `celery_broker_url` is set).
- Event types: `GraphEntityCreated`, `GraphEntityUpdated`,
  `GraphEntityMerged`, `GraphAssertionCreated`,
  `GraphAssertionSuperseded`, `GraphAssertionInvalidated`,
  `GraphNeighborhoodChanged`, `GraphCommunityUpdated` (stub).

### Task 11: Configuration (`config.py`)
- New settings: `graph_enabled`, `graph_store_backend`,
  `graph_traversal_max_depth`, `graph_traversal_max_nodes`,
  `graph_traversal_max_edges`, `graph_context_token_budget`,
  `graph_query_timeout_seconds`, `graph_max_chunks_per_entity`,
  `graph_extraction_confidence_threshold`,
  `graph_default_retrieval_mode`.

### Task 12: Pipeline Integration
- `v2_documents.py`: add `GRAPH_INDEXING` to `JobStage` enum.
- `process_document_v2`: call `GraphIndexer` after INDEXING when
  `graph_enabled` is True.
- `v2_documents.py` delete endpoints: call `graph_store.delete_by_source()`.
- `schemas_v2.py`: add `retrieval_mode` field to `RetrievalRequest`.
- `v2_retrieval.py`: pass `retrieval_mode` to retriever.
- `qa/retriever.py`: `DefaultRetriever.retrieve()` accepts
  `retrieval_mode` param (default `VECTOR`).
- `qa/answerer.py`: use `GraphEnhancedResult` when graph data is present.

### Task 13: Extension SDK (`graph_ext/spi.py`)
- `GraphExtension` protocol.
- `GraphExtensionRegistry` (separate from `CapabilityRegistry` but loaded
  via the same `RETRIVA_EXTENSIONS` mechanism).
- Extensions call `register_graph_extension(ext)` in their `register()`
  hook.
- Missing methods are treated as no-ops.

### Task 14: Tests
- `test_graph_contracts.py` — model validation, serialization.
- `test_sqlite_graph_store.py` — CRUD, traversal, delete-by-source,
  idempotency, security filtering.
- `test_entity_resolution.py` — exact, alias, external_id, merge candidates.
- `test_vocabulary_registry.py` — registration, lookup, namespacing.
- `test_graph_indexer.py` — end-to-end indexing with mock extractor.
- `test_graph_query_service.py` — search, neighborhood, paths, temporal.
- `test_retrieval_orchestrator.py` — VECTOR, GRAPH_LOCAL, HYBRID modes.
- `test_graph_event_bus.py` — pub/sub, Redis adapter (mocked).
- `test_graph_extension_loading.py` — neutral example extension
  (`example:ProjectRisk`, `example:dependsOn`).

### Task 15: Docker Compose & Deployment
- No new service required (SQLite is file-based).
- `graph.db` lives on the existing `retriva_core_storage` volume.
- `.env.example` updated with `GRAPH_ENABLED` and related settings.

---

## Phase 2 — Community Detection & Global GraphRAG (designed)

- Community detection (Louvain or label propagation) as a background
  Celery task.
- Community report generation (LLM-summarized) within compatible security
  scopes.
- `GRAPH_GLOBAL` retrieval mode: query community reports.
- `AUTO` retrieval mode: route between VECTOR, GRAPH_LOCAL, GRAPH_GLOBAL
  based on query characteristics.

## Phase 3 — Probabilistic Entity Resolution (designed)

- Configurable matching thresholds.
- Automatic merges above confidence threshold.
- Human review queue for below-threshold candidates.
- Extension-provided identity hints (e.g. CRM system IDs, email domains).

## Phase 4 — Per-Document ACLs (designed)

- Document-level ACL metadata on chunks.
- Propagation to graph objects.
- Per-entity, per-assertion security scope beyond kb_id.
- Inference-leakage hardening for community summaries.
