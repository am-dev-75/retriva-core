# ADR-001: Graph Store Backend for GraphRAG Phase 1

> Status: Accepted · 2026-07-12

## Context

GraphRAG requires a graph store to persist entities, assertions,
relationships, and provenance. The store must:

- Support entity upsert/retrieve by ID, alias, and external ID.
- Support assertion storage with temporal and status filtering.
- Support bounded neighborhood traversal (BFS up to depth N).
- Support path discovery between two entities.
- Filter by tenant, knowledge space, and security scope.
- Support delete/invalidate by source document.
- Be idempotent for reprocessing.
- Be containerizable and self-hostable.
- Be consistent with Retriva's deployment model (local containerized).

## Options Considered

### Option A: Neo4j

- **Pros**: Mature graph database, Cypher query language, built-in
  traversal, community detection algorithms.
- **Cons**: 
  - Community Edition license (GPLv3) is incompatible with Retriva's
    Apache-2.0 core. Enterprise Edition is commercial.
  - Requires a separate container with significant memory footprint
    (≥ 1GB JVM heap).
  - Introduces a new operational dependency for every deployment.
  - Public contracts would be tempted to leak Cypher-specific concepts.
  - Overkill for Phase 1 entity/assertion volumes.

### Option B: Memgraph

- **Pros**: In-memory, fast, Cypher-compatible, MIT-licensed.
- **Cons**:
  - Requires a separate container.
  - In-memory storage means data loss on restart unless snapshots are
    configured.
  - Adds operational complexity for Phase 1.
  - Memory consumption scales with graph size, which is unpredictable.

### Option C: Apache AGE (PostgreSQL extension)

- **Pros**: Runs on PostgreSQL, which is well-understood. Cypher support.
- **Cons**:
  - Requires a PostgreSQL container (Retriva currently uses no PostgreSQL).
  - AGE is relatively young; fewer production deployments.
  - Adds a significant new dependency for Phase 1.

### Option D: ArangoDB

- **Pros**: Multi-model (graph + document), Apache-2.0 licensed.
- **Cons**:
  - Requires a separate container.
  - AQL is a new query language to learn and maintain.
  - Overkill for Phase 1.

### Option E: Qdrant (reuse existing)

- **Pros**: No new dependency. Already containerized.
- **Cons**:
  - Qdrant is a vector database, not a graph database.
  - No native graph traversal. Multi-hop queries would require multiple
    round-trips with application-side join logic.
  - Payload filtering is not optimized for graph adjacency patterns.
  - Would conflate vector storage and graph storage responsibilities.

### Option F: SQLite (chosen)

- **Pros**:
  - Retriva already depends on SQLite (`registry.db`, `dedup_catalog.json`
    is being migrated toward SQLite patterns).
  - No additional container or service. The database is a file on the
    existing `retriva_core_storage` volume.
  - Consistent with the local containerized deployment model.
  - Adequate for Phase 1 volumes (thousands to tens-of-thousands of
    entities per tenant).
  - WAL journaling provides good read/write concurrency.
  - Schema is simple and portable: entities, assertions, relationships,
    provenance, aliases, external_ids — all as tables with JSON columns
    for flexible properties.
  - Traversal is implemented in Python (bounded BFS using SQL queries
    for edge lookup), keeping the schema simple and avoiding SQL
    recursion portability issues.
  - The `GraphStore` protocol abstracts the backend, so swapping to a
    dedicated graph database later requires no contract changes.
- **Cons**:
  - Not a real graph database. Complex traversals (depth > 3, full
    community detection) will be slow.
  - No native graph algorithms (PageRank, Louvain, etc.).
  - Single-writer per database file (mitigated by WAL + connection-per-call
    pattern already used by `RegistryDB`).

## Decision

Use **SQLite** as the Phase 1 graph store backend.

The implementation follows the same patterns as `RegistryDB`:
- One SQLite file per subsystem: `<storage_path>/collections/<collection>/graph.db`.
- Connection-per-call with a module-level `threading.Lock` for writes.
- WAL journaling.
- Idempotent schema creation (`CREATE TABLE IF NOT EXISTS`).
- JSON columns for flexible properties (`properties`, `metadata`,
  `source_spans`, etc.).

Traversal is implemented as bounded BFS in Python, issuing SQL queries
for edge lookup at each depth level. This avoids SQL recursion
portability issues and keeps the traversal logic in the application
layer where it can be optimized or replaced independently.

## Consequences

- No new container or service is added to `docker-compose.yml`.
- Graph data is stored alongside existing Retriva data on the
  `retriva_core_storage` volume.
- When entity count exceeds ~100K per tenant or multi-hop traversal
  depth > 3 becomes common, a new ADR should evaluate migrating to a
  dedicated graph database (Neo4j, Memgraph, or ArangoDB).
- The `GraphStore` protocol ensures this migration requires no changes
  to public contracts, the retrieval orchestrator, or the extension SDK.
