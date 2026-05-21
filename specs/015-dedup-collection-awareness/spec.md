# Feature Spec — Collection-Aware Deduplication

## Goal
Make the DeduplicationStore aware of the active Qdrant collection, so that
switching `QDRANT_COLLECTION_NAME` does not silently prevent re-ingestion of
documents whose chunks reside in a different collection.

## Problem Statement
The current `DeduplicationStore` keys records on `(kb_id, content_hash)`.
It does not track which Qdrant collection was used when the document was
originally indexed. If the operator changes `QDRANT_COLLECTION_NAME`
(e.g. for testing or multi-tenant deployments), the dedup store still finds
a matching record from the old collection and returns `already_exists`,
even though the chunks do not exist in the new collection.

This results in:
- Upload appears successful (HTTP 202, `status=already_exists`)
- No chunks are written to the current collection
- Document search returns 0 results

## In scope
- Add `collection_name` to `DocRecord`
- Include `collection_name` in the dedup lookup key: `(kb_id, content_hash, collection_name)`
- Re-ingest when the same content is uploaded to a different collection
- Migrate existing catalog records (default to legacy collection name)

## Out of scope
- Multi-collection support at the API level (single collection per Core instance)
- Cross-collection deduplication or chunk sharing
- Changes to Retriva Gateway or WebUI

## Functional requirements

### FR1 — Collection tracking
Each `DocRecord` in the dedup catalog shall include the `collection_name` that
was active at the time of ingestion.

### FR2 — Collection-scoped dedup lookup
`DeduplicationStore.get_by_hash()` shall match on `(kb_id, content_hash, collection_name)`.
A document previously ingested into collection A shall not block ingestion into
collection B.

### FR3 — Backward compatibility
Existing catalog files without `collection_name` shall be treated as belonging
to the default collection (`retriva_chunks`). No manual migration required.

### FR4 — Logging
When a document is re-ingested due to a collection change, the log shall clearly
indicate the reason (e.g. `collection_changed_reingestion`).

## Acceptance summary
The feature is accepted when changing `QDRANT_COLLECTION_NAME` and re-uploading
a previously ingested document results in the full pipeline executing and chunks
appearing in the new collection.
