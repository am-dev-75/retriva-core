# Spec 031 — Acceptance

Status: ACCEPTED (owner acceptance 2026-10-07). Implementation and isolated
validation authorized; live deployment/point deletion NOT authorized.

## A. Acceptance matrix

| # | Criterion | Gate |
|---|---|---|
| A1 | Fail-closed eligibility; current/staging never eligible; serving superseded blocks | tests |
| A2 | Durable intent/evidence via `qdrant_operations`; no new schema | review/tests |
| A3 | Mandatory zero-point postcondition; API acceptance insufficient | tests |
| A4 | Idempotent duplicate/dry-run/apply; ambiguous outcomes never blind-replayed | tests |
| A5 | Dry-run read-only, complete, operator-reviewable | tests |
| A6 | Reconciliation classifies; no auto-delete | tests |
| A7 | Tenancy/RLS/security preserved; no cross-tenant batch | review/tests |
| A8 | Isolated real PG+Qdrant lifecycle incl. interruption/crash/retry/restart/Redis reconnect | runtime harness |
| A9 | Spec 028/029 compatibility; frozen evidence unchanged; no live mutation in discovery | review |
| A10 | One local Core commit + separate deployment prompt after acceptance | repo audit |

## B. Evidence at proposal time (read-only / isolated)

- Live: 17 Qdrant points, 16 serving, 1 hidden superseded (`stale_superseded_points=1`);
  1 superseded native version with 1 verified chunk; all operations verified;
  frozen catalog/registry unchanged.
- Isolated real PG+Qdrant: reconcile `stale_superseded_points=1`; `Purger.run(apply=True)`
  eligible=0 purged=0; superseded point retained after purge; with the point set
  `serving=true` the authoritative visibility predicate returns visible.
- No source change, no live point mutation, no commit.

## C. Acceptance record

Owner accepted 2026-10-07 with D1=30 days, D2=global, D3=hybrid,
D4=retain adopted_uncertain, D5=none. Open item: concrete operational bounds
(max versions/points per batch, Qdrant wait bounds, rate limit, concurrency,
maintenance window) are not fixed by the pack and require an owner decision
before the apply path is implemented (implementation §16).

## D. Status

PROPOSED. Implementation, isolated validation, commit, and deployment prompt
remain pending acceptance.

## E. Implementation evidence (2026-10-07)

- B1-B7 recorded (spec §8.1); `tests/test_superseded_cleanup.py` = 18 passed
  (real PostgreSQL + FakeQdrant + injected clock/latency): eligibility
  (current/age/uncertain/serving/missing-manifest), B1 batching, B2 oversized
  version, B3 verify/reconciliation, B4 rate limit, B5 concurrency, B6 window
  (unconfigured/outside), B7 backlog+p95+missing-baseline, dry-run non-mutation,
  fingerprint change, operator authorization.
- Regression: `test_knowledge_*`, governance, constitution, lock-order = 84 passed.
- No migration; Spec 028/029/030 unchanged; live untouched.
