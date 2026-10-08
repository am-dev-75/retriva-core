# ADR-039 (ACCEPTED) — Redis destructive-command denial and least-privilege identities

Status: ACCEPTED (owner accepted the recommended control set on 2026-10-08). Implementation tracked separately; deployment requires a separate authorization. Date: 2026-10-08. Core baseline
61bb777e4a15a74e1ba528163a666bdc7fcf9aaf. Companion spec: Spec 034
(PROPOSED).

## Context

A post-incident audit of the completed Spec 033 recovery found one
unauthorized live `FLUSHALL` against the project Redis instance. The audit
closed as disposition A (`CLOSE_RECOVERY_WITH_RECORDED_DEVIATION`) with no
unresolved impact, and identified the structural weakness that permitted it:
the instance runs standalone without TLS and with `requirepass` unset, its
only user is `default on nopass ~* &* +@all`, its port is published on all
host interfaces, and no ACL persistence or credential mechanism exists.
Broker (db0) and result backend (db1) share the instance; the deployment
currently assumes unrestricted access.

## Decision (proposed)

Adopt Redis ACL least-privilege identities with deny-by-default command
allowlists: `rtrv-broker`, `rtrv-results`, `rtrv-monitor`, `rtrv-health`, and
a disabled-by-default `rtrv-emergency` principal; disable `default`; deny
`FLUSHALL`/`FLUSHDB` and the other destructive/administrative commands listed
in Spec 034 to all normal identities; persist users via a mounted
configuration with `aclfile` and hashed passwords; stop publishing port 6379
on all interfaces. Retain the single instance for now; split only on the
documented criterion.

## Alternatives considered

- **Rename/disable dangerous commands**: rejected as primary control
  (security by obscurity, breaks emergency tooling, harder rollback where
  ACLs exist); may remain a defense-in-depth option for the emergency path.
- **Topology separation now**: deferred; the audit proved only broker/result
  state on the instance and no unrelated application state; split criterion
  documented.
- **Procedural controls only** (banners, typed acknowledgement, wrappers):
  necessary but insufficient alone; adopted as complements, not substitutes.

## Consequences

- Broker/result flows require per-role credentials and URL/health-check
  updates; a staged rollout and rollback path are mandatory.
- `EVALSHA`/`SCRIPT LOAD` require attribution before final allow/deny.
- ACL LOG and monitoring become first-class evidence for denied attempts.
- No change to Celery limits (visibility timeout/R7/task limits) or to the
  closed MediaWiki incident; no data restore/replay is introduced.
- Implementation is gated on this ADR's acceptance per Constitution §§42/43.

## References

Spec 033; ADR-037/038; FLUSHALL deviation audit artifacts
(`redis-flushall-post-incident-*`); Spec 034 pack; role/command matrix and
validation plan artifacts.
