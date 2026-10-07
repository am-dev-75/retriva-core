# ADR-035: Canonical upload submission identity and temp-file ownership

- **Status:** ACCEPTED (owner acceptance 2026-10-07; Constitution §42, §43).
  Supersedes its own PROPOSED state; no prior accepted ADR amended or
  superseded.
- **Date:** 2026-10-07
- **Repository:** retriva-core
- **Amends / relates:** clarifies the accepted idempotency contract of Spec 025
  §3.1 and ADR-030 (durable jobs) for the v2 **upload** submission path; does
  not change Spec 025 transition semantics, the Spec 028 knowledge authority,
  the Spec 029 lock-order invariant, Qdrant, or stores of record.
- **Deciders:** Retriva Core owner (acceptance recorded 2026-10-07).

## Acceptance record (2026-10-07)

The owner accepted ADR-035 and Spec 030. The decision below is binding.
Implementation (canonical `v2upload-input/1` identity, narrow
`IdempotencyConflictError` → HTTP 409 mapping, shared `UploadTempFile`
ownership/cleanup, deterministic and isolated runtime validation) is authorized
and was performed under isolated validation, followed by exactly one local Core
commit. Live deployment remains a separate governed phase and was not performed
here. No schema migration was required. Spec 028 and Spec 029 remain closed and
untouched; a migration would require additional owner acceptance before
creation.

## Context

Spec 025 §3.1 (ACCEPTED) already states: repeating a submission with the same
`(tenant, job_type, idempotency_key)` returns the existing job, and reusing the
key with a **different input identity** is a conflict (409). However, no accepted
artifact defines **what the input identity is composed of** for uploads, and no
accepted artifact governs **temporary upload file ownership**.

Implementation defect (reproduced deterministically on the pristine baseline,
`110a824a…`, isolated PostgreSQL):

- `durable_jobs.submit_upload_job` derives a content-stable idempotency key
  (`v2up:{hash(kb|content_hash)}`) but computes the compared
  `input_fingerprint` with `fingerprint_for(payload)` over a payload that
  **includes request-volatile fields** `temp_path` (per-request `mkstemp`) and
  `created_at` (request receipt time).
- `repository.submit_job` therefore sees a `different input identity` for a
  semantically identical re-upload and raises `IdempotencyConflictError`.
- The v2 upload route does not map that error → **HTTP 500**.
- Each rejected duplicate leaves its `mkstemp` temporary file behind.

Baseline reproduction (isolated PG, real methods): fingerprints differ when only
`temp_path`/`created_at` differ; the second submit raises
`IdempotencyConflictError`; a canonical projection excluding the volatile fields
is equal; and identical same-fingerprint submissions reuse one job. This is a
normal-workflow defect (`POST /api/v2/documents/upload`).

Because the correction defines a **durable cross-workflow idempotency contract**
(field set + canonicalization + HTTP mapping) and a **temporary-file ownership
state machine**, §43 requires this decision in an ADR before implementation.

## Decision

1. **Canonical upload input identity.** The upload idempotency input identity is
   a deterministic serialization of an explicit, versioned schema
   (`v2upload-input/1`) over **stable semantic fields only**: tenant scope;
   operation/job type; KB scope; stable source identity (normalized
   `source_path`/`source_paths`); content identity (`content_hash`, or content
   fingerprint); normalized `content_type`; stable canonicalized
   `user_metadata`; `parser_hint`; processing-contract fields (`payload_version`,
   `collection_name`, `ingestion_status`, accepted force/replace flags). Hash:
   SHA-256 hex of canonical JSON, explicitly versioned.

2. **Excluded (volatile/transport) fields.** `temp_path`; request receipt
   `created_at` when it does not affect the operation; trace/span/request IDs;
   process/container identity; ephemeral staging filename when it is not source
   identity; upload stream object identity; retry counter; network metadata.
   Derived identifiers (`doc_id`) are excluded from the fingerprint and treated
   as derived from content identity.

3. **Canonicalization rules.** Deterministic JSON with stable key ordering;
   explicit null-vs-absent handling; stable scalar formatting; timezone-
   independent representation; no secret values; no raw content in the
   fingerprint or logs. Test vectors committed as synthetic fixtures.

4. **Semantics.** Same key + same canonical identity → reuse the existing
   durable job/result (no new job/attempt/version/ingestion/manifest/Qdrant
   operation); clean the request-local temp file. Same key + different canonical
   identity → deterministic typed `IdempotencyConflictError` mapped to **HTTP
   409** with a stable machine-readable public code and no internal leakage;
   no side effect; clean the temp file. Different key + same content follows the
   accepted source-identity/dedup/versioning semantics unchanged.

5. **HTTP mapping.** `IdempotencyConflictError` is mapped at the narrowest common
   v2 submission boundary to 409. No API v1 work; no broad remapping of
   unrelated state conflicts.

6. **Temp-file ownership.** One explicit ownership state machine; exactly one
   current owner per file; exactly-once, idempotent, missing-safe deletion;
   deletion only after worker-safe acquisition; cleanup confined to the
   configured upload temp root with symlink/race safety; low-cardinality
   cleanup metrics; Celery and local-fallback parity; startup recovery sweep
   for abandoned files.

7. **No migration.** The change is code + tests; no schema, RLS, grant, trigger,
   or index change. Disposition: **no migration required**.

## Consequences

- Equivalent uploads reuse one durable job; genuine conflicts return a bounded
  409; no temp-file leak.
- Spec 025 transition semantics, Spec 028 knowledge authority, and the Spec 029
  `jobs -> job_attempts` lock order are unchanged.
- Historical persisted fingerprints are not rewritten; new submissions use the
  canonical schema (compatibility handled per Spec 030 rollout).

## Compliance

- Constitution §42/§43 (governance before implementation): Spec 030 and this ADR
  are presented `PROPOSED`; implementation MUST NOT begin until both are
  `ACCEPTED`.
- §8/§12 (idempotent, additive compatibility), §26 (determinism), §32 (tenant
  isolation), §30 (auditability), §35 (secure temporary-file handling)
  preserved.

## Alternatives considered

- **Keep volatile fields and return 409 for identical re-uploads:** rejected —
  contradicts Spec 025 §3.1 (same input identity must reuse) and breaks client
  retries.
- **Best-effort temp cleanup only:** rejected — leaves unbounded orphans.
- **Move to a separate idempotency store:** rejected — duplicates the accepted
  jobs unique-index mechanism (Spec 025 §3.1).