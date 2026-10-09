# Spec 035 (ACCEPTED) — Canonical monitoring stack for Redis security alerting

Status: ACCEPTED (owner decisions recorded in the task brief of 2026-10-09:
canonical ownership belongs to `retriva-local-containerized-deployment`;
Prometheus-compatible collection with Alertmanager-compatible routing; no
dashboard product; closure scope OA-1…OA-4; production boundary excludes live
deployment).
Date: 2026-10-09. Core baseline: `a890f74eaa0364e9bec74be84736c568741dcab5`.
Deployment baseline: `1c432ebd7427e320de7234c5bb6fe888c7554141`.
Governing: `retriva-core/.agent/rules/retriva-constitution.md` (v1.2,
sections 34, 37–40, 42–44), Spec 034, ADR-039, ADR-040, and the
Spec 034 closure classification `OPEN_MONITORING_GAP`.

## 1. Problem

Spec 034 / ADR-039 hardened the project Redis instance (deny-by-default ACLs,
default user off, loopback/private publication) and its deployment closed with
`OPEN_MONITORING_GAP`: the six accepted alert classes (A1…A6) were validated as
Prometheus rules but no canonical monitoring platform, repository, or owner
existed to activate them. The detection layer of a production security control
must not remain a handoff.

## 2. Scope

In scope:

- canonical ownership and topology of the local monitoring stack;
- Prometheus-compatible metrics collection, rule evaluation, persistent local
  storage, and health/readiness;
- Alertmanager-compatible routing interface with a safe local default route;
- a read-only Redis exporter using only the accepted `rtrv-monitor` identity;
- a read-only PostgreSQL collector exposing `retriva_pg_nonterminal_jobs`;
- the nine validated Redis rules (classes A1…A6 plus the accepted persistence
  rule) and monitoring self-monitoring rules;
- operational runbooks for every alert and failure mode;
- deterministic tests and isolated end-to-end validation;
- a separate live deployment prompt.

Out of scope: dashboards/visualization (deferred), application changes, Redis
ACL changes, live deployment (separately authorized), any mutation of
production state.

## 3. Decisions (owner, 2026-10-09)

1. Ownership: `retriva-local-containerized-deployment` owns the monitoring
   Compose profile, collectors, rule loading, routing configuration
   interfaces, exporters, the PostgreSQL gauge integration, health checks,
   runbooks, and deployment/rollback procedures. Core owns only governance,
   Specs/ADRs, and application-native metrics; no deployment-owned monitoring
   configuration is duplicated in Core.
2. Platform baseline: Prometheus-compatible collector/evaluator plus
   Alertmanager-compatible routing, pinned to supportable container versions,
   recorded here: `prom/prometheus:v3.15.0` and `prom/alertmanager:v0.34.1`
   (digests recorded in the deployment implementation). No dashboard product is
   added by this spec.
3. Closure scope: OA-1…OA-4 of `spec034-open-actions.json`.
4. Production boundary: no live deployment, reload, credential, or service
   change is performed by the implementation phase; a separate prompt governs
   live activation.

## 4. Required platform properties

- metrics collection, rule evaluation, alert routing interface,
  health/readiness, retention/persistence policy, runbooks (minimum platform);
- private-network-only by default; no all-interface host publication;
- rule files mounted read-only; configuration validated before startup;
- restart-safe persistent storage with bounded retention;
- alert routing must not require any committed notification secret; receivers
  use external secret references; a safe local default route exists for
  isolated validation;
- grouping/inhibition configured to avoid alert storms;
- no dashboard dependency.

## 5. Metric and alert contract

The normative contract is the committed metric contract
(`deployment/config/monitoring/metric-contract.md` and the external
`monitoring-metric-contract.json`). At minimum: Redis availability, auth
success/failure aggregates, ACL denied-command aggregates by bounded
reason/category and role class, default/emergency identity use, key counts by
logical DB, queue depth, unacked presence, binding churn, result-record count
and rate, persistence indicator, PostgreSQL non-terminal durable-job count,
and monitoring self-health.

