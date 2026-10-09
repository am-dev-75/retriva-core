# Spec 035 — Architecture (ACCEPTED)

Status: ACCEPTED (owner decisions 2026-10-09). Companion to `spec.md`.

## 1. Topology (deployment repository, profile `monitoring`)

```text
              (cust_0007 project network, private)
  +-------------------+        scrape /metrics         +---------------------+
  | redis-monitor-    | <----------------------------- | prometheus          |
  | exporter          |                                | v3.15.0             |
  | (python:3.12-     |        scrape /metrics         | - rule files ro     |
  |  alpine, script)  |                                | - tsdb volume       |
  +---------+---------+                                | - retention 30d     |
            | read-only rtrv-monitor                   +----------+----------+
            v                                                     |
  +-------------------+                                alerts   | alertmanager API
  | redis 7.4.10      |                                +----------v----------+
  | (Spec 034 posture)|                                | alertmanager        |
  +-------------------+                                | v0.34.1             |
                                                       | - grouping/inhibit  |
  +-------------------+        scrape /metrics         | - default route:    |
  | pg-monitor-       | <----------------------------- |   local sink        |
  | exporter          |                                +----------+----------+
  | (postgres:16.15-  |                                           |
  |  alpine + psql +  |                                           v
  |  busybox httpd)   |                                +---------------------+
  +---------+---------+                                | alert-sink (local)  |
            | read-only retriva_monitor role           | logs delivered      |
            v                                          | alerts (no external |
  +-------------------+                                | notification)       |
  | postgres 16.15    |                                +---------------------+
  +-------------------+
```

- All services attach to the existing project network only; no host ports are
  published by the committed configuration.
- `prometheus` and `alertmanager` mount configuration and rule files
  read-only and use named volumes for persistent state.
- `alert-sink` is a minimal local receiver (python:3.12-alpine running a
  committed script) that records received alerts for validation/ops evidence;
  external receivers are configured by the operator through external secret
  references, never committed.

## 2. Failure and freshness semantics

- Every collector exposes `<collector>_up`, a last-success timestamp, and an
  auth-failure counter. Scrape failure is explicit; absence of a metric is
  never interpreted as health.
- The Redis exporter treats ACL LOG counters carefully: it polls the bounded
  ACL LOG and accumulates monotonic per-(role class, category) counters,
  tolerating ring-buffer aging (decreases never decrement the counter).
- The PostgreSQL collector writes its metrics file atomically after a
  statement-timeout-bounded query; a stale timestamp raises
  `MonitoringPostgresGaugeStale`.
- Prometheus self-scrape provides `prometheus_config_last_reload_successful`
  and rule-evaluation counters used by self-monitoring.

## 3. Read-only access model

- Redis: `rtrv-monitor` (accepted allowlist) only, via the isolated template
  in validation and via the deployment `.env` reference in production. The
  exporter holds no broker/results/emergency credentials.
- PostgreSQL: dedicated `retriva_monitor` role with `CONNECT`, `USAGE` on the
  `jobs` schema, and `SELECT` on `jobs.jobs` only (documented grant template;
  creation is a live-deployment step).
- The exporter never writes; the Prometheus/Alertmanager volumes are private
  to the monitoring stack.

## 4. Metric contract (normative summary)

See `config/monitoring/metric-contract.md` for the full contract. Summary:

| Metric | Type | Labels | Source |
|---|---|---|---|
| `redis_monitor_exporter_up` | gauge | – | exporter loop |
| `redis_up` | gauge | – | PING (rtrv-monitor) |
| `redis_db_keys` | gauge | db | INFO keyspace |
| `redis_key_exists` | gauge | key (bounded: ingestion) | EXISTS |
| `redis_queue_depth` | gauge | queue (bounded: ingestion) | LLEN |
| `redis_unacked_present` / `redis_unacked_index_present` | gauge | – | EXISTS (counts unavailable under the accepted monitor scope; documented) |
| `redis_binding_sets` / `redis_binding_recreation_events_total` | gauge/counter | – | SCAN `_kombu.binding.*` + membership churn |
| `redis_result_records_total` | gauge | – | SCAN `celery-task-meta-*` count |
| `redis_acl_denied_commands_total` | counter | role, category | ACL LOG |
| `redis_auth_failures_total` | counter | – | ACL LOG reason=auth |
| `redis_acl_emergency_use_total` | counter | – | ACL LOG username=rtrv-emergency (reason=command) |
| `redis_acl_default_auth_success_total` | counter | – | `ACL DRYRUN default PING` transition |
| `redis_acl_default_usable` | gauge | – | same probe |
| `redis_rdb_last_bgsave_status` | gauge | – | INFO persistence |
| `redis_monitor_exporter_auth_failures_total` | counter | – | exporter-side |
| `retriva_pg_nonterminal_jobs` | gauge | – | `SELECT count(*) … NOT IN (terminal)` |
| `retriva_pg_monitor_last_success_timestamp_seconds` | gauge | – | collector |

Healthy baselines are recorded in the metric contract. No command arguments,
key names beyond the bounded classes, payloads, or identifiers are exported.

## 5. Security boundary

- Monitoring is read-only and unprivileged; it cannot administer Redis,
  PostgreSQL, or applications.
- Credentials are delivered exclusively through the accepted environment/
  secret interface; startup fails closed when required values are absent; no
  secret appears in argv, labels, metrics, rules, annotations, logs, or
  fixtures.
- Private-network only; no all-interface publication.

## 6. Retention, persistence, recovery

- Prometheus TSDB volume with `--storage.tsdb.retention.time=30d`
  (configurable through the committed command; documented).
- Alertmanager storage volume for silences/notifications.
- Restart preserves data; recovery procedure in the runbook; rollback removes
  the monitoring profile without touching application or Redis state.

## 7. Platform health / self-monitoring

`MonitoringCollectorDown`, `MonitoringRuleEvaluationFailures`,
`MonitoringAlertmanagerDown`, `MonitoringRedisExporterAuthFailures`,
`MonitoringPostgresGaugeStale`, `MonitoringConfigReloadFailed`, and
`MonitoringStorageHighUsage` (when the metric is available). Detection uses
Prometheus self-metrics to avoid circular blind spots.

## 8. Validation architecture

- Deterministic repository tests (pytest) cover configuration, pinning,
  exposure, secrets, contracts, label bounds, rule syntax and the synthetic
  alert cases via pinned `promtool`.
- An isolated end-to-end harness (outside repositories) runs the committed
  monitoring configuration against synthetic Redis (Spec 034 posture) and
  synthetic PostgreSQL, exercising collection, firing, routing, lifecycle,
  rotation, and rollback.
