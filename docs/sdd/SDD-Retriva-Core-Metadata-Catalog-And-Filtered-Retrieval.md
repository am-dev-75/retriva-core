# SDD Pack — Retriva Core Metadata Catalog and Filtered Retrieval APIs

## Status
Proposed

## Scope
Retriva Core only.

This SDD adds the missing Core APIs required to support structured metadata/catalog queries and metadata-filtered retrieval. It assumes user-provided metadata is already stored in Qdrant payloads as `user_metadata` and also available or recoverable at document level.

---

## Motivation

Retriva already stores user-provided tags in chunk payloads, for example:

```json
"user_metadata": {
  "kb_id": "default",
  "project": "apollo",
  "department": "r&d"
}
```

This enables Qdrant payload filtering such as:

```json
{
  "filter": {
    "must": [
      {
        "key": "user_metadata.project",
        "match": { "value": "apollo" }
      }
    ]
  }
}
```

However, Retriva Core needs first-class APIs for:

- listing documents by metadata
- counting documents by metadata
- discovering user-defined metadata keys
- discovering metadata values
- running vector retrieval with metadata filters
- grouping chunk-level matches into document-level results

Without these APIs, Gateway and WebUI are forced to misuse semantic RAG for catalog/listing questions.

---

## Design Principles

### DP-1 — Metadata remains structured

User metadata must remain structured payload, not injected into embedded text.

### DP-2 — Document catalog queries are document-level

Catalog/list/count APIs must return unique documents, not raw chunks.

### DP-3 — Filtered retrieval combines metadata and semantics

Metadata filters constrain the search space. Vector search ranks content within that filtered space.

### DP-4 — Arbitrary user-defined tags

The system must support arbitrary user-defined metadata keys and values.

---

## API Surface

All APIs are under `/api/v2`.

---

## Document Catalog APIs

### GET `/api/v2/documents`

List documents with optional filters.

Query parameters:

```text
kb_id
q
metadata.<key>
limit
offset
sort
```

Example:

```http
GET /api/v2/documents?metadata.project=apollo
```

Response:

```json
{
  "items": [
    {
      "doc_id": "prj_apollo/costs.png",
      "title": "costs.png",
      "source_path": "prj_apollo/costs.png",
      "chunk_count": 1,
      "user_metadata": {
        "project": "apollo",
        "department": "r&d"
      },
      "ingestion_timestamp": "2026-05-10T22:29:36Z"
    }
  ],
  "limit": 50,
  "offset": 0,
  "total": 1
}
```

Implementation may use Qdrant scroll with payload filters and group by `doc_id` if no separate document catalog table exists.

### GET `/api/v2/documents/count`

Returns the number of unique documents matching filters.

Example:

```http
GET /api/v2/documents/count?metadata.project=apollo
```

Response:

```json
{
  "count": 2
}
```

### GET `/api/v2/documents/{doc_id}`

Return document-level metadata and summary.

### DELETE `/api/v2/documents/{doc_id}`

Delete a document and all associated chunks.

Deletion should be idempotent.

---

## Metadata Discovery APIs

### GET `/api/v2/metadata/schema`

Returns observed metadata keys and basic statistics.

Response:

```json
{
  "keys": [
    {
      "key": "project",
      "path": "user_metadata.project",
      "type": "string",
      "document_count": 2,
      "value_count": 1
    },
    {
      "key": "department",
      "path": "user_metadata.department",
      "type": "string",
      "document_count": 2,
      "value_count": 1
    }
  ]
}
```

### GET `/api/v2/metadata/values`

Returns known values for a metadata key.

Example:

```http
GET /api/v2/metadata/values?key=project
```

Response:

```json
{
  "key": "project",
  "values": [
    { "value": "apollo", "document_count": 2 }
  ]
}
```

---

## Metadata-Filtered Retrieval API

### POST `/api/v2/retrieval/query`

Runs semantic retrieval with optional metadata filters.

Request:

```json
{
  "query": "costs",
  "kb_ids": ["default"],
  "filters": {
    "user_metadata.project": "apollo"
  },
  "top_k": 20,
  "rerank": true,
  "hybrid_selection": true
}
```

Behavior:

- Convert filters into Qdrant payload filters.
- Run vector search within filtered subset.
- Apply reranking if enabled.
- Apply hybrid selection if enabled.
- Return chunks with source metadata.

Response:

```json
{
  "chunks": [
    {
      "doc_id": "prj_apollo/costs.png",
      "chunk_id": "...",
      "text": "...",
      "score": 0.82,
      "user_metadata": {
        "project": "apollo"
      }
    }
  ]
}
```

---

## Qdrant Payload Indexes

Core should create payload indexes for commonly used metadata fields when possible.

Recommended indexes:

```text
user_metadata.kb_id
user_metadata.project
user_metadata.department
```

Because users may define arbitrary keys, Core should also support on-demand or best-effort index creation for observed metadata keys.

If index creation is not available or fails, queries must still work via payload filtering, but may be slower.

---

## Document-Level Grouping

When catalog APIs are implemented using Qdrant chunk payloads, Core must group results by `doc_id`.

Rules:

- One result per document.
- Preserve representative fields such as `page_title`, `source_path`, `user_metadata`, and `ingestion_timestamp`.
- Compute `chunk_count` when feasible.
- Avoid letting large documents dominate catalog results because of chunk count.

---

## Metadata Normalization

Metadata matching should support:

- case-insensitive key matching
- case-insensitive string value matching where feasible
- exact matching for canonical stored values

Core may preserve original casing in returned metadata.

---

## Observability

Core must log:

```text
document_catalog_query
metadata_schema_query
metadata_values_query
filtered_retrieval_query
qdrant_filter_built
document_grouping_completed
```

Logs must include correlation ID if provided, filters, result counts, and duration in milliseconds.

---

## Non-Goals

This SDD does not include:

- Changing existing payload shape
- Injecting metadata into embeddings
- Implementing Gateway intent detection
- Implementing WebUI filter UX
- Requiring a relational document catalog table if Qdrant grouping is sufficient for first implementation

---

## Acceptance Criteria

1. `GET /api/v2/documents?metadata.project=apollo` returns all unique documents tagged with project apollo.
2. Both `costs.png` and `Rust For Beginners` are returned when both documents share the metadata tag.
3. Catalog APIs return document-level results, not duplicate chunk-level results.
4. `GET /api/v2/documents/count?metadata.project=apollo` returns the correct unique document count.
5. `GET /api/v2/metadata/schema` returns observed user metadata keys.
6. `GET /api/v2/metadata/values?key=project` returns observed values such as `apollo`.
7. `POST /api/v2/retrieval/query` supports metadata filters.
8. Filtered retrieval applies Qdrant payload filters before vector ranking.
9. Existing unfiltered RAG behavior remains unchanged.
10. No metadata is injected into embedded content text as part of this feature.

---

## One-Sentence Summary

Retriva Core gains first-class metadata catalog and filtered retrieval APIs so document-listing and tag-based queries use structured metadata instead of dense semantic retrieval.
