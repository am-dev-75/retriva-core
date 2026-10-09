# Spec 035 — Plan (ACCEPTED)

Status: ACCEPTED (owner decisions 2026-10-09). Implementation, commits, and
isolated validation may proceed; live deployment remains separately authorized.

## P0 — Owner decision (complete, 2026-10-09)

Owner decisions recorded in the task brief: canonical ownership
(`retriva-local-containerized-deployment`), platform baseline (Prometheus-
compatible + Alertmanager-compatible, pinned versions), closure scope OA-1…OA-4,
production boundary (no live change in this phase).

## P1 — Governance (this task)

- T1 Write this pack and ADR-040 as ACCEPTED citing the owner decisions.
- T2 Record registry entries (Spec 035, ADR-040) consistent with §43.
- T3 Run governance integrity tests; commit Core governance only.

## P2 — Implementation in the deployment repository (this task)

- T4 Monitoring Compose profile: prometheus v3.15.0, alertmanager v0.34.1,
  redis-monitor-exporter, pg-monitor-exporter, alert-sink; private networking;
  named volumes; healthchecks.
- T5 Redis read-only exporter implementing the metric contract (stdlib only,
  `rtrv-monitor`), fail-closed with explicit failure metrics.
- T6 PostgreSQL aggregate collector with least-privilege grant template and
  statement timeout.
- T7 Commit the nine validated Redis rules unchanged where possible; add the
  self-monitoring rule group.
- T8 Runbooks for all alert classes and failure modes.
- T9 Deterministic tests (config, pinning, exposure, secrets, contracts,
  labels, rule syntax, 15 synthetic cases via pinned promtool, self-monitoring,
  runbook references, route validity, rollback compatibility).
- T10 Commit deployment implementation (one commit).

## P3 — Isolated end-to-end validation (this task, outside repositories)

- T11 Disposable stack: hardened synthetic Redis, synthetic PostgreSQL,
  committed monitoring config, alert sink; synthetic Celery traffic.
- T12 Prove collection/read-only/no-leak; real firing/resolution for the nine
  rules; routing/grouping/inhibition; self-monitoring; lifecycle (restart,
  reload, bad-config rejection, retention), rotation, rollback.

## P4 — Live deployment prompt (this task, outside repositories)

- T13 Produce `retriva-monitoring-stack-live-deployment-prompt.txt` with exact
  commits/digests, credential creation, safe test-alert verification, no-fire
  period, activation of all rules, observation, rollback, and the
  `CLOSED_SUCCESS` condition.

## P5 — Live deployment (separately authorized; not this task)

Executed by an operator under the P4 prompt; production remains unchanged here.
