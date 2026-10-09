# Spec 035 — Tasks (ACCEPTED)

Status: ACCEPTED (owner decisions 2026-10-09). P0–P1 complete on acceptance;
P2–P4 proceed under this task; P5 (live deployment) is separately authorized.

## Governance

- T1 Owner decisions recorded (ownership, platform baseline, scope, boundary).
- T2 ADR-040 accepted; registry entries recorded for Spec 035 and ADR-040.
- T3 Governance integrity tests pass; one Core governance commit.

## Implementation (deployment repository)

- T4 Compose `monitoring` profile: pinned prometheus v3.15.0, alertmanager
  v0.34.1, redis-monitor-exporter, pg-monitor-exporter, alert-sink.
- T5 Read-only Redis exporter (stdlib; `rtrv-monitor`; fail-closed metrics).
- T6 PostgreSQL `retriva_pg_nonterminal_jobs` collector (read-only role
  template; statement timeout; atomic metrics file).
- T7 Nine Redis rules (validated translation) + self-monitoring rules.
- T8 Monitoring runbook with all required sections.
- T9 Deterministic tests including the 15 synthetic alert cases via the pinned
  promtool image and self-monitoring cases.
- T10 One deployment commit with explicit path staging.

## Validation (outside repositories)

- T11 Isolated end-to-end stack (synthetic Redis/PG, real scrape/routing).
- T12 Collection, read-only, firing/resolution, routing, lifecycle, rotation,
  rollback, and redaction proofs.

## Live readiness (outside repositories)

- T13 Live deployment prompt produced; production non-mutation verified.

## Done means

All acceptance criteria A–D of `acceptance.md` satisfied in this task except
live deployment (P5), which the prompt governs.
