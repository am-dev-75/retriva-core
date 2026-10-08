# Spec 034 (ACCEPTED) — Redis destructive-command hardening

Status: ACCEPTED (owner accepted the recommended control set on 2026-10-08; see ADR-039). Implementation and deployment are tracked separately; nothing is deployed by this document.
Date: 2026-10-08. Core baseline: 61bb777e4a15a74e1ba528163a666bdc7fcf9aaf.
Governing: `retriva-core/.agent/rules/retriva-constitution.md` (v1.2, sections
42/43/44), Spec 032, ADR-037, and the recorded FLUSHALL deviation
(defect evidence: `redis-flushall-post-incident-*`, disposition A,
`CLOSE_RECOVERY_WITH_RECORDED_DEVIATION`).

## 1. Problem

During the completed Spec 033 MediaWiki recovery, one unauthorized live
`FLUSHALL` was executed against the project Redis instance. The audit proved
the deleted obligations stale/reconstructible with no unresolved impact, but
also proved that any client on the deployment network (and any host able to
reach the published port) can execute destructive administrative commands:
`requirepass` is unset, the only ACL user is `default on nopass ~* &* +@all`,
no TLS is configured, and port 6379 is published on all host interfaces.
Preventive hardening is required before the deviation can be considered
fully mitigated.

## 2. Scope

In scope: the single Redis 7.4.10 standalone instance used by compose project
`cust_0007` for the Celery/Kombu broker (db0) and Celery result backend (db1);
its identities, ACL model, network exposure, health checks, monitoring,
secret provisioning/rotation, safe reconnect procedure, and isolated
validation.

Out of scope: the completed MediaWiki incident (closed, no action); any
restore/replay of deleted Redis state (rejected); changes to the Celery
visibility timeout, R7 thresholds, task limits, or the queue topology
(Spec 032/ADR-037 remain binding); PostgreSQL, Qdrant, connector state.

## 3. Threat and deviation statement

T-1 destructive command execution by any co-located client or network peer
(realized once: `FLUSHALL`). T-2 accidental destructive execution through
runbooks, prompts, or tooling. T-3 unnecessary blast radius from broker,
result-backend, and any future application state sharing one instance.
T-4 silent loss of a first-time credential at restart because this Redis
instance currently runs with **no configuration file and no ACL persistence**.
T-5 stale-state replay if anyone restores broker/result data during an
incident.

## 4. Required decisions (owner)

1. ACL role model with destructive-command denial for all normal identities.
2. Emergency principal model (disabled by default, dual control).
3. Network exposure: stop publishing 6379 on all interfaces.
4. Result-backend retention/necessity (keep, shorten, or retire results).
5. Topology: retain single instance now, or split; criterion for later split.

## 5. Role and command model (summary)

Least-privilege users (deny-by-default command allowlists; credentials from
deployment environment/secrets; full matrix in `architecture.md` and in the
external role/command matrix artifact):

- `rtrv-broker` — ingestion API publisher + worker broker consumer (db0):
  queue/binding/unacked/pubsub commands plus `select`, `ping`,
  `client|setinfo`, `multi`/`exec`; keys `ingestion`, `_kombu.binding.*`,
  `unacked*`; fanout channels `/{db}.celeryev/*`, `/{db}.celery.pidbox/*`.
- `rtrv-results` — worker result backend (db1): string/expiry result commands
  and result pub/sub; keys `celery-task-meta-*`; channels
  `celery-task-meta-*`.
- `rtrv-monitor` — operator/monitoring read-only: `info`, `ping`, `dbsize`,
  `scan`, `type`, `ttl`, `llen`, `exists`, `client|list`, `slowlog|*`,
  `memory|usage`, `select`; no writes.
- `rtrv-health` — health check: `ping` only.
- `rtrv-emergency` — administrative break-glass (disabled by default).
- `default` — disabled after staged rollout.

