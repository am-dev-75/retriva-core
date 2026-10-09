# Spec 036 — Acceptance (PROPOSED)

Status: PROPOSED. Every gate must pass after acceptance; SUCCESS is not
declared while any gate is open (Constitution §§37–40).

## A. Governance

- A1 ADR-041 `ACCEPTED`.
- A2 Registry entries for Spec 036 / ADR-041 present, status-consistent, no
  collisions.
- A3 No conflicting aggregate interface exists in any repository.

## B. Migration

- B1 Fresh install: bootstrap → V001 → V002 reaches the defined catalog
  state.
- B2 Upgrade: an installation at V001 applies V002 only; re-run is a no-op.
- B3 Down migration removes only the function and owner grants; no table,
  role, policy, or data change in either direction.
- B4 Ledger rows and sha256 checksums correct; transactional apply; no
  runtime DDL.

## C. Fidelity

- C1 With multiple synthetic tenants and non-terminal jobs, the interface
  returns the exact cross-tenant count.
- C2 The authoritative count and `retriva_pg_nonterminal_jobs` reconcile
  through the collector; stale/error paths preserve the last value with an
  explicit stale signal.
- C3 `RedisQueueDisappearance` fires on the synthetic queue-absence +
  durable-work condition and resolves after recovery (isolated validation).

## D. Security

- D1 `FORCE ROW LEVEL SECURITY` remains enabled and enforced on all three
  job tables.
- D2 Direct monitoring-role `SELECT` on `jobs.jobs` is denied.
- D3 `PUBLIC` cannot `EXECUTE`; only `retriva_monitor` is granted.
- D4 Monitoring cannot write, DDL, `SET ROLE`, or bypass RLS; no
  `BYPASSRLS`, superuser, ownership, replication, or role-management
  privilege exists in the design.
- D5 `search_path` is fixed inside the function; references are
  schema-qualified; shadowing attempts cannot change behavior.
- D6 Result shape is a single bounded numeric aggregate; no row, tenant,
  identifier, payload, error, or free-text data.
- D7 Statement timeout enforced; collector fails closed without leaking SQL
  or identifiers.
- D8 Rollback removes access cleanly; application RLS/behavior tests
  unchanged.

## E. Deployment and validation

- E1 Collector uses the interface only; runbook and metric contract match.
- E2 Canonical topology tests and all deterministic suites pass with zero
  new failures against pristine baselines.
- E3 Isolated end-to-end validation passes (collection, firing/resolution,
  routing, lifecycle, redaction, cardinality) on the canonical topology.
- E4 A superseding live deployment prompt pins the corrected commits and
  requires the migration, interface verification, and RLS gates.
- E5 Production remains unchanged by the implementation phases.

## F. Non-goals verification

- F1 Specs 025–035 semantics preserved; no application API, table, policy,
  role, or data change.
- F2 No Redis/Qdrant/connector change; no destructive command; no replay or
  reconciliation apply.
- F3 No secret appears in any committed artifact, test, metric, log, or
  fixture (Constitution §34).
