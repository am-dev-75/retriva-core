# Architecture — 016 GraphRAG as a Core Platform Capability

> Version 0.1 · 2026-07-12

---

## 1  Layered View

```
┌──────────────────────────────────────────────────────────────────┐
│  FastAPI apps (openai_api, ingestion_api)                         │
│  ── resolve implementations via CapabilityRegistry ──            │
├──────────────────────────────────────────────────────────────────┤
│  Retrieval Orchestrator                                           │
│  ── VECTOR | GRAPH_LOCAL | HYBRID (AUTO/GRAPH_GLOBAL designed) ─│
├──────────────────────────────────────────────────────────────────┤
│  Graph Query Service          Graph Indexer                      │
│  ── entity search             ── post-indexing stage             │
│  ── neighborhood expansion    ── extraction → resolution → store │
│  ── bounded traversal         ── provenance persistence          │
│  ── evidence retrieval        ── event publication               │
│  ── temporal filtering                                           │
├──────────────────────────────────────────────────────────────────┤
│  Entity Resolution Service   Graph Event Bus                     │
│  ── canonical IDs             ── in-process pub/sub              │
│  ── aliases / external IDs    ── Redis pub/sub adapter           │
│  ── merge candidates                                           │
├──────────────────────────────────────────────────────────────────┤
│  Graph Contracts (Pydantic models + Protocols)                   │
│  ── Entity, Assertion, Relationship, Event, Evidence,            │
│     Community, GraphMutation, GraphQuery, GraphResult,           │
│     GraphPath, ExtractionProfile, VocabularyRegistration,        │
│     GraphChangeEvent                                             │
├──────────────────────────────────────────────────────────────────┤
│  Graph Store Abstraction (Protocol)                              │
│  ── upsert/retrieve entities, assertions, provenance             │
│  ── neighborhoods, bounded paths, filtered queries               │
│  ── delete-by-source, idempotent reprocessing                    │
├──────────────────────────────────────────────────────────────────┤
│  Graph Store Implementation (Phase 1: SQLite-backed)             │
├══════════════════════════════════════════════════════════════════┤
│  Extension SDK/SPI                                               │
│  ── vocabulary registration, extraction profiles,                │
│     validators, normalizers, resolution hints,                   │
│     retrieval hints, event handlers, projection handlers         │
├──────────────────────────────────────────────────────────────────┤
│  Existing Retriva Infrastructure                                 │
│  ── Qdrant (vectors), SQLite (registry/dedup), Redis (Celery)   │
│  ── CapabilityRegistry, Protocols, Config                        │
└──────────────────────────────────────────────────────────────────┘
```

## 2  Package Map (new modules)

```
src/retriva/
├── graph/                          # NEW — GraphRAG platform service
│   ├── __init__.py
│   ├── contracts.py                # Pydantic models (storage-neutral)
│   ├── protocols.py                # Protocol interfaces
│   ├── entity_resolution.py        # EntityResolutionService
│   ├── graph_indexer.py            # GraphIndexer (post-indexing stage)
│   ├── graph_query_service.py      # GraphQueryService
│   ├── retrieval_orchestrator.py   # GraphRetrievalOrchestrator
│   ├── event_bus.py                # GraphEventBus (in-process + Redis)
│   ├── extraction.py               # DefaultEntityExtractor (generic)
│   ├── vocabulary.py               # VocabularyRegistry
│   └── stores/
│       ├── __init__.py
│       └── sqlite_graph_store.py   # Phase 1 backend
├── graph_ext/                      # NEW — Extension SDK/SPI
│   ├── __init__.py
│   └── spi.py                      # Protocols for extension registration
├── domain/
│   └── models.py                   # EXTENDED — GraphEnhancedResult
├── qa/
│   ├── retriever.py                # EXTENDED — retrieval_mode param
│   └── answerer.py                 # EXTENDED — graph context assembly
├── ingestion_api/
│   ├── routers/
│   │   └── v2_documents.py         # EXTENDED — GRAPH_INDEXING stage
│   └── schemas_v2.py               # EXTENDED — retrieval_mode field
├── config.py                       # EXTENDED — graph settings
└── protocols.py                    # EXTENDED — GraphStore protocol
```

## 3  Graph Contracts (`graph/contracts.py`)

All contracts are Pydantic `BaseModel` subclasses. No graph-DB-specific
types are exposed.

