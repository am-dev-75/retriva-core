# ADR-037 (ACCEPTED) — Worker-loss redelivery bounds and R7 interaction

Status: ACCEPTED (owner accepted Option B on 2026-10-08; governance-only). Date: 2026-10-08. Core candidate
d07db21387f1528c4adfa9d46af89fa13757f8a7.

## Context
Abrupt worker loss (SIGKILL) with the Redis broker up did not produce prompt
redelivery: the message remained unacked pending Kombu `restore_visible` bounded
by `visibility_timeout` (default 3600 s). Celery 5.6.3 / kombu 5.6.2 / redis-py
6.4.0 / billiard 4.3.1; Celery soft/hard time limits disabled (H unbounded).

## Decision (proposed)
Retain the Kombu default `visibility_timeout` V=3600 s and R7 threshold R=1800 s
and rely on the governed T8 claim/takeover semantics plus execution_generation
fencing for safety, with R7 as the operator-invoked faster path. Bounding V
(Option A) requires first bounding per-task hard limits; the concrete values are
in Spec 032 (PROPOSED).

## Consequences
Worker-loss recovery is bounded but slow (~V+G ≈ 65 min automatic, or ~30 min +
operator via R7). No migration; no public API change. Any V reduction requires
per-task hard time limits and an accepted configuration surface.

## Status of prior evidence
Supersedes the prompt-redelivery assumption in
`DETERMINATION-orphaned-running-recovery.md`; consistent with
`abrupt-worker-loss-runtime-report.md`.

## Accepted decision (2026-10-08)

Owner accepted Option B with V=3600 s, R=1800 s, unbounded H, G=300 s,
restore-visible decimation ~10 s, recovery concurrency 1. R7 is the primary
bounded PostgreSQL recovery owner; eventual Redis transport
restoration of the original delivery is fenced by atomic claim, terminal-state
checks and execution_generation (idempotent no-op unless the single authorized
claimant). Restart-safe work may recover via R7 lost-and-redispatch;
non-restart-safe work becomes manual_review and is never replayed automatically.
No product source, configuration, migration, or deployment change is required or
authorized by this acceptance. Option A (configurable visibility timeout) remains
unaccepted future work requiring separately governed task hard limits. Option C
is not selected.
