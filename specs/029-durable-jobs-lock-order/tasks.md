# Spec 029 — Tasks

Status: PROPOSED. Governing: ADR-034. Checked items are complete; unchecked
items require `ACCEPTED` status first.

## T0 — Discovery and governance
- [x] Repository baselines and clean-tree verification.
- [x] Live authoritative-state verification (read-only).
- [x] Complete two-relation lock/transaction inventory.
- [x] Root-cause analysis (J→A vs A→J cycle).
- [x] Pristine-baseline reproduction (deterministic + concurrent).
- [x] Spec 029 pack + ADR-034 presented PROPOSED; registry entries.
- [x] External report / evidence / lock-inventory written.
- [x] Owner acceptance of Spec 029 and ADR-034 (2026-10-06) — GATE PASSED.

## T1 — Implementation
- [x] Added `_lock_job_row` / `_lock_attempt_row`.
- [x] Normalized the eight inverse paths to J→A.
- [x] Added bounded `_retry_on_deadlock` + low-cardinality metrics.
- [x] Static assertion: no A→J path remains.

## T2 — Deterministic concurrency tests
- [x] Implemented the race matrix on real PostgreSQL
      (`tests/test_jobs_lock_order.py`).
- [x] Final-consistency assertions per race.

## T3 — Stress validation
- [x] Bounded multi-worker/multi-tenant stress; restart + reconnect cases.
- [x] Zero known-cycle deadlocks; bounded retries; no stuck/duplicate.

## T4 — Regression and gates
- [x] Durable-jobs + API + Celery + local fallback suites.
- [x] Spec 025/026/027/028 compatibility.
- [x] Governance registry test; OpenAPI (no change).
- [x] Security/observability review.

## T5 — Commit and deployment prompt
- [x] One local Core commit (path-based staging).
- [x] Deployment prompt produced; no push/tag/PR/release/deploy.