### 3.1  Entity

```python
class EntityCategory(str, Enum):
    PERSON = "retriva:Person"
    ORGANIZATION = "retriva:Organization"
    PRODUCT = "retriva:Product"
    PROJECT = "retriva:Project"
    LOCATION = "retriva:Location"
    EVENT = "retriva:Event"
    TECHNOLOGY = "retriva:Technology"
    REGULATION = "retriva:Regulation"
    CONCEPT = "retriva:Concept"
    UNKNOWN = "retriva:Unknown"

class Entity(BaseModel):
    entity_id: str                    # canonical ID (assigned by EntityResolutionService)
    tenant_id: str                    # collection_name in current Retriva
    kb_id: str                        # knowledge-space ID
    name: str
    name_normalized: str              # normalized form for matching
    category: EntityCategory = EntityCategory.UNKNOWN
    entity_type: Optional[str] = None # namespaced type, e.g. "crm:Opportunity"
    namespace: str = "retriva"        # owning namespace
    aliases: list[str] = []
    external_ids: dict[str, str] = {} # system → ID mapping
    description: Optional[str] = None
    properties: dict[str, Any] = {}   # extension metadata
    security_scope: list[str] = []    # authorized scopes (kb_id-based in Phase 1)
    created_at: str
    updated_at: str
```

### 3.2  Assertion

```python
class AssertionClass(str, Enum):
    EXTRACTED = "extracted"           # LLM-extracted
    INFERRED = "inferred"             # derived via reasoning
    HUMAN_VALIDATED = "human_validated"
    SOURCE_METADATA = "source_metadata"  # deterministic from document metadata
    OPERATIONAL = "operational"       # materialized projection

class AssertionStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    INVALIDATED = "invalidated"

class Assertion(BaseModel):
    assertion_id: str
    tenant_id: str
    kb_id: str
    subject_entity_id: str
    predicate: str                    # namespaced, e.g. "retriva:worksFor"
    object_entity_id: Optional[str] = None  # None for literal assertions
    object_value: Optional[str] = None      # literal value
    assertion_class: AssertionClass
    source_document_ids: list[str] = []
    source_chunk_ids: list[str] = []
    source_spans: list[dict] = []     # [{chunk_id, start, end}]
    extraction_confidence: float = 0.0
    extractor_profile: str = "retriva:default"
    extractor_version: str = "0.1.0"
    observed_at: Optional[str] = None
    ingested_at: str
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    status: AssertionStatus = AssertionStatus.ACTIVE
    superseded_by: Optional[str] = None
    security_scope: list[str] = []
    metadata: dict[str, Any] = {}
```

### 3.3  Relationship, Event, Evidence, Community

```python
class Relationship(BaseModel):
    relationship_id: str
    tenant_id: str
    kb_id: str
    source_entity_id: str
    target_entity_id: str
    predicate: str
    assertion_ids: list[str] = []     # backing assertions
    properties: dict[str, Any] = {}
    security_scope: list[str] = []

class GraphEvent(BaseModel):
    event_id: str
    tenant_id: str
    kb_id: str
    entity_id: Optional[str] = None
    event_type: str                   # namespaced
    description: str
    observed_at: Optional[str] = None
    source_assertion_ids: list[str] = []
    security_scope: list[str] = []

class EvidenceReference(BaseModel):
    document_id: str
    chunk_id: str
    span: Optional[dict] = None       # {start, end}
    text_snippet: Optional[str] = None

class Community(BaseModel):
    community_id: str
    tenant_id: str
    kb_id: str
    entity_ids: list[str] = []
    level: int = 0
    title: Optional[str] = None
    summary: Optional[str] = None     # populated when community reports are implemented
    security_scope: list[str] = []
```

### 3.4  Temporal & Security Metadata

Temporal semantics are embedded directly in `Assertion` (`valid_from`,
`valid_to`, `observed_at`, `ingested_at`, `superseded_by`, `status`).
Security metadata is embedded as `security_scope: list[str]` on every
graph object. In Phase 1, `security_scope` contains `kb_id` values; the
query layer filters on this before returning results.

### 3.5  Mutation, Query, Result, Path

