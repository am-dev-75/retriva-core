# Retriva Ingestion API — v1 Retirement Notice

**Retriva API v1 was removed by Spec 027 / ADR-032 (accepted
2026-10-05).** The supported ingestion surface is **API v2**
(`/api/v2/...`), which runs asynchronous work on the durable
PostgreSQL-backed Core jobs subsystem (Specs 025/026).

## Removed routes

All Retriva API v1 paths were removed and now return the framework's
ordinary 404 (no retirement router, no feature flag):

- `POST /api/v1/ingest/chunks`
- `POST /api/v1/ingest/html`
- `POST /api/v1/ingest/text`
- `POST /api/v1/ingest/markdown`
- `POST /api/v1/ingest/pdf`
- `POST /api/v1/ingest/upload/pdf`
- `POST /api/v1/ingest/image`
- `POST /api/v1/ingest/mediawiki`
- `DELETE /api/v1/ingest/collection`
- `GET /api/v1/jobs`, `GET /api/v1/jobs/{job_id}`,
  `POST /api/v1/jobs/{job_id}/cancel`
- `DELETE /api/v1/documents/{doc_id}`,
  `DELETE /api/v1/documents/metadata/filter`

The OpenAI-compatible API (`/v1/chat/completions`, port 8001) is a
separate surface and is NOT affected.

## v2 replacements

| removed v1 operation | v2 replacement |
|---|---|
| file/text/markdown/pdf/html ingestion | `POST /api/v2/documents` (by `source_uri`) and `POST /api/v2/documents/upload` (multipart) |
| MediaWiki XML export ingestion | `POST /api/v2/documents/mediawiki` (staged directory; durable job) |
| document deletion | `DELETE /api/v2/documents/{doc_id}` |
| metadata-filter deletion | `DELETE /api/v2/documents/filter` |
| job status / list / cancel | `GET /api/v2/jobs`, `GET /api/v2/jobs/{job_id}`, `POST /api/v2/jobs/{job_id}/cancel` (tenant-scoped, durable) |

## Removed operations with no replacement

- **Standalone image ingestion** (`/api/v1/ingest/image`): no
  v2-native image surface exists yet; v2-native image ingestion is
  deferred governed work. The CLI's image handler now fails locally
  with bounded guidance and makes no HTTP request.
- **Collection clear** (`DELETE /api/v1/ingest/collection`): no v2
  equivalent; the CLI `reindex` command is retired and fails locally
  (use `DELETE /api/v2/documents/{doc_id}` per document or manage the
  Qdrant collection directly, then `ingest`).
- Raw chunk-list ingestion (`/api/v1/ingest/chunks`): superseded by
  the v2 document pipeline's chunking.

## Behavior notes

- Old clients calling removed v1 paths receive ordinary 404 without
  internal detail.
- Pre-removal v1 job IDs are NOT migrated and may be unresolvable
  after removal/restart (they were in-memory/Redis-only and already
  vanished on any restart). No synthetic durable history is created.
- Deploying this change requires restarting the Core ingestion
  service (new image).
- Version-control rollback of the Core change restores the previous
  surface; no database migration is involved (none was added).
- User-metadata validation and limits are unchanged and shared by the
  v2 surfaces (`retriva.ingestion_api.metadata_validation`: ≤ 20
  keys, ≤ 256 chars per value, ≤ 4096 serialized bytes).
