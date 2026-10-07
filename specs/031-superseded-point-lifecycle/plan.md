# Spec 031 — Plan

Status: PROPOSED. Governing: ADR-036.

## Phase 0 — Discovery and governance (COMPLETE)
- Repository + live baseline; governance determination.
- Read-only live inventory (1 hidden superseded point; 16 serving).
- Lifecycle/purge reconstruction; isolated real PG+Qdrant reproduction.
- Spec 031 + ADR-036 presented PROPOSED; registry entries; external reports.

## Phase 1 — Owner decisions and acceptance (GATE)
- Decide D1 retention window (7/30/90 days), D2 scope, D3 execution model,
  D4 uncertain-provenance policy, D5 migration (expected none).
- Owner accepts Spec 031 + ADR-036. No implementation before `ACCEPTED`.

## Phase 2 — Implementation (post-acceptance)
- Read-only candidate selector + eligibility predicate (fail-closed).
- Dry-run report; apply workflow with `qdrant_operations` intent/evidence.
- Explicit-id deletion + mandatory zero-point postcondition + ambiguity handling.
- Retry/idempotency/operator abort; low-cardinality observability.
- Reconciliation classification incl. `superseded_but_serving`.

## Phase 3 — Isolated validation (post-acceptance)
- Real PostgreSQL + Qdrant lifecycle, interruption/crash/retry/duplicate/restart/
  Redis reconnect; current/staging never eligible; cross-tenant isolation.

## Phase 4 — Commit + separate deployment prompt (post-acceptance)
- Exactly one local Core commit; parameterized deployment prompt (not executed);
  live cleanup requires separate authorization.

## Out of scope
Upload-staging cleanup (separate governance); Spec 028/029 architecture changes;
re-embedding; live deletion during discovery.