Forbidden in metrics, labels, annotations, logs, or fixtures: credentials,
password hashes, ACL material, userinfo URLs, command arguments, arbitrary key
names, result payloads, tenant/job/task/document identifiers, content.

Label/cardinality limits: role class ≤ 4, category ≤ 3, key class ≤ 2, db ≤ 2,
severity ≤ 3, owner ≤ 1. No unbounded labels.

## 6. Read-only security boundary

- Redis access: `rtrv-monitor` only; no broker/results credential reuse; no
  emergency/default identity; no payload retrieval; no mutation; no `EVAL`;
  no keyspace notifications (which would require `CONFIG SET`).
- PostgreSQL access: a dedicated least-privilege read-only role; bounded
  indexed aggregate queries with statement timeout; counts only.
- Both collectors fail closed: authentication/connection failure is surfaced as
  an explicit metric and an alert, never as silent absence.
- The monitoring stack is separate from Redis/application administration: it
  gains no administrative capability and holds no privileged credentials.

## 7. Acceptance criteria

A. Governance
1. ADR-040 accepted (canonical ownership + platform baseline).
2. Registry entries for Spec 035 and ADR-040 recorded and consistent.
3. No conflicting canonical monitoring implementation exists in another
   repository.

B. Implementation (deployment repository)
1. Monitoring Compose profile with pinned images, persistent storage,
   bounded retention, health checks, and private-network-only exposure.
2. Read-only Redis exporter covering the metric contract, using only
   `rtrv-monitor`, with explicit failure metrics and no secret surfaces.
3. PostgreSQL gauge collector (`retriva_pg_nonterminal_jobs` + freshness)
   with a documented least-privilege SQL grant template (interface only; no
   live PostgreSQL change).
4. The nine validated Redis rules committed and loaded from a read-only
   mount, preserving the validated expressions unless a documented metric
   change requires an updated expression and test.
5. Monitoring self-monitoring rules (scrape down, rule-evaluation failure,
   Alertmanager unavailable, exporter auth failure, PG gauge stale, reload
   failure, storage capacity where supported).
6. Runbooks for every alert class and failure mode, including the FLUSHALL/
   FLUSHDB live-test prohibition, safe evidence collection, silencing,
   escalation roles, and rollback/removal.
7. Deterministic committed tests for compose/config, pinning, exposure,
   secrets fail-closed, read-only contracts, label bounds, rule syntax, the 15
   synthetic alert cases, self-monitoring, runbook references, Alertmanager
   route validity, and rollback compatibility.

C. Isolated validation (disposable stack)
1. All required metrics collected from synthetic Redis (Spec 034 posture,
   synthetic credentials) and synthetic PostgreSQL; values match source
   aggregates; no payload/secret leaks; read-only access proven.
2. Real scraped conditions trigger the intended alerts; healthy and recovered
   conditions resolve; labels/severity/owner/runbook correct.
3. Alerts reach the local validation sink; grouping/inhibition behaves;
   routing failure is detected by self-monitoring; no external notification.
4. Lifecycle: restart preserves configuration and local history; rule reload
   succeeds; bad configuration fails before startup; retention enforced;
   credential rotation and rollback proven in isolation.

D. Live deployment readiness
1. Separate prompt exists with exact commits/image digests, least-privilege
   credential creation, safe test-alert routing verification, healthy
   no-fire period, final `CLOSED_SUCCESS` only after all six classes are
   active and evaluating, and rollback.
2. Production remains unchanged by the implementation phase.

## 8. Failure semantics

Collector unreachable/unauthorized: `*_up=0` and explicit auth-failure counters;
the corresponding self-monitoring alert fires; security rules that depend on
the missing signal are covered by self-monitoring (signal-gap alarm) rather
than silently passing.

Prometheus unavailable: Alertmanager routing receives no new alerts; the
`MonitoringCollectorDown` / `MonitoringRuleEvaluationFailures` alerts cover
detection; runbook directs to the monitoring-stack recovery procedure.

## 9. Compatibility

No application image/behavior change; no Redis ACL/command-scope change; no
PostgreSQL schema change; no Qdrant or connector dependency; no change to
Specs 025–034 semantics; no destructive probes; no replay or reconciliation
apply.
