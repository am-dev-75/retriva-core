# Feature Spec — 016 GraphRAG as a Core Platform Capability

## Goal

Add a shared, storage-neutral knowledge-graph indexing and retrieval
capability integrated into Retriva's existing ingestion and query pipelines.

GraphRAG is a **Retriva Core platform service**, not a domain-specific
subsystem. It must benefit every Retriva function — whether built into Core
or supplied by a commercial extension — without exposing any specific graph
database or GraphRAG framework through public contracts.

## Background

Retriva currently uses Qdrant for dense vector retrieval. The retrieval
pipeline (`qa/retriever.py` → `qa/answerer.py`) performs vector search,
cross-encoder reranking, hybrid selection, and context budgeting. The
ingestion pipeline (`ingestion_api/routers/v2_documents.py ::
process_document_v2`) runs a six-stage flow: DETECTING → PREPROCESSING →
PARSING → NORMALIZATION → CHUNKING → INDEXING.

GraphRAG extends both pipelines:

- **Ingestion**: after chunking and vector indexing, a new GRAPH_INDEXING
  stage extracts entities, assertions, and relationships from the document's
  chunks and writes them to a graph store with full provenance.
- **Retrieval**: vector results are mapped to graph entities, expanded
  through a bounded, authorized neighborhood, and merged with supporting
  chunks to produce a provenance-aware context.

## In scope — Phase 1 (this spec)

- Storage-neutral graph contracts (Pydantic models + Protocols).
- Generic Core knowledge model (Entity, Assertion, Relationship, Event,
  Evidence, Community placeholder).
- Graph store abstraction (Protocol) + one initial backend implementation.
- Extension SDK/SPI for vocabulary registration, extraction profiles,
  validators, normalizers, entity-resolution hints, retrieval hints,
  graph-change event handlers, and domain projection handlers.
- Entity resolution (conservative: exact + normalized matching, merge
  candidates only — no automatic probabilistic merges).
- Graph indexer integrated into `process_document_v2` as an optional,
  non-breaking post-indexing stage.
- Graph query service (entity search, neighborhood expansion, bounded
  traversal, evidence retrieval, temporal filtering).
- Retrieval orchestrator with `VECTOR` (default, backward-compatible),
  `GRAPH_LOCAL`, and `HYBRID` modes. `GRAPH_GLOBAL` and `AUTO` are
  designed but not implemented in Phase 1.
- Common retrieval result wrapper that extends the current chunk-list
  result without breaking existing consumers.
- Graph-change events (in-process pub/sub; Redis pub/sub when available).
- Security-trimmed graph retrieval (document-level authorization extends
  to graph-derived knowledge).
- ADR documenting the graph backend decision.
- Tests using neutral example extensions (`example:ProjectRisk`,
  `example:dependsOn`).

## Out of scope — Phase 1

- Community detection and community report generation (designed, not
  implemented).
- Global GraphRAG search (designed, not implemented).
- Automatic probabilistic entity merging (merge candidates only).
- CRM-specific schemas or code in the Core repository.
- A second graph backend implementation.
- External message-queue event publishing (Redis pub/sub is sufficient
  for Phase 1; Kafka/NATS can be added later behind the same event
  interface).

## Constraints

- The OSS Core **must not** import or depend on proprietary code.
- Public contracts **must not** expose Neo4j, Memgraph, ArangoDB, Apache
  AGE, Microsoft GraphRAG, or any other implementation-specific type.
- Graph indexing failures **must not** corrupt or silently invalidate the
  existing vector index.
- Backward compatibility: callers that do not use graph data must continue
  to work unchanged.
- Tenant and knowledge-space isolation is mandatory. No query, traversal,
  community report, cache entry, or embedding may cross an unauthorized
  boundary.
- An unavailable or faulty optional extension **must not** prevent Core
  from processing documents with the generic extraction profile.
- No proprietary CRM code or schemas in the public Core repository.

## Discrepancies with current implementation

1. **No graph store exists.** The current stack has Qdrant (vectors) and
   SQLite (KB registry, dedup catalog). A graph store must be added.

2. **No authorization model on chunks.** Chunks carry `kb_id` and
   `user_metadata` but there is no per-document ACL. Graph security
   trimming will initially rely on `kb_id` + `collection_name` scoping
   (the existing security boundary) and will document the inference-leakage
   risk when community summaries are added in a later phase.

3. **No event bus.** The current architecture has no pub/sub mechanism.
   Graph-change events will introduce an in-process event dispatcher with
   a Redis pub/sub adapter for multi-process deployments.

4. **Retrieval result is `List[Dict]`.** The current retrieval result is a
   list of chunk dictionaries. The common retrieval result wrapper must
   preserve this shape for backward compatibility while adding optional
   graph fields.

5. **`process_document_v2` has no post-indexing hook.** The pipeline ends
   at INDEXING. A GRAPH_INDEXING stage must be added as an optional,
   configurable step that is skipped when graph features are disabled.
