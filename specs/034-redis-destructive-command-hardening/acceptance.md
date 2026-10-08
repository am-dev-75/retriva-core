# Spec 034 — Acceptance (PROPOSED)

Status: ACCEPTED (owner decision 2026-10-08) for the specification and ADR-039 control set; criteria B-D below are validated separately and deployment remains separately authorized.

## A. Governance acceptance criteria

1. Owner records an explicit decision on each of: ACL role model, emergency
   principal, network binding, topology (retain/split criterion), and
   result-backend retention input.
2. ADR-039 moves to ACCEPTED by owner decision; registry entries (spec 034,
   ADR 039) move from `proposed` to `accepted` only at that point.
3. No implementation, configuration, or deployment change is made before
   acceptance.

## B. Implementation acceptance criteria

4. Redis runs with authenticated, deny-by-default users: `rtrv-broker`,
   `rtrv-results`, `rtrv-monitor`, `rtrv-health`; `default` disabled; users
   persisted through an `aclfile` with hashed passwords.
5. Normal roles cannot execute `FLUSHALL`, `FLUSHDB`, `SWAPDB`, `KEYS`,
   `CONFIG`, `ACL`, `SHUTDOWN`, `DEBUG`, `MONITOR`, `REPLICAOF`/`SLAVEOF`,
   `FAILOVER`, `SAVE`/`BGSAVE`/`BGREWRITEAOF`, `CLUSTER`, `MODULE`, `MIGRATE`,
   `RESTORE`, `DUMP`, `SCRIPT FLUSH|KILL`, `FUNCTION`, `WAIT`,
   `CLIENT KILL|PAUSE|UNPAUSE`, `RESET`, `LATENCY`, or any command outside
   the role allowlist; denials appear in ACL LOG and alerting.
6. Port 6379 is no longer published on all interfaces.
7. Health check authenticates as `rtrv-health` and requires only `PING`.
8. Secrets appear only through the deployment secret mechanism; no plaintext
   password in repository content or artifacts; rotation procedure
   documented and rehearsed in isolation.

## C. Validation acceptance criteria (isolated, exact versions)

9. Celery 5.6.3 / Kombu 5.6.2 / redis-py 6.4.0 traffic passes with each
   role's allowlist: normal publish/consume; late acknowledgements and
   unacked restoration; result write/read/expiry; worker start/restart; API
   start/restart; Redis reconnect without flush; R7 and Spec 033 dry-runs;
   operator read-only diagnostics.
10. Every denial class in criterion 5 verified via a non-mutating method
    (`ACL DRYRUN`/ACL LOG) and never by executing a destructive command in
    production.
11. Authentication failure is fail-closed; rotation causes no queue loss,
    duplicate effects, stuck jobs, or result regression; rollback restores
    prior behavior.
12. `EVALSHA`/`SCRIPT LOAD` attributed and either tightly allowlisted with a
    justification or denied with evidence that no deployed consumer requires
    them.

## D. Deployment acceptance criteria (later, separately authorized)

13. Staged rollout per `architecture.md` §7: additive users → authenticated
    health check → consumer restarts with per-role URLs → `default` disabled
    → loopback binding → observation.
14. Baselines compared before/after: queue depth, unacked, result records,
    binding set; no loss or duplication.
15. Post-change observation window clean; ACL review scheduled; emergency
    principal disabled.

## E. Explicit non-goals

16. No restore/replay of Redis state; no changes to visibility timeout, R7
    thresholds, or task limits; no changes to the closed MediaWiki incident;
    no Core business-logic change.
