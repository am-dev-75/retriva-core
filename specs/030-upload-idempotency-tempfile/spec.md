# Spec 030 — Upload idempotency, conflict mapping, and temp-file lifecycle

- **Status:** ACCEPTED (owner acceptance recorded 2026-10-07; Constitution
  §42, §43). Implementation authorized; live deployment is a separate phase.
- **Revision:** 2 (2026-10-07) — revision 1 presented `PROPOSED`; owner accepted
  revision 1 on 2026-10-07 and implementation proceeded.
- **Repository:** retriva-core
- **Governing documents:** `.agent/rules/retriva-constitution.md`; ADR-035;
  Spec 025 §3.1 / ADR-030 (idempotency contract); Spec 028 / ADR-033
  (knowledge authority); Spec 029 / ADR-034 (lock order). Spec 028 and Spec 029
  are NOT reopened.
- **Owner:** Retriva Core owner (acceptance required).

## 1. Problem

`POST /api/v2/documents/upload` returns HTTP 500 for a semantically identical
re-upload, and leaks one temporary file per rejected duplicate. Root cause (see
ADR-035 and §5): the durable-job idempotency key is content-stable, but the
compared `input_fingerprint` includes request-volatile `temp_path` and
`created_at`; the resulting `IdempotencyConflictError` is unmapped; temp files
are not cleaned on owner/terminal paths.

## 2. Normative requirements

**R1 — Canonical input identity.** The upload idempotency input identity MUST be
a deterministic, versioned (`v2upload-input/1`) serialization over stable
semantic fields only (tenant, operation/job type, KB scope, normalized source
identity, content identity, normalized content type, canonicalized stable user
metadata, parser hint, processing-contract fields, accepted force/replace
flags). Request-volatile/transport fields (`temp_path`, receipt `created_at`,
trace/request/span ids, process/container identity, ephemeral staging filename,
stream identity, retry counters, network metadata) MUST be excluded.

**R2 — Canonicalization.** Deterministic JSON, stable key ordering, explicit
null-vs-absent handling, stable scalar formatting, timezone-independent;
SHA-256 hex; no secret values or raw content in the fingerprint or logs;
synthetic test vectors committed.

**R3 — Same key + same canonical identity.** Reuse the existing durable
job/result per the accepted v2 contract; create no new job, attempt, source,
document, version, ingestion, manifest, or Qdrant operation; return a
deterministic success/idempotent-reuse response; clean the request-local temp
file; expose no internal temp path or fingerprint.

**R4 — Same key + different canonical identity.** Deterministic typed
`IdempotencyConflictError` mapped to **HTTP 409** (unless governance specifies
another bounded status) with a stable public error code; no stack trace, SQL,
fingerprint, temp path, content hash, or private metadata; no second job or
partial domain side effect; clean the request-local temp file.

**R5 — Different key + same content.** Preserve accepted source-identity,
versioning, dedup, and job-submission semantics; do not collapse requests merely
by content-hash equality.

**R6 — API mapping.** `IdempotencyConflictError` MUST NOT escape as 500; map it
at the narrowest common v2 submission boundary; consistent across all upload
routes; no API v1 work; OpenAPI updated only if the public contract requires it.

**R7 — Temp-file ownership.** Explicit ownership state machine; exactly one
current owner; exactly-once idempotent missing-safe deletion; no deletion before
worker-safe acquisition; cleanup confined to the configured upload temp root
with symlink/path-traversal safety; startup recovery sweep for abandoned files;
low-cardinality metrics; Celery and local-fallback parity.

**R8 — Preserve accepted architecture.** Spec 029 `jobs -> job_attempts`, Spec
028 authority/Qdrant/frozen evidence, migration ledgers, and RLS/roles are
unchanged. Migration disposition: **none**.

**R9 — Isolation.** Live `cust_0007` MUST NOT be modified by this phase; exactly
one local Core commit after all gates; a separate deployment prompt is produced
but not executed.

## 3. Acceptance criteria

- **A1** Baseline reproduction of HTTP 500 + `IdempotencyConflictError` +
  temp-file leak on the pristine pre-fix baseline (isolated, real methods).
- **A2** Canonical identity excludes all volatile fields and includes all
  material stable fields (test vectors).
- **A3** Same key + same canonical identity reuses one job/result (no duplicate
  attempt/version/ingestion/manifest/Qdrant operation).
- **A4** Same key + different canonical identity returns bounded 409 with a
  stable code and no leakage; no side effect.
- **A5** Every success, duplicate, conflict, validation-failure, submission-
  failure, cancellation, interruption, Celery, and local-fallback path removes
  the request-local temp file exactly once (leak ledger empty at terminal).
- **A6** Deterministic concurrency/race matrix passes (real PostgreSQL
  uniqueness; barriers/events, no arbitrary sleeps).
- **A7** Real isolated runtime (PostgreSQL, Redis, Celery, API, worker, local
  fallback, Qdrant, parser, temp storage) validation passes; restart/reconnect
  gates pass.
- **A8** Spec 029 AST invariant + concurrency tests remain green (zero inverse
  paths, zero known-cycle deadlocks); Spec 028 authority/frozen evidence
  unchanged.
- **A9** Compatibility (upload v2, generic document, MediaWiki, job
  status/cancel, OpenAPI, CRM/Gateway/Web UI where they submit uploads) passes.
- **A10** Security/privacy: tenant-scoped isolation, no temp path/fingerprint
  leakage, no arbitrary deletion, no new DDL/privilege, low-cardinality metrics.
- **A11** No migration required (or stop for additional governance).
- **A12** Exactly one local Core commit; separate deployment prompt produced.

## 4. Out of scope

Spec 028/029 architecture; Qdrant; lock-order refactor; CRM/Messaging/gateway/
deployment/connector source; GraphIndexer/GraphRAG; stale superseded-point
cleanup; migration; unrelated test cleanup; live deployment;
push/merge/rebase/tag/PR/release.

## 5. Governance and phase gates

1. Present this pack + ADR-035 (`PROPOSED`) with registry entries.
2. **Owner acceptance required** before any implementation (Constitution §42,
   §43). "Accept with changes" ⇒ `CHANGES_REQUESTED`, re-present.
3. On `ACCEPTED`: implement, run the concurrency/runtime/leak/compatibility/
   security gates, create one local Core commit, produce the deployment prompt.
4. Live deployment is a separate governed phase.

## 6. Current status

`ACCEPTED` (2026-10-07). Discovery, input inventory, and isolated baseline
reproduction were completed under revision 1. Owner acceptance is recorded;
implementation, deterministic tests, isolated runtime validation, and exactly
one local Core commit followed. The live `cust_0007` stack is deployed
separately and was unchanged by this phase.

### 6.1 Owner acceptance record (2026-10-07)

The owner accepted Spec 030 and ADR-035. Authorized: status change to
`ACCEPTED`; canonical `v2upload-input/1` identity; equivalent-duplicate reuse;
narrow `IdempotencyConflictError` → HTTP 409 mapping; shared `UploadTempFile`
ownership/cleanup; deterministic and real-runtime validation on isolated
PostgreSQL/Redis/Celery/API/worker/local fallback/Qdrant; exactly one local Core
commit; deployment-prompt finalization. Not authorized: live deployment/live
duplicate-upload tests; changes to Spec 028 knowledge authority, Spec 029 lock
ordering, Qdrant identity, CRM/Messaging/Gateway/deployment/connectors,
GraphIndexer/GraphRAG, superseded-point cleanup, or API v1; push/merge/rebase/
tag/PR/release. Expected disposition: no migration; a migration would require
additional owner acceptance before creation.