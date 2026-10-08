# Spec 032 (ACCEPTED) — Bounded Celery/Redis worker-loss redelivery and R7 interaction

Status: ACCEPTED (owner accepted Option B on 2026-10-08; governance-only; no implementation authorized)
Author: agent. Date: 2026-10-08. Candidate: Core d07db21387f1528c4adfa9d46af89fa13757f8a7.

## 1. Problem
The abrupt-worker-loss runtime gate (isolated, SIGKILL of the Celery worker child
with Redis up) failed to show prompt redelivery/takeover: the message stayed in
the Redis unacked set pending Kombu's `restore_visible` cycle bounded by
`visibility_timeout`, and the attempt remained RUNNING at execution_generation=1
(R7 fires at 1800 s but is operator-invoked). The earlier determination that
prompt redelivery was already proven is **superseded** by this evidence
(`abrupt-worker-loss-runtime-report.md`).

## 2. Superseded conclusions
- `DETERMINATION-orphaned-running-recovery.md`: its claim that the prior stuck
  RUNNING was purely a harness artifact (graceful restart + Redis restart) is
  superseded; abrupt SIGKILL with Redis up also leaves the attempt RUNNING until
  the visibility timeout, not prompt redelivery.

## 3. Verified runtime scope
celery 5.6.3, kombu 5.6.2, redis-py 6.4.0, billiard 4.3.1. Kombu Redis
`Transport.visibility_timeout` default 3600 s; no project configuration surface.
`QoS.restore_visible(start=0, num=10, interval=10)` decimates the polling loop
(~10 s with default polling_interval) and restores only messages older than
`now - visibility_timeout`; restoration marks `redelivered=True`
(`_do_restore_message`). Restoration is performed by a RUNNING worker's QoS, so a
replacement worker must be up and polling. With `task_acks_late=True` and
`task_reject_on_worker_lost=True`, the observed SIGKILL produced a parent
`WorkerLostError` but no prompt reject/requeue; the message awaited restoration.

## 4. Task runtime bounds
Durable task types: v2_document (restart-safe), v2_mediawiki (restart-safe),
v2_upload (non-restart-safe, owns the parse temp), v2_artifact (non-restart-safe).
Celery soft/hard time limits are **disabled** (0) → **H is unbounded** today;
reliable p50/p95/p99 telemetry is unavailable. Fail-closed conclusion: no finite
H can be assumed; therefore H + G < V < R cannot be satisfied without first
bounding per-task hard limits.

## 5. Options
- **A — configurable visibility_timeout** (bounded V) requires bounding H first
  (per-task hard limits per queue); then H+G < V < R is satisfiable.
- **B — retain default V=3600 and R=1800.** Race-safety argument (see §6).
- **C — immediate worker-lost requeue.** Not supported by the observed parent
  behaviour; would rely on fragile internals; not recommended.

## 6. Recommended decision (primary B, fallback A)
**Primary: Option B** — retain V=3600 s and R=1800 s. Safety: R < V, and after V
the restored message is marked `redelivered`, so the T8 claim takes over only with
provable dead-worker evidence (worker_id differs and/or started_at > stale
threshold), re-claiming the SAME attempt once (execution_generation+1) under the
atomic claim; terminal attempts and `manual_review` jobs are idempotent no-ops,
fencing duplicate business/terminal effects. R7 remains the operator-invoked
faster path for restart-safe redispatch / non-restart-safe manual_review.

Concrete values — V=3600; R=1800; H=unbounded today (must be bounded before any V
reduction); G=300; restore decimation ≈10 s (treat restoration latency as
≤ V + 10 s + jitter); recovery concurrency = 1; max worker-loss recovery
objective ≈ V + G = 3900 s automatic, or R + operator latency via R7; max
parse-temp claimed after worker loss = same window; alerts when a RUNNING attempt
exceeds R and when unacked age exceeds R.

**Fallback: Option A** — bound per-task hard limits per queue (e.g. H=600 s),
G=300 s, V=1200 s, R=1800 s → H+G (900) < V (1200) < R (1800). Requires the
governed configuration surface and per-task limit changes.

