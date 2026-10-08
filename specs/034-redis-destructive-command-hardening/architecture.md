# Spec 034 — Architecture (PROPOSED)

Status: PROPOSED. Companion to `spec.md`. All facts below are derived from
read-only inspection of the deployed stack on 2026-10-08; nothing is
implemented by this document.

## 1. Current topology (verified)

- Redis 7.4.10, standalone master, no replicas, no Sentinel, no cluster,
  `databases = 16`, no TLS (`tls-port 0`), `protected-mode no`.
- `requirepass` unset; the only ACL user is
  `default on nopass sanitize-payload ~* &* +@all`.
- Runs with no configuration file (image default command); RDB persistence
  policy `3600 1 300 100 60 10000`; AOF disabled; no ACL persistence today.
- Host port mapping `0.0.0.0:6379 -> 6379` (and IPv6), i.e. published on all
  interfaces.
- Consumers (source- and env-verified):
  - `retriva-ingestion` — Celery publisher, db0 (`redis://redis:6379/0`).
  - `retriva-worker` — Celery consumer + result backend, db0/db1
    (`/0`, `/1`).
  - `retriva-core`, `retriva-gateway`, `retriva-webui`,
    `retriva-email-agent-connector`, `retriva-messaging`,
    `retriva-mediawiki-connector`, `retriva-searxng` — no Redis usage in
    source; they inherit `CELERY_*` values from the deployment environment
    file only (verified: no imports/usages; SearXNG limiter disabled).
  - Health check: compose `redis-cli ping` (unauthenticated today).
  - Operator/audit tooling: read-only `redis-cli` inspection via
    `docker exec`.
  - Dormant capability: graph event bus `_RedisAdapter`
    (`retriva:graph:events` pub/sub) is never initialized in deployed code;
    if ever activated it requires `+publish` on that channel only.
  - Unattributed: `EVALSHA` 215 / `SCRIPT LOAD` 1 observed with no identified
    in-deployment caller (possible manual/external client via the published
    port). Must be attributed in the isolation tracing gate.

## 2. Identities and ACL design

Deny-by-default command allowlists; per-role credentials; key and channel
patterns matched to the observed Kombu 5.6.2 / Celery 5.6.3 layout:

| role | purpose | allowlisted commands | key patterns | channels |
|---|---|---|---|---|
| `rtrv-broker` | publisher + consumer (db0) | `select ping client\|setinfo multi exec lpush brpop publish subscribe psubscribe unsubscribe sadd srem smembers zadd zrem zrevrangebyscore zrangebyscore zcount hget hset hdel del evalsha eval script\|load` (last three temporary pending tracing) | `ingestion`, `_kombu.binding.*`, `unacked`, `unacked_index`, `unacked_mutex` | `/{db}.celeryev/*`, `/{db}.celery.pidbox/*` |
| `rtrv-results` | result backend (db1) | `select ping client\|setinfo multi exec get mget set setex delete expire ttl incr publish subscribe unsubscribe` | `celery-task-meta-*` | `celery-task-meta-*` |
| `rtrv-monitor` | read-only ops | `select ping info dbsize scan type ttl llen exists client\|list slowlog\|get slowlog\|len memory\|usage` | `*` (reads) | none |
| `rtrv-health` | health check | `ping` | none | none |
| `rtrv-emergency` | break-glass | `+@all` (present only while enabled) | `*` | `*` |
| `default` | none | disabled after staged rollout | — | — |

Notes: `AUTH` is implicit once credentials are configured. `client|setinfo`
is required because redis-py 6.4.0 sends it on connect (186 calls observed).
`multi`/`exec` are used by Kombu/Celery pipelines (54 observed). `smembers`,
`zrangebyscore`, `zcount`, `mget`, `incr` are library code paths not
exercised on the idle instance today; they are included only where the
owning library can invoke them, and the validation matrix exercises the
paths. `unacked*` keys are global (Kombu class constants `unacked`,
`unacked_index`, `unacked_mutex`). Fanout channels use the default prefix
`/{db}.` with `fanout_patterns=True`. Redis cannot isolate users per logical
database; isolation is via key/channel patterns (documented limitation).

## 3. Destructive-command denial (normal roles)

`FLUSHALL`, `FLUSHDB`, `SWAPDB`, `KEYS`, `CONFIG`, `ACL`, `SHUTDOWN`,
`DEBUG`, `MONITOR`, `REPLICAOF`/`SLAVEOF`, `FAILOVER`,
`SAVE`/`BGSAVE`/`BGREWRITEAOF`, `CLUSTER`, `MODULE`, `MIGRATE`, `RESTORE`,
`DUMP`, `SCRIPT FLUSH|KILL`, `FUNCTION`, `WAIT`, `CLIENT KILL|PAUSE|UNPAUSE`,
`RESET`, `LATENCY`, and any command not explicitly allowlisted. Denials are
logged by Redis ACL LOG and alerted.

## 4. Emergency principal

Disabled user in the ACL file; password only in the operator secret store;
`ACL SETUSER ... on >hash +@all` enable + `ACL SAVE`/`ACL DELUSER` disable,
with recorded owner approval, a mandatory post-use disable, and ACL
LOG/telemetry review. Never used for routine operations.

## 5. Secrets and ACL persistence

- Add a mounted Redis configuration with `aclfile` (and optionally bind
  address) so users survive restarts; generate the file with Redis password
  **hashes** (never plaintext).
- Deployment environment gains `REDIS_BROKER_PASSWORD`,
  `REDIS_RESULTS_PASSWORD`, `REDIS_MONITOR_PASSWORD`,
  `REDIS_HEALTH_PASSWORD`; `CELERY_BROKER_URL`/`CELERY_RESULT_BACKEND` are
  updated to per-role URLs. `REDIS_EMERGENCY_PASSWORD` is not placed in the
  deployment environment.
- Rotation: `ACL SETUSER` new password → `ACL SAVE` → rolling restart of
  consumers → verify → document. Rollback: reapply the previous hash and
  service URLs.
- No existing Redis secret exists to rotate; this is first-time provisioning.

## 6. Network exposure

Change the published mapping to loopback (`127.0.0.1:${REDIS_PORT}:6379`) or
remove it entirely if no host-side consumer requires it; the compose network
alone carries application traffic. No TLS is planned while traffic stays on
loopback/Docker network with authentication enabled (documented decision).

## 7. Deployment ordering (later phase; not executed now)

1. Add ACL file + hashed users (additive; `default` still enabled).
2. Update health check to `rtrv-health` credentials; verify PING.
3. Roll `retriva-ingestion` and `retriva-worker` with per-role URLs; verify
   publish/consume/result traffic.
4. Disable `default` user; `ACL SAVE`; verify all clients authenticated.
5. Bind port to loopback; restart Redis container with the new configuration.
6. Post-change observation; verify destructive-command denial via
   `ACL LOG`/`ACL DRYRUN` (non-mutating) and monitoring.
7. Rollback on any gate failure to the previous URLs/ACL/port mapping.

## 8. Compatibility

Celery 5.6.3 / Kombu 5.6.2 / redis-py 6.4.0 command paths are unimplemented
changes only at the ACL boundary; no application source change is expected
beyond environment/URL plumbing. Spec 032/ADR-037 limits (visibility
timeout, R7, task limits) are untouched. The graph event bus remains a
dormant capability; its future activation requires its own narrow ACL
channel.
