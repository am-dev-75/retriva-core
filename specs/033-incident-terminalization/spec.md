# Spec 033 (ACCEPTED) — Incident-specific no-effect dispatch-unknown terminalization

Status: ACCEPTED (owner selected Option 2 on 2026-10-08; implemented under this spec)
Date: 2026-10-08. Core baseline: 4125b039c5009fc41b038f520e976677cde195a8.

## 1. Problem
A single live quarantined incident cannot be closed through any supported
mechanism: job `dispatch_unknown` (non-terminal) with a QUEUED attempt whose
`publication_state=unknown` and never executed. The owner requires an audited,
incident-scoped operator mechanism to terminalize it (job FAILED; selected
attempt FAILED) with reason
`operator_fail_clean_pre_fix_ambiguous_generation_no_effects`.

## 2. Governance conflict (blocker)
The accepted state machines in `src/retriva/jobs/domain.py` do not permit the
required dispositions:

- ALLOWED_ATTEMPT_TRANSITIONS[QUEUED] = {RUNNING, CANCELLED, DISPATCH_FAILED,
  LOST} — there is **no** QUEUED -> FAILED.
- ALLOWED_JOB_TRANSITIONS[DISPATCH_UNKNOWN] = {QUEUED, MANUAL_REVIEW,
  CANCELLING} — there is **no** DISPATCH_UNKNOWN -> FAILED.
- Legal routes to job FAILED exist only via MANUAL_REVIEW -> FAILED (T22,
  operator resolution) or RUNNING -> FAILED (T18).

Therefore the exact owner-required terminal states require **new governed
transitions** (a new lifecycle contract), not merely a missing operator surface.

## 3. Options (owner decision required)

**Option 1 — extend the state machine (matches the owner's exact requirement).**
Add governed transitions QUEUED -> FAILED and DISPATCH_UNKNOWN -> FAILED with
bounded preconditions (proven no-effect, no claimant, generation match), audit,
idempotency, and late-delivery fencing. Requires an accepted amendment to
Spec 025's state machine (new transition rules) and possibly a CHANGELOG/evidence
update. No schema migration (statuses exist).

**Option 2 — implement using existing legal transitions (recommended if the owner
accepts an equivalent disposition).**
- attempt: QUEUED -> LOST (allowed; "reconciliation: publication lost" — an
  accurate semantic for a queued/publication-unknown attempt);
- job: DISPATCH_UNKNOWN -> MANUAL_REVIEW (T9) -> FAILED (T22 operator
  resolution).
All transitions are already accepted; only a new **job-scoped, privileged,
audited operator command** is added (no new states). Audit reason remains
`operator_fail_clean_pre_fix_ambiguous_generation_no_effects`.

**Option 3 — cancellation semantics.** QUEUED -> CANCELLED + job -> CANCELLING ->
CANCELLED (T13/T14). Legal, but not a "failed" disposition and the owner already
rejected cancellation.

## 4. Recommended
Prefer **Option 2** (no new states; fully within accepted semantics; attempt
`lost` is the semantically correct terminal for a publication-unknown queued
attempt), with **Option 1** if the owner insists on the literal FAILED attempt
status. Both require a governed operator command with: privileged tenant scope;
exact job/attempt/generation/publication preconditions; durable no-effect proof;
job-before-attempt locking; audit; idempotency; concurrency safety; late-delivery
fencing; separate downstream fail-ingestion and parse-temp release.

## 5. Explicitly out of scope
No migration, no Qdrant mutation, no global reconciliation, no generic
state-editing command, no public/tenant API exposure.

## 6. Accepted decision (2026-10-08) — Option 2

The owner ACCEPTED Option 2: implement a private, job-scoped, audited operator
mechanism using ONLY the accepted legal transitions (no state-machine extension):

    attempt: QUEUED -> LOST
    job:     DISPATCH_UNKNOWN -> MANUAL_REVIEW -> FAILED

Required bounded reason:
`operator_fail_clean_pre_fix_ambiguous_generation_no_effects`.

Accepted contract: exact single-generation scope; job-before-attempt lock order;
mandatory operator privilege + tenant scope; mandatory dry-run + evidence
fingerprint; durable no-effect proof from authoritative PostgreSQL evidence (zero
chunks, zero Qdrant operations, staging not current, known-good current present,
ingestion failed); no active claimant/later generation/terminal success/retry/
reschedule/republish/cancellation; durable audit; terminal idempotency; late-
delivery fencing; no batch/wildcard/all-tenants mode; no Qdrant mutation;
downstream ingestion closure and parse-temp release remain separate; no schema
migration; deployment and live invocation separately authorized.
