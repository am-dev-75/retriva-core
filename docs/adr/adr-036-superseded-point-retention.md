# ADR-036: Superseded-version Qdrant point retention and cleanup

- **Status:** ACCEPTED (owner acceptance 2026-10-07; Constitution §42, §43).
  Implementation and isolated validation authorized; live deployment/live point
  deletion NOT authorized.
- **Date:** 2026-10-07
- **Repository:** retriva-core
- **Relates:** Spec 031; Spec 028 / ADR-033 (knowledge metadata + Qdrant
  operation model, not reopened); Spec 029 / ADR-034 (lock order); Spec 030 /
  ADR-035 (upload temp ownership — explicitly separate).
- **Deciders:** Retriva Core owner (acceptance required).

## Context

On changed-content replacement, `promote_version` marks the prior version
`superseded` and the pipeline deactivates its Qdrant points (`serving=false`).
Those points are never deleted: `knowledge/purge.py` only covers
`lifecycle_state='deleted'` tombstones. Reconciliation reports a permanent
`stale_superseded_points` finding while hidden points consume storage. Because
PG supersession commits before Qdrant serving deactivation, an interruption can
also leave a superseded point `serving=true` (retrieval-visible).

Live inventory: exactly one hidden superseded point today; isolated reproduction
confirmed purge does not remove it and that a `serving=true` superseded point is
retrieval-visible.

The existing `knowledge.qdrant_operations` table already permits op types
`delete_points` / `delete_document` and states `prepared/executing/
applied_unverified/verified/failed/reconciliation_required`, so no schema change
is required to record cleanup intent/evidence.

## Decision (proposed)

1. **Retain-then-delete.** Superseded points are retained evidence for a bounded
   window after `superseded_at`, then eligible for deletion. Immediate deletion
   on supersession is rejected (loses rollback/fallback/debugging value and
   risks deleting points a slow reader still references).

2. **Fail-closed eligibility.** Delete only when the full R2 predicate holds;
   any uncertainty ⇒ retain. Never infer eligibility from missing legacy JSON.

3. **Reuse the accepted operation model.** Record intent/evidence in
   `qdrant_operations` (`delete_points`, optionally `delete_document`), with an
   idempotency key, candidate fingerprint, expected count, and a mandatory
   read-only zero-point postcondition. **No migration.**

4. **Dry-run first; operator-reviewable.** A read-only dry-run is mandatory and
   reports eligible/blocked/uncertain candidates and reclamation estimate.

5. **Execution model (proposed hybrid).** Cleanup is an operator-triggered
   privileged command and MAY be driven by a scheduled reconciliation job that
   only ever *proposes* work; apply requires operator authorization (or an
   explicitly accepted auto-apply policy). Rationale: high auditability, bounded
   blast radius, no unbounded accumulation, RLS-safe.

6. **Reconciliation classifies, never auto-deletes.**

7. **Interruption repair.** Reconciliation surfaces `superseded_but_serving`;
   retrieval already excludes non-current versions in authoritative mode via the
   current-version serving flag, and the repair deactivates then deletes under
   the same operation model.

## Owner decision points (required before implementation)

- **D1 Retention window:** options **7 / 30 / 90 days** (no evidence-based
  duration exists; recommend 30 days, matching the accepted succeeded-job
  retention default, subject to owner policy).
- **D2 Scope:** one global window vs per-tenant override.
- **D3 Execution:** operator-only / scheduled / hybrid (recommend hybrid).
- **D4 Uncertain/adopted provenance:** retain (recommended) vs policy-based.
- **D5 Migration:** none expected; if a schema change becomes necessary, stop for
  additional acceptance.

## Consequences

- Hidden superseded points are reclaimed after the window; storage growth is
  bounded; the `stale_superseded_points` finding becomes actionable.
- Rollback within the window remains possible via retained points.
- No schema change; Spec 028/029 semantics and Spec 030 upload temp ownership are
  untouched.

## Compliance

§42/§43 (governance before implementation); §20 (store of record — PG authority);
§28 (explicit, authorized, auditable deletion); §30 (auditability); §32 (tenant
isolation); §40 (regression protection).

## Acceptance record (2026-10-07)

The owner accepted ADR-036 and Spec 031 with D1=30 days, D2=global, D3=hybrid
(operator-authorized apply), D4=retain adopted_uncertain, D5=none (no
migration). Implementation and isolated validation are authorized; live
deployment and live point deletion require a separate authorization. The
accepted pack does not fix concrete batch/rate-limit/concurrency bounds, which
remain an open owner decision before the apply path is implemented.
