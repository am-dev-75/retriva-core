# Modular GraphRAG — Configuration Reference

All GraphRAG settings are environment variables read by `retriva.config.Settings` (Pydantic `BaseSettings`). They are **all optional** and default to values that leave GraphRAG completely disabled, so the system behaves exactly as before unless explicitly enabled.

When using the local containerized deployment, set these in `.env` (see `retriva-local-containerized-deployment/.env.example`). The `docker-compose.yml` passes them through to the `retriva-ingestion`, `retriva-core`, and `retriva-worker` services.

---

## Enabling GraphRAG

| Variable | Default | Description |
|---|---|---|
| `GRAPH_ENABLED` | `false` | Master switch. When `false`, no graph indexing stage runs, no graph retrieval is available, and the system is indistinguishable from a non-GraphRAG deployment. Set to `true` to enable both the `GRAPH_INDEXING` ingestion stage and graph-aware retrieval modes. |
| `GRAPH_STORE_BACKEND` | `sqlite` | Graph store backend identifier. Phase 1 supports only `sqlite` (file-based, no additional container). Future phases may add dedicated graph databases behind the same `GraphStore` protocol. |
| `GRAPH_DEFAULT_RETRIEVAL_MODE` | `vector` | Retrieval mode used when the API request does not specify `retrieval_mode`. One of: `vector`, `graph_local`, `hybrid`. (`graph_global` and `auto` are designed but not implemented in Phase 1.) When `GRAPH_ENABLED=false`, this is ignored and `vector` is always used. |

---

## Extraction

| Variable | Default | Description |
|---|---|---|
| `GRAPH_EXTRACTION_CONFIDENCE_THRESHOLD` | `0.5` | Minimum confidence (0.0–1.0) for an LLM-extracted assertion to be persisted to the graph. Assertions below this threshold are silently dropped during indexing. Lower values retain more assertions at the cost of noise; higher values improve precision but may miss valid relationships. The default `DefaultEntityExtractor` asks the LLM to self-rate confidence; extension-provided extractors may use different scoring schemes. |

---

## Traversal & Retrieval Limits

These settings prevent graph explosion during retrieval. They apply to `graph_local` and `hybrid` retrieval modes and are enforced per query.

| Variable | Default | Description |
|---|---|---|
| `GRAPH_TRAVERSAL_MAX_DEPTH` | `2` | Maximum BFS depth for neighborhood expansion. Depth 1 returns direct neighbours; depth 2 includes neighbours-of-neighbours. Higher values increase recall but risk exponential blow-up. |
| `GRAPH_TRAVERSAL_MAX_NODES` | `50` | Maximum number of entities returned by a single neighborhood expansion. When this limit is reached, the result is marked `truncated: true` and a warning is added. |
| `GRAPH_TRAVERSAL_MAX_EDGES` | `100` | Maximum number of relationships (edges) traversed during a single neighborhood expansion. When reached, further edges are skipped and the result is marked `truncated: true`. |
| `GRAPH_MAX_CHUNKS_PER_ENTITY` | `5` | In `hybrid` mode, maximum number of supporting chunks retrieved per expanded graph entity. Prevents a single highly-connected entity from dominating the context budget. |
| `GRAPH_CONTEXT_TOKEN_BUDGET` | `4000` | Soft token budget for graph-derived context assembled by the retrieval orchestrator. Used to cap the total graph context injected into the LLM prompt. |
| `GRAPH_QUERY_TIMEOUT_SECONDS` | `5.0` | Maximum wall-clock duration for a single graph query (neighborhood expansion or path search). Queries that exceed this are aborted and return partial results with a timeout warning. |

---

## Complete `.env` Example

```
# --- GraphRAG ---
GRAPH_ENABLED=false
GRAPH_STORE_BACKEND=sqlite
GRAPH_DEFAULT_RETRIEVAL_MODE=vector
GRAPH_TRAVERSAL_MAX_DEPTH=2
GRAPH_TRAVERSAL_MAX_NODES=50
GRAPH_TRAVERSAL_MAX_EDGES=100
GRAPH_CONTEXT_TOKEN_BUDGET=4000
GRAPH_QUERY_TIMEOUT_SECONDS=5.0
GRAPH_MAX_CHUNKS_PER_ENTITY=5
GRAPH_EXTRACTION_CONFIDENCE_THRESHOLD=0.5
```

---

## Enabling GraphRAG for Testing

To enable GraphRAG in the local containerized deployment:

1. Copy `.env.example` to `.env` (if not already done).
2. Set `GRAPH_ENABLED=true`.
3. Optionally set `GRAPH_DEFAULT_RETRIEVAL_MODE=hybrid` to use hybrid vector+graph retrieval by default.
4. Rebuild and restart the Core services:

```bash
cd retriva-local-containerized-deployment
./manage.sh rebuild retriva-ingestion
./manage.sh rebuild retriva-core
./manage.sh rebuild retriva-worker
./manage.sh up
```

The graph store is a SQLite file at `<storage_path>/collections/<collection_name>/graph.db` on the `retriva_core_storage` Docker volume. No additional container or service is required.

---

## Retrieval Modes

The v2 retrieval API (`POST /api/v2/retrieval/query`) accepts an optional `retrieval_mode` field that overrides `GRAPH_DEFAULT_RETRIEVAL_MODE` per request:

| Mode | Requires `GRAPH_ENABLED` | Description |
|---|---|---|
| `vector` | No | Backward-compatible default. Qdrant vector search → rerank → hybrid select. |
| `graph_local` | Yes | Entity-centric: search entities by query text → expand bounded neighborhood → retrieve supporting chunks. |
| `hybrid` | Yes | Vector retrieval → map chunks to graph entities → expand neighborhood → retrieve supporting chunks → deduplicate → rerank → assemble provenance-aware context. |
| `graph_global` | Yes | Community-report-based search. **Designed, not implemented in Phase 1.** Falls back to `hybrid`. |
| `auto` | Yes | Automatic mode selection. **Designed, not implemented in Phase 1.** Falls back to `hybrid`. |

When `GRAPH_ENABLED=false` and a non-`vector` mode is requested, the system logs a warning and falls back to `vector`.

---

## Extension Configuration

GraphRAG extensions are loaded through the existing `RETRIVA_EXTENSIONS` environment variable (comma-separated dotted module paths). Extensions call `register_graph_extension()` in their `register()` hook to contribute vocabularies, extraction profiles, validators, normalizers, entity-resolution hints, retrieval hints, and graph-change event handlers.

```
RETRIVA_EXTENSIONS=my_extension.graph_module,another_extension
```

An unavailable or faulty optional extension does not prevent Core from processing documents with the generic extraction profile (`retriva:default`). Extension errors are logged and skipped.

---

## Related Documentation

- **Design spec**: [`../specs/016-graphrag/spec.md`](../specs/016-graphrag/spec.md)
- **Architecture**: [`specs/016-graphrag/architecture.md`](../specs/016-graphrag/architecture.md)
- **Backend ADR**: [`specs/016-graphrag/adr-001-graph-backend.md`](../specs/016-graphrag/adr-001-graph-backend.md)
- **Implementation plan**: [`specs/016-graphrag/plan.md`](../specs/016-graphrag/plan.md)
- **README section**: [Modular GraphRAG](../README.md#modular-graphrag)
