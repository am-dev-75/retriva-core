# ADR-038 (ACCEPTED) — Incident-specific no-effect dispatch-unknown terminalization

Status: ACCEPTED (owner selected Option 2 on 2026-10-08). Date: 2026-10-08. Core baseline
4125b039c5009fc41b038f520e976677cde195a8.

## Context
The live quarantined incident (job `dispatch_unknown`, QUEUED attempt with
`publication_state=unknown`, generation 1, zero chunks/operations/points) cannot
be closed by any supported mechanism. The owner requires an audited,
incident-scoped terminalization.

## Finding
The accepted `ALLOWED_ATTEMPT_TRANSITIONS`/`ALLOWED_JOB_TRANSITIONS` do not
include QUEUED -> FAILED or DISPATCH_UNKNOWN -> FAILED. The exact required
disposition therefore needs new governed transitions.

## Decision required
Either (1) extend the accepted state machine with the two transitions, or
(2) implement the mechanism using existing legal transitions: attempt
QUEUED -> LOST plus job DISPATCH_UNKNOWN -> MANUAL_REVIEW -> FAILED (T9/T22),
adding only a privileged job-scoped audited operator command.

## Consequences
Option 1 amends Spec 025's state machine. Option 2 adds no states and is
preferred. Neither requires a schema migration. Both keep Spec 028/029/030/031/032
boundaries, prohibit global reconciliation and Qdrant mutation, and require
separate downstream fail-ingestion and parse-temp release.

## Accepted decision (2026-10-08)

Owner accepted Option 2: no state-machine extension; implement a private,
job-scoped, audited operator mechanism that terminalizes exactly one proven
no-effect dispatch_unknown generation via attempt QUEUED->LOST and job
DISPATCH_UNKNOWN->MANUAL_REVIEW->FAILED, with durable no-effect evidence,
dry-run fingerprint matching, job-before-attempt locking, audit, idempotency,
concurrency safety, and late-delivery fencing. Direct QUEUED->FAILED and
DISPATCH_UNKNOWN->FAILED transitions are rejected as illegal. No migration.
