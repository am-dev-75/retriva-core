# Spec 030 — Tasks

Status: PROPOSED. Governing: ADR-035.

## T0 — Discovery and governance
- [x] Baselines + live non-mutation state.
- [x] Root-cause localization.
- [x] Baseline reproduction (isolated PG, real methods).
- [x] Idempotency input inventory.
- [x] Canonical fingerprint contract + temp-file ownership model (design).
- [x] Spec 030 pack + ADR-035 presented PROPOSED; registry entries.
- [x] External discovery report/evidence written.
- [x] Owner acceptance of Spec 030 and ADR-035 (2026-10-07) — GATE PASSED.

## T1 — Implementation
- [x] `canonical_upload_identity` + versioned hash; wired into upload fingerprint.
- [x] `IdempotencyConflictError` → 409 mapping at the v2 boundary.
- [x] `UploadTempFile` ownership guard; worker parity; savepoint convergence.

## T2 — Deterministic tests
- [x] Canonicalization vectors; unit + real-PG integration.
- [x] Concurrency/race matrix; Spec 029 invariant preserved.

## T3 — Real isolated runtime
- [x] Route-level HTTP (accept/transfer, reuse/no-leak, conflict/409/no-leak).
- [x] Celery + Redis restart (isolated harness) and concurrency (0 deadlocks).

## T4 — Compatibility and security
- [x] Upload/document/MediaWiki/job-status/OpenAPI/CRM/Gateway/Web UI paths.
- [x] Tenant isolation, no leakage, no new DDL/privilege; out-of-root refusal.

## T5 — Commit and deployment prompt
- [x] One local Core commit; deployment prompt produced; no deploy.