## 7. Invariants
R < V (B) or H+G < V < R (A); one logical owner/atomic claim; execution_generation
fencing; no blind retry of ambiguous side effects (→ manual_review); PostgreSQL
durable authority; tenant-scoped bounded recovery; parse-temp retained while a
claimant exists and released exactly once at terminal; lock order
`jobs -> job_attempts`.

## 8. Migration / rollback
No migration or schema change for either option (existing states/columns). Option
A adds a deployment configuration surface + per-task limits; rollback restores
values.

## 9. Observability / security
Low-cardinality worker-loss and restore metrics; sanitized errors; no cross-tenant
recovery; no credentials/IDs/paths in output.

## 10. Acceptance matrix (future implementation)
SIGKILL child (broker up); full-container SIGKILL; parent alive/lost; task < H,
≈H, > H; restoration before R7; restoration vs R7 race; concurrent claimants;
possibly-alive negative control; side effect before evidence; evidence before
terminalization; API/worker restart; Redis reconnect without flush; temp retention
+ exactly-once cleanup; non-restart-safe manual_review; retry exhaustion;
cross-tenant isolation; no duplicate job/version/manifest/Qdrant/terminal effect;
Specs 028/029/030/031 compatibility.

## 11. Disposition
Deployment and live incident recovery remain separately authorized. No product or
configuration change is authorized by this proposal.

## 12. Owner acceptance (2026-10-08)

The owner ACCEPTED the recommended primary Option B exactly as recorded:

- V (Kombu Redis visibility timeout) = 3600 seconds
- R (R7 stale-RUNNING threshold) = 1800 seconds
- H (current task hard runtime) = unbounded in this revision
- G (modeled recovery/jitter margin) = 300 seconds
- restore-visible effective polling interval = approximately 10 seconds
  (installed Kombu decimation)
- recovery/reconciliation concurrency = 1
- expected transport restoration latency = no earlier than V, normally bounded
  by V + ~10 s plus ordinary scheduling jitter, provided at least one compatible
  worker is running to execute restore_visible

Accepted consequences and invariants:

1. R7 is the primary bounded PostgreSQL recovery owner because R=1800 < V=3600.
2. Eventual Redis restoration of the original unacknowledged delivery is expected
   and MUST be fenced by atomic attempt claim, terminal-state checks, and
   execution_generation.
3. A delivery restored after R7 resolution MUST become an idempotent no-op unless
   it is the single currently authorized claimant.
4. Restart-safe tasks may follow the accepted R7 lost-and-redispatch path.
5. Non-restart-safe tasks MUST enter manual_review and MUST NOT be replayed
   automatically.
6. Ambiguous side effects MUST fail closed into reconciliation/manual review
   rather than blind retry.
7. Parse-temp ownership remains with a valid claimant or the
   manual-review/reconciliation owner and is released exactly once only when no
   valid claimant remains.
8. Confirmed Redis publication is queued, NOT dispatch_unknown.
9. dispatch_unknown remains available for genuinely indeterminate transports or
   phases, but MUST NOT be fabricated for the tested Kombu Redis boundary.
10. No new visibility-timeout configuration surface, task hard limit, migration,
    or immediate worker-lost requeue correction is authorized in this revision.
11. Option A remains a future fallback only; it is NOT accepted for
    implementation now. Any later Option A adoption requires separately governed
    task hard limits and concrete V/R/H/G values.
12. The live MediaWiki incident remains governed by fail-and-clean, with no R2
    republish and no combined global R2/R9 apply.

Installed scope: Celery 5.6.3, Kombu 5.6.2, redis-py 6.4.0, billiard 4.3.1,
unless later superseded by governed compatibility evidence. H is unbounded in
this revision; G=300 s is a modeled margin, not proof that H+G<V; H+G<V<R is NOT
satisfied in the current revision and is NOT claimed. Acceptance does not
authorize deployment or live incident mutation.