Explicitly denied for normal roles (categories): `FLUSHALL`, `FLUSHDB`,
`SWAPDB`, `KEYS`, `CONFIG`, `ACL`, `SHUTDOWN`, `DEBUG`, `MONITOR`,
`REPLICAOF`/`SLAVEOF`, `FAILOVER`, `SAVE`/`BGSAVE`/`BGREWRITEAOF`,
`CLUSTER`, `MODULE`, `MIGRATE`, `RESTORE`, `DUMP`, `SCRIPT FLUSH|KILL`,
`FUNCTION`, `WAIT`, `CLIENT KILL|PAUSE|UNPAUSE|NO-EVICT`, `RESET`,
`LATENCY`, and everything else outside each role's allowlist.

Uncertainty (mandatory isolated tracing): `EVALSHA` (215 observed) and
`SCRIPT LOAD` (1 observed) have no identified in-deployment caller; they must
be attributed by the isolation tracing gate before they are either kept
(temporary compatibility allowance) or denied. `HELLO`/`AUTH` behavior is
version-dependent and must be validated with the deployed redis-py 6.4.0.

## 6. Emergency access

`rtrv-emergency`: `+@all ~* &*`, present in the ACL file only while enabled,
password held outside the deployment environment file (operator secret
store), use requires recorded owner approval, produces audit entries
(ACL LOG/monitor sink), and is disabled again immediately after use.

## 7. Secrets, rotation, persistence

New credentials per role, provisioned through the existing deployment
mechanism (`.env`/compose environment; `CELERY_BROKER_URL` /
`CELERY_RESULT_BACKEND` gain per-role credentials). ACL users must persist
across restarts: the Redis service gains a mounted configuration plus
`aclfile`; passwords are stored as Redis ACL SHA-256 hashes (`ACL SAVE`
format), never plaintext, and never printed. Rotation: set the new password
with `ACL SETUSER`, `ACL SAVE`, then roll services; rollback restores the
previous credential. No existing Redis secret is being rotated (none exists).

## 8. Topology decision

Retain the single instance for now (single-tenant, single-purpose broker and
result backend; ACL patterns isolate roles). **Split is deferred** with
criterion: split becomes required if any non-broker/non-result state
(caches, locks, sessions, dedup, rate limits) or a second independent
workload is introduced onto this instance. Result-backend retention review is
a prerequisite input to the Phase-2 decision.

## 9. Observability

Alert on `FLUSHALL`/`FLUSHDB` execution and denied destructive attempts (ACL
LOG), mass key deletion, queue/binding disappearance, and binding recreation
bursts; record key-count, queue-depth, unacked, and result-record baselines;
retain ACL LOG; review ACLs periodically.

## 10. Compatibility and rollback

Broker/result traffic must continue under Celery 5.6.3 / Kombu 5.6.2 /
redis-py 6.4.0 without behavior change. Staged rollout keeps `default` enabled
until new credentials are verified; rollback returns service URLs/health check
to the previous configuration and re-disables the new users. No queue loss,
no replay, no visibility-timeout change.

## 11. Isolated validation and deployment gates

Mandatory isolated matrix (full detail in the external validation-plan
artifact): publish/consume, late ack/unacked restoration, result
write/read/expiry, worker/API restart, no-flush reconnect, R7 and Spec 033
dry-runs, denied destructive commands per role, denied admin commands,
emergency role confined to the isolated environment, auth failure
fail-closed, rotation without task loss, rollback, and zero duplicate/stuck
effects. Deployment gates: owner acceptance of the ADR; registry entries
finalized; fresh recovery assets; staged identity rollout; post-change
observation; destructive-denial verification by a non-mutating method.

## 12. Incident response

On a denied destructive attempt or keyspace anomaly: keep the instance
available, capture ACL LOG/telemetry, treat as a security event, and follow
the recorded no-replay recovery policy; never restore stale broker state
without durable PostgreSQL reconciliation.

## 13. Separation from the completed recovery

This specification creates no obligation on the closed MediaWiki incident
(no reopen, no replay, no Redis restoration). It exists solely as the
preventive control set identified by the FLUSHALL deviation audit.

## 14. Acceptance criteria

See `acceptance.md`. Owner acceptance must be recorded explicitly before
implementation (Constitution §42/§43); this document remains PROPOSED.
