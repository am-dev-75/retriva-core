# Governed evidence — MediaWiki changed-content correction (2026-10-08)

Status: **implemented, validated in isolation; live deployment pending separate
authorization.** This annex records evidence for a bounded conformance
correction under **already accepted** contracts. It does not revise any accepted
Spec 025/028/029/030/031 decision; it cross-references them.

## 1. Live incident (contained)

A single synthetic `mediawiki-local` page update through the real connector →
Gateway → Core v2 durable path produced, on Core commit `c9acf281…`:

- `KnowledgeRepositoryError` during `record_intent`; the transaction rolled back
  leaving the replacement version `staging` with **0 chunks / 0 operations**;
- the durable retry reschedule then published an **empty** task payload; Celery
  5.6.3 raised `TypeError`, classified ambiguous → job `dispatch_unknown`
  (attempt 2/3);
- one parse temp file (referenced by the job's `input_metadata.temp_path`) was
  retained with no terminal reaper.

Containment: the connector was paused (checkpoint `7666778550d9…` unchanged);
the incident job/version/temp file remain quarantined and unmodified.

## 2. Root causes

**A — replacement point-id collision (proven).** `ingestion/chunker.py` derived
chunk point ids from `canonical_doc_id + ordinal` only. A changed-content
replacement of the same document therefore regenerated the **same** point ids as
the prior version, violating the tenant-wide unique constraint
`knowledge.version_chunks (tenant_id, point_id)` (`uq_version_chunks_point`).
Isolated reproduction with the real chunker + real knowledge pipeline + real
PostgreSQL captured `UniqueViolation`, SQLSTATE `23505`, constraint
`uq_version_chunks_point`, at `record_intent`.

**B — retry payload + publication classification (proven).** `JobsService.
reschedule_due` built the retry `DeliveryEnvelope` with `payload={}` (unlike
`republish_dispatch`, which uses `task_payload_from_metadata`). Celery 5.6.3
validates task arguments at `apply_async`, so the empty payload raised a
deterministic pre-broker `TypeError`; `classify_publish_exception` mapped the
unmapped `TypeError` to AMBIGUOUS → `dispatch_unknown`.

**C — parse-temp ownership gap (identified).** `routers/v2_documents.py`
retained the staged temp file on every non-success (for a hoped-for retry) with
no terminal-path reaper, so a job that could not terminalize (B) leaked the
file. The Spec 030 `UploadTempFile` primitive already provides safe cleanup but
was invoked only on success.

## 3. Corrective change (accepted-contract conformance)

- **A:** `ParsedDocument.chunk_id_seed` (Spec 028 §3.5 persisted version
  property) is set by the route from the durable knowledge context and consumed
  by the chunker via `derive_point_id`, so replacement point ids are per-version
  distinct and deterministic; legacy callers without a seed are unchanged.
- **B:** `reschedule_due` reconstructs the accepted payload through
  `task_payload_from_metadata(job)`; `TypeError` is a bounded
  definite-pre-publication-rejection class (Spec 025 publication semantics),
  never ambiguous. Broker uncertainty (unmapped exceptions) remains AMBIGUOUS.
- **C:** terminal outcomes (success, terminal failure/retry exhaustion,
  cancellation, and reconciliation terminal closure R3/R9) release the staged
  temp file through the shared Spec 030 primitive (idempotent, missing-safe,
  staging-root-confined, symlink-safe). Retry generations, pending and
  `manual_review`/`dispatch_unknown` quarantine retain it.

## 4. Validation evidence

- Deterministic tests: `tests/test_mediawiki_changed_content.py` — 8 tests
  (A: distinct point ids + legacy fallback; B: payload reconstruction via the
  real retry lifecycle, TypeError definite rejection, RuntimeError ambiguous;
  C: idempotent/missing-safe release, out-of-root and symlink refusal).
- Focused suites (knowledge pipeline/domain/integration, jobs dispatch/domain,
  chunker) pass.
- Full Core suite parity vs the pristine `c9acf281…` baseline: identical failure
  set (25 pre-existing environment failures), **zero new failures**; the new
  tests pass.

## 5. Governance disposition

- No migration; no schema or public-API change.
- No new reconciliation selector/policy (R2/R9 remain as accepted; the live
  incident must be resolved by a separately authorized deployment step).
- No Spec 031 cleanup boundary change; no Qdrant deletion.

## 6. Outstanding

- Real HTTP/MediaWiki → Celery worker end-to-end matrix and the API/worker/Redis
  restart gates were not executed in this task; deterministic + integration +
  full-suite evidence substitutes partially.
- Live deployment and the quarantined-incident recovery remain pending a
  separate authorization (see the external deployment prompt).
