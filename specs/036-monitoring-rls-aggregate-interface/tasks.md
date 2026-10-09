# Spec 036 — Tasks (PROPOSED)

Status: ACCEPTED (2026-10-09). Tasks execute through the phase gates.

## T1 — Registry and governance

- T1.1 Registry entries 036 (spec) and 041 (ADR) recorded as `proposed`
  before first presentation.
- T1.2 Owner review; on acceptance update statuses via the same review class.

## T2 — Core bootstrap

- T2.1 Provision `retriva_monitor_owner` idempotently
  (`NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`).
- T2.2 Grant membership to `retriva_migrator` (ownership transfer only).
- T2.3 Refuse to manage an existing elevated role; emit summary without
  secrets; unit tests.

## T3 — Core migration V002

- T3.1 Add `V002__monitoring_aggregate_interface.up.sql` / `.down.sql`.
- T3.2 Register `retriva_monitor_owner` in the jobs provider
  `required_roles()`.
- T3.3 Migration-contract tests: name/version shape, checksum stability,
  idempotent re-run, upgrade path from V001, down removes only the interface,
  no table ownership/data change.

## T4 — Interface security tests

- T4.1 Cross-tenant aggregate equals synthetic ground truth.
- T4.2 Forced RLS still enabled and enforced; direct `SELECT` denied for the
  monitoring role.
- T4.3 `PUBLIC` and wrong roles cannot `EXECUTE`; monitoring cannot write,
  DDL, `SET ROLE`, or bypass RLS.
- T4.4 `search_path` fixed; references schema-qualified; result shape is one
  integer; statement timeout enforced at the session boundary.
- T4.5 Rollback removes access cleanly; application RLS/behavior tests
  unchanged.

## T5 — Deployment collector and contract

- T5.1 Query switch to `SELECT monitoring.nonterminal_job_count();`.
- T5.2 Grant template: `CONNECT` + `USAGE` + `EXECUTE` only; no table
  `SELECT`.
- T5.3 Metric contract, runbook, and deterministic tests updated (canonical
  topology tests retained).

## T6 — Isolated end-to-end validation

- T6.1 Canonical-topology harness with real jobs schema + forced RLS +
  multiple tenants.
- T6.2 Failure proof (pre-correction metric zero) retained.
- T6.3 Corrected path: reconciliation, A3 fire/resolve, security posture,
  lifecycle, rollback.
- T6.4 Regression runs; no new failures.

## T7 — Artifacts

- T7.1 Implementation report/evidence and updated commit ledgers.
- T7.2 Superseding live deployment prompt with migration and RLS gates.
