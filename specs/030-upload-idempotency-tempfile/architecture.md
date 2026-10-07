# Spec 030 — Architecture

Status: PROPOSED. Governing: ADR-035. Repository: retriva-core.

## 1. Current structure

- `src/retriva/ingestion_api/durable_jobs.py`
  - `submit_upload_job(...)` builds `payload` (including `temp_path`,
    `created_at`), sets `idempotency_key = v2up:{hash(kb|content_hash)}`, and
    sets `input_metadata["input_fingerprint"] = fingerprint_for(payload)`.
  - `fingerprint_for(identity)` = `sha256(json.dumps(identity, sort_keys=True,
    default=str))`.
- `src/retriva/jobs/repository.py` (`submit_job`) compares the stored vs new
  `input_fingerprint`; a mismatch raises `IdempotencyConflictError`.
- `src/retriva/ingestion_api/routers/v2_documents.py` `upload_document_v2`
  creates the temp file with `tempfile.mkstemp` and calls `submit_upload_job`;
  there is no `IdempotencyConflictError` handler, so it surfaces as 500, and the
  temp file is not removed on that path.

## 2. Target design

### 2.1 Canonical upload identity (`v2upload-input/1`)

A new pure function, e.g. `canonical_upload_identity(...) -> dict`, in
`durable_jobs.py` returns the ordered, normalized mapping:

```
{ "schema": "v2upload-input/1",
  "tenant_id": <str>,
  "operation": "v2_upload",
  "kb_id": <str>,
  "source_identity": {"source_path": <normalized str>,
                       "source_paths": <sorted normalized list|null>},
  "content_identity": {"content_hash": <"sha256:..." normalized>},
  "content_type": <normalized mime|null>,
  "parser_hint": <str|null>,
  "user_metadata": <canonicalized stable map|null>,
  "processing": {"payload_version": <str>, "collection_name": <str|null>,
                  "ingestion_status": <str>,
                  "force": <bool|null>} }
```

`input_fingerprint = sha256( json.dumps(identity, sort_keys=True,
separators=(",",":"), ensure_ascii=True) ).hexdigest()`; schema version is part
of the hashed structure.

Normalization rules: NFC strings; POSIX path separators; case-preserving source
paths (identity is case-sensitive); explicit `null` vs absent unified to `null`
for known keys; MIME lowercased with parameters ordered; `user_metadata`
restricted to scalar/stable values, keys sorted, values string-normalized;
timestamps never included.

### 2.2 Conflict mapping

Register an exception handler (or narrow try/except) at the v2 submission
boundary mapping `IdempotencyConflictError` → HTTP 409
`{"error_code": "upload_idempotency_conflict", "message": <bounded>}`. Applies
to all v2 submission routes; no API v1 path.

### 2.3 Temp-file ownership

Introduce a small `UploadTempFile` guard (context manager) that owns a
`mkstemp` path under the configured upload temp root and guarantees exactly-once
idempotent removal (`missing_ok`) on exit unless ownership was explicitly
transferred.

Ownership states: `handler_owned` → (a) `transferred_to_worker` (payload
carries `temp_path`; worker deletes after acquiring input) or (b)
`transferred_to_local` (local runner deletes after processing) or
`released_by_reuse`/`released_conflict`/`released_failure` (route deletes).
Worker deletion is idempotent and missing-safe; a startup recovery sweep removes
abandoned files older than a bounded age under the temp root only.

### 2.4 Semantics preservation

No change to Spec 029 lock primitives, Spec 028 knowledge registration, job
transition semantics, or Qdrant operations. The fix is additive: canonical
identity replaces the ad-hoc payload fingerprint for the upload path.

## 3. Testing architecture

- Unit: canonicalization test vectors (volatile-only diffs collapse; stable diffs
  diverge); `fingerprint_for` compatibility retained for other job types.
- Integration (real isolated PostgreSQL): same-key/same-input reuse; same-key/
  different-input 409; different-key/same-content semantics.
- Route-level (real API + isolated PG/Redis/Celery/Qdrant): HTTP 500 → 409
  mapping; temp-file before/after accounting per scenario.
- Concurrency matrix via barriers/events against real PostgreSQL uniqueness and
  the real claim path; restart/reconnect gates.
- Leak ledger (external JSON) with one entry per scenario.

## 4. Migration disposition

None. No schema/RLS/grant/trigger/index change. Historical persisted
fingerprints are not rewritten; new submissions use the canonical schema.