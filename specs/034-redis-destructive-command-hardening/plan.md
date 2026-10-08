# Spec 034 — Plan (PROPOSED)

Status: ACCEPTED (owner decision 2026-10-08). Implementation and isolated validation may proceed; deployment remains separately authorized.

## P0 — Owner decision (current phase)

- Review `spec.md`, `architecture.md`, `acceptance.md`, ADR-039, and the
  external owner-decision pack (`redis-destructive-command-hardening-owner-decision.md`,
  role matrix, option matrix, validation plan).
- Decide: ACL model, emergency principal, network binding, result-backend
  retention input, topology (retain now / split criterion).
- Record acceptance (or specific replacements) in the ADR/spec status
  sections; only then may registries move beyond `proposed` and
  implementation begin.

## P1 — Implementation (deployment configuration only)

- Add ACL file with hashed `rtrv-*` users (default still enabled, additive).
- Add environment plumbing for per-role URLs and health credentials.
- Update compose: mounted config + `aclfile`, health check authentication,
  loopback port binding.
- Add guarded no-flush reconnect runbook command and destructive-command
  prohibition/policy checks in scripts and prompts.
- Add monitoring: ACL LOG collection, denied-command alerts, keyspace/queue/
  result baselines.
- Expected repositories touched: deployment repository (configuration and
  runbooks); Core only if URL plumbing requires it (unlikely; a Core change
  would reopen P0). No migration, no schema change.

## P2 — Isolated validation (mandatory gate)

- Execute the validation matrix from `acceptance.md` and the external
  validation-plan artifact on fresh isolated PostgreSQL/Redis/Qdrant
  resources with the exact deployed library versions.
- Trace and attribute the unattributed `EVALSHA`/`SCRIPT LOAD` calls
  (`ACL LOG`/`MONITOR` in isolation only); keep or deny accordingly.
- Prove denied destructive/admin commands, fail-closed auth, rotation,
  restart/reconnect without flush, rollback, and zero task/result regression.
- Remove isolated resources afterwards; no global prune.

## P3 — Deployment (separate authorization required)

- Fresh PostgreSQL backup and Qdrant snapshot; rollback tags/credentials and
  configuration export (secrets redacted).
- Quiesce producers for the replacement window; the connector has no Redis
  role and requires no action (handled only if the separate deployment prompt
  requires it).
- Staged identity rollout and Redis container recreation exactly as ordered
  in `architecture.md` §7; verify baselines and denial; observe; close as
  SUCCESS/PARTIAL/ROLLED_BACK/BLOCKED.

## P4 — Closeout

- Record ACL review cadence, monitoring evidence, and residual-risk
  acceptance for the emergency principal. Update runbooks and the deviation
  record linkage. No changes to the closed MediaWiki incident.

## Rollback strategy (all phases)

- P1/P2: revert configuration files and isolated stacks; no live impact.
- P3: restore previous service URLs/health check/port mapping and the
  previous ACL state (default user re-enabled), preserving volumes; verify
  queue/unacked/result baselines; never restore/replay Redis data.
