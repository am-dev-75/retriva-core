# Retriva Ingestion API Reference

The Retriva Ingestion API provides endpoints for submitting documents, images, and raw chunks into the Retriva RAG pipeline.

All ingestion endpoints (except `/collection`) are asynchronous and return a `job_id` that can be used to track the status of the ingestion process.

## Base URL
Default: `http://127.0.0.1:8000/api/v1/ingest`

## User-Provided Metadata

All document-level endpoints accept an optional `user_metadata` field — a flat dictionary of string key/value pairs. This metadata is stored on the document, propagated to every chunk, and made visible during retrieval and in citations.

### Hard Limits

| Constraint                    | Limit          |
| ----------------------------- | -------------- |
| Maximum number of keys        | 20             |
| Maximum length per value      | 256 characters |
| Maximum serialized total size | 4096 bytes     |

If any limit is violated, the request is rejected with **422 Unprocessable Entity** and a structured error payload describing which limits were exceeded.

## Endpoints

### 1. Ingest HTML
`POST /html`

Ingests raw HTML content. The system will extract the title and main text content automatically.

**Payload:**
```json
{
  "source_path": "https://example.com/page",
  "page_title": "Example Page",
  "html_content": "<html>...</html>",
  "origin_file_path": "/path/to/local/file.html",
  "user_metadata": {"author": "Alice", "version": "2.0"}
}
```

### 2. Ingest Markdown
`POST /markdown`

Ingests structured Markdown content, split into sections. This ensures high-precision retrieval with section-aware metadata.

**Payload:**
```json
{
  "source_path": "/path/to/local/file.md",
  "page_title": "Document Title",
  "sections": [
    {
      "heading": "Introduction",
      "content": "This is the intro..."
    },
    {
      "heading": "Installation",
      "content": "Steps to install..."
    }
  ],
  "user_metadata": {"category": "docs"}
}
```

### 3. Ingest PDF Page
`POST /pdf`

Ingests text from a single PDF page. This is typically called page-by-page by the CLI.

**Payload:**
```json
{
  "source_path": "/path/to/local/file.pdf",
  "page_title": "Document Title",
  "content_text": "Text from page 1...",
  "page_number": 1,
  "total_pages": 10,
  "user_metadata": {"department": "engineering"}
}
```

### 4. Ingest MediaWiki Page
`POST /mediawiki`

Ingests a page extracted from a MediaWiki XML export.

**Payload:**
```json
{
  "source_path": "/path/to/export.xml",
  "page_title": "Wiki Page",
  "content_text": "Plain text content...",
  "page_id": 123,
  "namespace": 0,
  "linked_assets": ["/path/to/image.png"],
  "user_metadata": {"wiki": "internal"}
}
```

### 5. Ingest Standalone Image
`POST /image`

Ingests an image for VLM-based enrichment (visual description).

**Payload:**
```json
{
  "source_path": "/path/to/image.png",
  "page_title": "image",
  "file_path": "/path/to/image.png",
  "user_metadata": {"project": "schematics"}
}
```

### 6. Ingest Plain Text
`POST /text`

Ingests raw plain text.

**Payload:**
```json
{
  "source_path": "/path/to/note.txt",
  "page_title": "My Note",
  "content_text": "Hello world...",
  "user_metadata": {"source": "manual-entry"}
}
```

### 7. Ingest Raw Chunks
`POST /chunks`

Ingests pre-processed `Chunk` objects directly.

**Payload:**
```json
{
  "chunks": [
    {
      "text": "Chunk content",
      "metadata": {
        "doc_id": "...",
        "source_path": "...",
        "page_title": "...",
        "section_path": "...",
        "chunk_id": "...",
        "chunk_index": 0,
        "user_metadata": {"custom": "value"}
      }
    }
  ]
}
```

### 8. Clear Collection
`DELETE /collection`

Clears the vector database collection and re-initializes it. **Warning: This is a destructive operation.**

