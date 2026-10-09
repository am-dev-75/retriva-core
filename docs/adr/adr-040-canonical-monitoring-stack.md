# ADR-040 (ACCEPTED) — Canonical monitoring-stack ownership and platform baseline

Status: ACCEPTED (owner decisions recorded in the task brief of 2026-10-09).
Date: 2026-10-09. Core baseline
`a890f74eaa0364e9bec74be84736c568741dcab5`. Deployment baseline
`1c432ebd7427e320de7234c5bb6fe888c7554141`. Companion spec: Spec 035.

## Context

Spec 034 / ADR-039 hardened the project Redis instance and closed its live
deployment with the classification `OPEN_MONITORING_GAP`: six accepted alert
classes were validated as Prometheus rules but no canonical monitoring
platform, repository, or owner existed. Monitoring was previously listed in
`plan/retriva.md` as undefined work. A production security control whose
detection layer is a handoff is incomplete; a canonical owner and platform
must be established before the closure item can end.

## Decision

1. **Ownership.** The canonical production monitoring-stack implementation and
   deployment ownership for the local containerized Retriva installation
   belongs to `retriva-local-containerized-deployment`. That repository owns
   the monitoring Compose profile/topology, Prometheus-compatible metrics
   collection, alert-rule loading, Alertmanager-compatible routing
   configuration interfaces, the read-only Redis exporter, the PostgreSQL
   non-terminal gauge integration, monitoring health checks, operational
   runbooks, and deployment/rollback procedures. Retriva Core owns only
   cross-cutting governance, Specs/ADRs, application-native metrics, and any
   genuinely non-collectable Core metric contract. Deployment-owned monitoring
   configuration is not duplicated in Core.
2. **Platform baseline.** Prometheus-compatible collection and rule
   evaluation with Alertmanager-compatible routing, pinned to
   `prom/prometheus:v3.15.0` and `prom/alertmanager:v0.34.1` (image digests
   recorded by the implementation), private-network only, persistent local
   storage with bounded retention (30 days), health/readiness checks, and a
   safe local default routing sink. No dashboard product is added.
3. **Read-only boundary.** Redis access uses only the accepted
   `rtrv-monitor` identity; PostgreSQL access uses a dedicated least-privilege
   read-only role with statement-timeout-bounded aggregate queries. No
   payloads, identifiers, command arguments, or credentials may enter
   metrics, labels, annotations, logs, or fixtures. No Redis mutation and no
   administrative capability.
4. **Closure scope.** OA-1…OA-4 of `spec034-open-actions.json`: designate and
   instantiate the owner/platform; implement the read-only Redis exporter and
   the PostgreSQL gauge; commit, load, and validate the nine alert rules plus
   self-monitoring; add the referenced runbook sections.
5. **Production boundary.** Implementation, commits, and isolated validation
   only. Live deployment, credential creation, reload, and service restarts
   are governed by a separate live deployment prompt; `OPEN_MONITORING_GAP`
   closes only when all six classes are active and evaluating in the canonical
   stack.

## Alternatives considered

- **External/managed monitoring platform**: no platform exists or is owned;
  rejected as the primary path because ownership would remain undefined.
- **Core-owned exporters emitting monitoring metrics from application code**:
  rejected; it couples the application to monitoring, expands the application
  security surface, and contradicts the read-only external-collector model.
- **Dashboard-first (Grafana)**: rejected; visualization does not close the
  detection gap and adds a product without an owner.
- **Alerting without Alertmanager-compatible routing**: rejected; routing
  ownership and verification are part of the accepted contract.

## Consequences

- The deployment repository gains a monitoring profile that must be validated
  in isolation before live activation; the live prompt is mandatory.
- Redis and PostgreSQL monitoring identities are least-privilege and
  read-only; failures are explicit and self-monitored rather than silent.
- The nine validated rules are committed unchanged where possible; any
  metric-contract-driven change requires an updated synthetic test.
- `OPEN_MONITORING_GAP` remains open until live activation under the prompt;
  this ADR does not itself close it.