```python
class GraphMutationRequest(BaseModel):
    tenant_id: str
    kb_id: str
    entities: list[Entity] = []
    assertions: list[Assertion] = []
    relationships: list[Relationship] = []
    events: list[GraphEvent] = []
    evidence: list[EvidenceReference] = []
    source_document_id: str
    source_chunk_ids: list[str] = []

class GraphQueryRequest(BaseModel):
    tenant_id: str
    kb_id: str
    entity_ids: list[str] = []
    entity_name: Optional[str] = None
    predicates: list[str] = []
    max_depth: int = 2
    max_nodes: int = 50
    max_edges: int = 100
    as_of: Optional[str] = None       # temporal filter
    security_scope: list[str] = []    # authorized scopes

class GraphPath(BaseModel):
    start_entity_id: str
    end_entity_id: str
    entity_ids: list[str]
    predicates: list[str]
    assertion_ids: list[str] = []

class GraphRetrievalResult(BaseModel):
    entities: list[Entity] = []
    assertions: list[Assertion] = []
    relationships: list[Relationship] = []
    paths: list[GraphPath] = []
    evidence: list[EvidenceReference] = []
    graph_score: float = 0.0
    warnings: list[str] = []
    truncated: bool = False
```

### 3.6  Extraction Profile & Vocabulary Registration

```python
class ExtractionProfile(BaseModel):
    profile_id: str                   # e.g. "retriva:default", "crm:opportunity"
    namespace: str
    supported_source_types: list[str] = []
    entity_types: list[str] = []
    predicates: list[str] = []
    description: str = ""

class VocabularyRegistration(BaseModel):
    namespace: str                    # e.g. "crm", "cyber", "example"
    entity_types: list[str] = []      # e.g. ["crm:Opportunity"]
    predicates: list[str] = []        # e.g. ["crm:blockedBy"]
    event_types: list[str] = []
    description: str = ""
```

### 3.7  Graph-Change Events

```python
class GraphChangeEvent(BaseModel):
    event_id: str
    event_type: str  # GraphEntityCreated | GraphEntityUpdated | GraphEntityMerged |
                     # GraphAssertionCreated | GraphAssertionSuperseded |
                     # GraphAssertionInvalidated | GraphNeighborhoodChanged |
                     # GraphCommunityUpdated
    tenant_id: str
    kb_id: str
    entity_ids: list[str] = []
    assertion_ids: list[str] = []
    source_document_id: Optional[str] = None
    timestamp: str
    metadata: dict[str, Any] = {}
```

## 4  Graph Store Abstraction (`graph/protocols.py`)

```python
@runtime_checkable
class GraphStore(Protocol):
    """Storage-neutral graph store port."""

    def upsert_entities(self, entities: list[Entity]) -> None: ...
    def upsert_assertions(self, assertions: list[Assertion]) -> None: ...
    def upsert_relationships(self, rels: list[Relationship]) -> None: ...
    def store_provenance(self, doc_id: str, chunk_ids: list[str],
                         entity_ids: list[str], assertion_ids: list[str]) -> None: ...

    def get_entity(self, entity_id: str) -> Optional[Entity]: ...
    def get_entities_by_alias(self, name: str, tenant_id: str,
                              kb_id: str) -> list[Entity]: ...
    def get_entities_by_external_id(self, system: str, external_id: str,
                                    tenant_id: str) -> Optional[Entity]: ...
    def get_assertions(self, entity_id: str,
                       status: Optional[AssertionStatus] = None) -> list[Assertion]: ...

    def get_neighborhood(self, entity_id: str, max_depth: int,
                         max_nodes: int, max_edges: int,
                         security_scope: list[str],
                         as_of: Optional[str] = None) -> GraphRetrievalResult: ...

    def find_paths(self, source_id: str, target_id: str, max_depth: int,
                   security_scope: list[str]) -> list[GraphPath]: ...

    def search_entities(self, query: str, tenant_id: str, kb_id: str,
                        limit: int = 20) -> list[Entity]: ...

    def get_impacted_entities(self, entity_id: str) -> list[str]: ...

    def delete_by_source(self, document_id: str) -> int: ...
    def invalidate_by_source(self, document_id: str) -> int: ...
```

## 5  Extension SDK/SPI (`graph_ext/spi.py`)

Extensions register through the existing `CapabilityRegistry` mechanism
(`RETRIVA_EXTENSIONS` env var → `module.register(registry)` hook) plus a
dedicated `GraphExtensionRegistry` for graph-specific contributions.

