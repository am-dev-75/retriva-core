# Spec 036 — Plan (PROPOSED)

Status: PROPOSED. Implementation is not authorized until Spec 036 and ADR-041
are `ACCEPTED` (Constitution §§42–44).

## Phase 0 — Governance (this proposal)

1. Registry entries for Spec 036 and ADR-041 recorded before first
   presentation (`proposed`).
2. Pack and ADR presented for owner decision.
3. On acceptance: statuses updated through the same review class; no clause
   of Spec 035 / ADR-040 edited in place (amendment recorded here).

Gate: explicit owner acceptance. Until then no migration, collector, or
configuration change is committed.

## Phase 1 — Core migration and bootstrap (after acceptance)

1. Extend the Core bootstrap one-shot to provision `retriva_monitor_owner`
   idempotently (NOLOGIN, non-elevated, membership to `retriva_migrator`);
   refuse to manage an existing elevated role; no password path.
2. Add `V002__monitoring_aggregate_interface.{up,down}.sql` to
   `src/retriva/jobs/sql/` exactly as specified in `architecture.md` §2.2/2.3.
3. Add `retriva_monitor_owner` to the jobs provider `required_roles()`.
4. Add migration-contract tests (file pair, checksums, no destructive
   downgrade, fresh/upgrade paths) and interface security tests per
   `acceptance.md`.
5. One bounded Core commit: `fix(monitoring): expose RLS-safe job aggregates`.

Gate: Core test suite green (governance, migration contract, jobs, RLS);
no application behavior change.

## Phase 2 — Deployment collector and contract (after Phase 1)

1. Switch the collector query to the interface and replace the grant
   template (EXECUTE only); keep metric names, labels, freshness, timeout,
   and fail-closed behavior.
2. Update the metric contract, runbook (interface verification and rollback),
   and deterministic tests.
3. One bounded deployment commit.

Gate: deployment test suite green; canonical topology tests green.

## Phase 3 — Isolated end-to-end validation

Use the canonical-topology harness (defect-correction artifact) extended
with the interface migration:

1. failure proof with the pre-correction source (metric zero under forced
   RLS) already recorded;
2. corrected path: interface returns ground truth; collector metric
   reconciles; `RedisQueueDisappearance` fires on the synthetic condition and
   resolves;
3. security posture (direct access denied, RLS active, PUBLIC/wrong-role
   denied) verified live in the harness;
4. lifecycle: restart, reload, rollback.

Gate: all `acceptance.md` gates pass; zero new failures against pristine
baselines.

## Phase 4 — Live prompt update

1. Produce the superseding live deployment prompt variant that includes the
   migration step, interface verification, and RLS security gates.
2. No live deployment in this plan; `OPEN_MONITORING_GAP` closes only when
   the live prompt's gates pass under a separate owner authorization.

## Rollback

- Core: down migration removes only the function and owner grants; bootstrap
  role remains inert (or is dropped by a later governed step).
- Deployment: revert the collector/grants commit; the old direct-SELECT path
  may be restored only with the documented limitation, or the collector may
  be disabled (fail-closed) until the interface is restored.
