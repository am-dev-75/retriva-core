# Spec 035 — Acceptance (ACCEPTED)

Status: ACCEPTED (owner decisions 2026-10-09). Criteria A–C and D1 are
validated by this task; D2 (live deployment) is separately authorized via the
prompt and closes `OPEN_MONITORING_GAP` only after all six alert classes are
active and evaluating in the canonical stack.

## A. Governance

1. ADR-040 recorded and accepted; owner decisions cited.
2. Registry entries for Spec 035 and ADR-040 accepted and consistent
   (governance registry tests pass).
3. No conflicting canonical monitoring implementation exists elsewhere.

## B. Implementation (deployment repository)

1. Monitoring profile with pinned images, persistent storage, bounded
   retention, healthchecks, private-network-only exposure (no published host
   ports).
2. Read-only Redis exporter using only `rtrv-monitor`: all contract metrics
   present; explicit `*_up`, freshness, and auth-failure metrics; fail-closed
   behavior; no secret surfaces; no mutation commands in code (static test).
3. PostgreSQL collector: `retriva_pg_nonterminal_jobs` plus freshness; grant
   template documented; no identifiers exported.
4. Nine Redis rules committed, loaded read-only, preserving validated
   expressions; any change accompanied by an updated test and rationale.
5. Self-monitoring rules for scrape down, rule-evaluation failure,
   Alertmanager down, exporter auth failure, PG gauge stale, config reload
   failure, storage usage (where supported).
6. Runbook covers all alert classes and failure modes, the FLUSHALL/FLUSHDB
   prohibition, safe evidence collection, silencing, escalation roles, and
   rollback/removal.
7. Deterministic tests cover compose/config validity, pinned images, private
   exposure, retention/persistence settings, secret fail-closed behavior, no
   plaintext secrets, monitor-role-only Redis access, read-only SQL contract,
   aggregate-only metrics, bounded labels, metric names/types, rule syntax,
   all 15 synthetic alert cases, self-monitoring tests, healthy no-fire,
   firing, recovery, annotation redaction, runbook reference existence,
   Alertmanager route validity, health checks, and rollback compatibility.

## C. Isolated validation

1. Collection: required metrics present; values match source aggregates; no
   payload/secret leaks; Redis and PostgreSQL access read-only; exporter
   restart recovers; auth failure visible and fail-closed.
2. Rules: 15 synthetic tests pass in the pinned promtool; real scraped
   synthetic conditions trigger A1–A6 alerts; healthy/recovered conditions
   resolve; labels/severity/owner/runbook correct; no secret/identifier in
   alert payloads.
3. Routing: alerts reach the local sink; grouping/inhibition as designed;
   routing failure detected by self-monitoring; no external notification.
4. Lifecycle: monitoring restart preserves configuration and local history;
   reload succeeds; bad config fails before startup; retention enforced;
   credential rotation and rollback work in isolation.

## D. Live deployment readiness

1. Prompt exists with exact commits/digests, least-privilege credential
   creation through accepted interfaces, safe test-alert routing verification,
   no destructive probes, healthy no-fire period, observation, rollback, and
   the `CLOSED_SUCCESS` condition (all six classes active and evaluating).
2. Production unchanged by this task (read-only proofs recorded).

## Closure

`OPEN_MONITORING_GAP` closes only under D1/D2 execution; this task's completion
state is "implementation complete, live activation pending prompt execution",
recorded with the canonical classification of the task's closure section.