```python
@runtime_checkable
class GraphExtension(Protocol):
    """SPI for graph-aware extensions."""

    def register_vocabulary(self, reg: "VocabularyRegistry") -> None: ...
    def get_extraction_profiles(self) -> list[ExtractionProfile]: ...
    def select_profile(self, source_type: str,
                       user_metadata: Optional[dict] = None) -> Optional[str]: ...
    def validate_entity(self, entity: Entity) -> list[str]: ...   # returns errors
    def normalize_entity(self, entity: Entity) -> Entity: ...
    def provide_resolution_hints(self, entity: Entity) -> dict: ...
    def provide_retrieval_hints(self, query: str,
                                entities: list[Entity]) -> dict: ...
    def on_graph_change(self, event: GraphChangeEvent) -> None: ...
    def build_projection(self, event: GraphChangeEvent) -> None: ...
```

Extensions that only need vocabulary registration can implement a subset;
the `GraphExtensionRegistry` treats missing methods as no-ops.

## 6  Graph Indexer Integration

The graph indexer hooks into `process_document_v2` as a new
`GRAPH_INDEXING` stage after `INDEXING`:

```
DETECTING → PREPROCESSING → PARSING → NORMALIZATION → CHUNKING →
INDEXING → GRAPH_INDEXING (optional)
```

When `settings.graph_enabled` is `False` (the default), the stage is
skipped entirely and the pipeline behaves exactly as before.

When enabled:

1. The `GraphIndexer` receives the `ParsedDocument`, its `Chunk` list,
   and the `doc_id` / `kb_id` / `collection_name`.
2. It selects an extraction profile (Core default or extension-provided).
3. The `DefaultEntityExtractor` (or extension override) extracts candidate
   entities and assertions from chunk text.
4. Extracted entities are validated and normalized by registered extensions.
5. The `EntityResolutionService` resolves candidates to canonical entities
   (exact + normalized matching; merge candidates recorded but not
   auto-merged in Phase 1).
6. A `GraphMutationRequest` is built and sent to the `GraphStore`.
7. Provenance is persisted (document → chunks → entities → assertions).
8. `GraphChangeEvent`s are published.
9. Failures are logged but do **not** fail the ingestion job or affect the
   vector index. Retries follow the same `celery_task_max_retries` policy.

## 7  Retrieval Orchestrator

### 7.1  Retrieval Modes

```python
class RetrievalMode(str, Enum):
    VECTOR = "vector"                 # backward-compatible default
    GRAPH_LOCAL = "graph_local"       # entity-centric graph expansion
    GRAPH_GLOBAL = "graph_global"     # designed, not implemented in Phase 1
    HYBRID = "hybrid"                 # vector + graph
    AUTO = "auto"                     # designed, not implemented in Phase 1
```

### 7.2  HYBRID Flow

```
1. Run existing vector + rerank + hybrid-select pipeline  (unchanged)
2. Map retrieved chunks → graph entities via provenance
3. Expand through bounded, authorized graph neighborhood
   (max_depth=2, max_nodes=50, max_edges=100 by default)
4. Retrieve supporting chunks for expanded entities/assertions
5. Deduplicate and normalize candidates
6. Apply existing reranking pipeline to merged candidates
7. Assemble provenance-aware context (GraphEnhancedResult)
8. Return source citations + graph paths
```

### 7.3  Limits (configurable)

| Limit | Default | Setting |
|---|---|---|
| Traversal depth | 2 | `graph_traversal_max_depth` |
| Max nodes | 50 | `graph_traversal_max_nodes` |
| Max edges | 100 | `graph_traversal_max_edges` |
| Token budget | 4000 | `graph_context_token_budget` |
| Query duration | 5s | `graph_query_timeout_seconds` |
| Per-source contribution | 5 | `graph_max_chunks_per_entity` |

### 7.4  Common Retrieval Result

```python
class GraphEnhancedResult(BaseModel):
    """Wraps the existing chunk-list result with optional graph data."""
    chunks: list[dict]                # backward-compatible chunk list
    entities: list[Entity] = []
    assertions: list[Assertion] = []
    paths: list[GraphPath] = []
    vector_scores: dict[str, float] = {}   # chunk_id → score
    graph_scores: dict[str, float] = {}    # entity_id → score
    reranker_scores: dict[str, float] = {} # chunk_id → score
    retrieval_mode: RetrievalMode = RetrievalMode.VECTOR
    provenance: dict[str, list[EvidenceReference]] = {}
    warnings: list[str] = []
    authorization_decisions: list[dict] = []
    truncated: bool = False
```