## Job Management

### Get Job Status
`GET /api/v1/jobs/{job_id}`

Returns the status of a specific ingestion job.

**Possible Statuses:**
- `queued`: Job is waiting for processing.
- `processing`: Job is currently being indexed.
- `completed`: Job finished successfully.
- `failed`: Job encountered an error.
- `cancelled`: Job was cancelled by the user.

## Common Responses

### 202 Accepted (Standard for Ingest)
```json
{
  "status": "accepted",
  "message": "Document accepted for processing",
  "job_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

### 400 Bad Request
Occurs if the payload is malformed or required fields are missing.

### 422 Unprocessable Entity (Metadata Validation)
Occurs when `user_metadata` violates hard limits.
```json
{
  "detail": [
    {
      "field": "user_metadata",
      "msg": "Too many keys: 25 exceeds maximum of 20"
    }
  ]
}
```

## Durable v2 Artifact Generation (Spec 026)

The v2 artifact workflow (`/api/v2/artifacts`) runs on the durable
Core jobs subsystem (Spec 025): **PostgreSQL is the sole
authoritative logical job store**; the rendered file remains owned by
the artifact storage provider and is referenced by bounded durable
result metadata. Celery (or the BackgroundTasks fallback) is
transport only — both use the identical durable lifecycle, and job
state survives API restarts and Redis loss.

**Job contract:** one registered job type `v2_artifact`
(payload contract `v2-1`; subject `artifact:<artifact_id>`;
`restart_safe=False` — `basic_report` may incur LLM provider cost,
which is never replayed automatically). Each accepted `POST` creates
a NEW artifact and a NEW durable job (no client idempotency key; a
canonical input fingerprint is persisted for diagnostics). The
collection/knowledge-base context is resolved server-side, persisted
bounded in the durable input metadata, and re-validated at execution.

**Client-visible behavior (compatibility preserved):**
- `POST /api/v2/artifacts` → 202 `{status, message, job_id,
  artifact_id}`; `job_id` is the durable Core job id.
- `GET /api/v2/artifacts/{artifact_id}` → durable status projection
  (`completed` / `pending` / `running` / `failed` / `cancelled`
  family) with additive progress fields
  (`current_stage` / `stages_completed` — phases:
  `fetching_data` → `rendering` → `finalizing`).
- `GET /api/v2/artifacts/{artifact_id}/content` → 200 with a
  deterministic format media type while the artifact exists; 202
  while non-terminal; 404 unknown/missing; 410 failed/cancelled
  with a SANITIZED detail (raw exception text is never exposed).
- `DELETE /api/v2/artifacts/{artifact_id}` → idempotent 204; records
  durable cancellation intent (Celery revoke is a non-guaranteed aid
  only) and removes the artifact file. A finalized artifact whose
  render already completed may legitimately remain `succeeded`
  (Spec 025 T15 — completion proven beats a racing cancel); the
  deletion then only removes the file.
- No list, no pagination, and **no public retry endpoint** — retries
  are operator actions (`python -m retriva.jobs.retry`).

**Finalization and recovery:** renderers write to a
`<artifact_id><ext>.partial` file in the final directory; the worker
flushes/closes, verifies cooperative cancellation, computes
size + SHA-256, and atomically finalizes with `os.replace` — a
partial render can never occupy the final name. A bounded
provenance sidecar (tenant/artifact/job/format/checksum/size) is
written at finalization so reconciliation can ADOPT a completed
artifact when the process crashed between finalization and the
success callback — ONLY with verified provenance; anything
uncertain goes to bounded `manual_review` (operator resolution).
Succeeded jobs whose file is missing emit a one-time bounded
anomaly event and download as 404. Job-history retention NEVER
deletes generated artifacts (an artifact may outlive its job
record; deletion remains an artifact-API operation).

Pre-deployment artifact jobs were in-memory only and are NOT
migrated (they were already lost on restart; they remain
unresolvable).
