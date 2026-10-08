# Spec 034 — Tasks (PROPOSED)

Status: ACCEPTED (owner decision 2026-10-08). P0 is complete; P1-P2 proceed under this task; P3 deployment remains separately authorized.

## Governance
- T1 Owner reviews and accepts (or replaces) the ACL role model, emergency
  principal, network binding, topology decision, and result-backend input.
- T2 Finalize ADR-039 and this pack to ACCEPTED through the normal process;
  update registry entries accordingly.

## Implementation (deployment repository)
- T3 Add hashed ACL users file + mounted Redis configuration with `aclfile`.
- T4 Add per-role credentials and URL plumbing to deployment environment
  (broker, results, monitor, health).
- T5 Update Redis health check to authenticated `PING`.
- T6 Bind the published port to loopback (or remove it).
- T7 Add guarded no-flush reconnect runbook command.
- T8 Add destructive-command prohibition + policy checks in scripts/prompts;
  add ACL LOG/denied-command and mass-deletion alerting; add key/queue/result
  baselines.

## Validation
- T9 Execute the isolated validation matrix (external validation plan), with
  library-version-exact stacks.
- T10 Attribute `EVALSHA`/`SCRIPT LOAD`; keep (temporary) or deny; record.
- T11 Prove rotation and rollback without task/result loss.

## Deployment
- T12 Produce the separate deployment prompt (fresh recovery assets, staged
  rollout, observation, denial verification, rollback, closure taxonomy).
- T13 Execute deployment only under a separate owner authorization.

## Closeout
- T14 Record monitoring evidence, periodic ACL review cadence, and
  emergency-principal residual-risk acceptance; link this hardening to the
  recorded FLUSHALL deviation.