Callers that ignore graph fields see only `chunks` and behave as before.

## 8  Entity Resolution

```python
class EntityResolutionService:
    """Core-owned entity resolution."""

    def resolve(self, candidate: Entity) -> Entity:
        """Return the canonical entity for a candidate.

        Phase 1 strategy (conservative):
        1. Exact match on (tenant_id, name_normalized, category).
        2. Match on external_ids[system].
        3. Match on alias (normalized).
        4. If no match, create a new canonical entity.
        5. If multiple matches, record a merge candidate (no auto-merge).
        """

    def get_merge_candidates(self, tenant_id: str) -> list[dict]: ...
    def approve_merge(self, canonical_id: str, duplicate_id: str) -> None: ...
    def reverse_merge(self, canonical_id: str, restored_id: str) -> None: ...
```

## 9  Graph Event Bus

```python
class GraphEventBus:
    """In-process pub/sub with optional Redis adapter."""

    def publish(self, event: GraphChangeEvent) -> None: ...
    def subscribe(self, event_type: str,
                  handler: Callable[[GraphChangeEvent], None]) -> None: ...

    # When settings.celery_broker_url is set, events are also published
    # to a Redis channel ("retriva:graph:events") for cross-process
    # subscribers (e.g. extension projection builders running in workers).
```

## 10  Security Model

### 10.1  Phase 1 Boundary

- **Tenant** = `collection_name` (set by `CollectionMiddleware`).
- **Knowledge space** = `kb_id`.
- Every graph object carries `tenant_id` + `kb_id` + `security_scope`.
- Graph queries filter on `security_scope` **before** returning results.
- No graph object, assertion, path, or embedding may cross a
  `(tenant_id, kb_id)` boundary.

### 10.2  Threat Model

| Threat | Mitigation |
|---|---|
| **Relationship leakage**: user infers entity existence from graph paths derived from inaccessible documents. | Phase 1: graph objects inherit `kb_id` from source documents. Queries filter on `security_scope` before returning. |
| **Community summary leakage**: community report combines sources from different security scopes. | Phase 1: community reports are not generated. When implemented, they will be generated within compatible security scopes or dynamically assembled. |
| **Inference via assertion count**: user infers document existence from assertion density. | Phase 1: assertions are filtered by `security_scope`. Count-based inference is documented as a residual risk. |
| **Cross-tenant data leakage**: graph query returns entities from another tenant. | `tenant_id` is a mandatory filter on every graph store operation. |

### 10.3  Document Deletion

When a document is deleted (`DELETE /api/v2/documents/{doc_id}`), the
graph store's `delete_by_source(document_id)` is called to remove or
invalidate all entities, assertions, and provenance links derived from
that document. This is idempotent and does not affect the vector index
(which has already been cleaned by the existing deletion flow).

## 11  Configuration

New settings added to `retriva.config.Settings`:

```python
# GraphRAG
graph_enabled: bool = False
graph_store_backend: str = "sqlite"   # Phase 1: only "sqlite"
graph_traversal_max_depth: int = 2
graph_traversal_max_nodes: int = 50
graph_traversal_max_edges: int = 100
graph_context_token_budget: int = 4000
graph_query_timeout_seconds: float = 5.0
graph_max_chunks_per_entity: int = 5
graph_extraction_confidence_threshold: float = 0.5
graph_default_retrieval_mode: str = "vector"  # vector | graph_local | hybrid
```

## 12  Backend Decision (summary — full ADR in `adr-001-graph-backend.md`)

**Decision**: SQLite as the Phase 1 graph store backend.

**Rationale**:
- Retriva already depends on SQLite (KB registry, dedup catalog).
- No additional container or service is required.
- Consistent with the local containerized deployment model.
- Adequate for the entity/assertion volumes of Phase 1 (thousands to
  tens-of-thousands of entities per tenant).
- Traversal is implemented in Python (bounded BFS), not in SQL recursion,
  keeping the schema simple and portable.
- When the graph outgrows SQLite, the `GraphStore` protocol allows
  swapping to a dedicated graph database without changing contracts.

**When to revisit**: when entity count exceeds ~100K per tenant or
multi-hop traversal depth > 3 becomes a common query pattern.
