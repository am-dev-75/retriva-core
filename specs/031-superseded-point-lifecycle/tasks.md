# Spec 031 — Tasks

Status: PROPOSED. Governing: ADR-036.

## T0 — Discovery and governance
- [x] Repository + live baseline.
- [x] Read-only inventory of superseded points.
- [x] Lifecycle/purge reconstruction.
- [x] Isolated real PG+Qdrant reproduction of the gap.
- [x] PROPOSED Spec 031 + ADR-036 + registry entries.
- [x] External report/evidence/inventory/policy-options + deferred prompt.
- [x] Owner decisions D1–D5 and acceptance of Spec 031 + ADR-036 (2026-10-07) — GATE PASSED.
- [ ] Owner decision: concrete operational bounds (§16) — OPEN.

## T1 — Implementation (post-acceptance)
- [ ] Candidate selector + fail-closed eligibility.
- [ ] Dry-run report.
- [ ] Apply workflow with durable `qdrant_operations` intent/evidence.
- [ ] Explicit-id deletion + zero-point postcondition + ambiguity handling.
- [ ] Idempotency/retry/abort + observability.

## T2 — Reconciliation integration (post-acceptance)
- [ ] Classification incl. `superseded_but_serving`; no auto-delete.

## T3 — Isolated validation (post-acceptance)
- [ ] Real PG+Qdrant matrix (spec §5/§19); tenancy/security; Spec 028/029.

## T4 — Commit + deployment prompt (post-acceptance)
- [ ] One local Core commit; separate deployment prompt; no live cleanup.