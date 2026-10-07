# Spec 031 — Superseded-version Qdrant point lifecycle

- **Status:** ACCEPTED (owner acceptance recorded 2026-10-07; Constitution
  §42, §43). Implementation and isolated validation authorized; live deployment
  and live point deletion remain a separate authorization.
- **Revision:** 2 (2026-10-07) — revision 1 presented `PROPOSED`; owner accepted
  revision 1 with policy selections D1–D5 on 2026-10-07.
- **Repository:** retriva-core
- **Governing:** `.agent/rules/retriva-constitution.md`; ADR-036; cross-references
  Spec 028 / ADR-033 (knowledge metadata + Qdrant operation foundation) and
  Spec 029 / ADR-034 (lock order) and Spec 030 / ADR-035 (upload temp ownership,
  explicitly separate). Spec 028 is not reopened.
- **Owner:** Retriva Core owner (acceptance required).

## 1. Problem

When a document's content changes, the new version is promoted and the prior
version becomes `superseded` (Spec 028 §6). The prior version's Qdrant points
are deactivated (`serving=false`) but **never deleted**: the accepted
`knowledge/purge.py` covers only `lifecycle_state='deleted'` tombstones, and no
cleanup covers superseded versions of **active** documents. Reconciliation
therefore reports a permanent `stale_superseded_points` finding while the hidden
point consumes storage indefinitely. There is also an interruption risk:
supersession is committed in PostgreSQL **before** Qdrant serving deactivation,
so a crash in between leaves a superseded point still `serving=true` and thus
retrieval-visible.

## 2. Evidence

- Live `cust_0007` read-only inventory: 17 Qdrant points, 16 `serving=true`, 1
  `serving=false`; 1 superseded version with 1 verified chunk (`stale_superseded_points=1`).
- Isolated reproduction (real PostgreSQL + real Qdrant, synthetic): a superseded
  version's point is flagged `stale_superseded_points=1`; `Purger.run(apply=True)`
  purges 0 (active document not covered); the superseded point remains in
  Qdrant; and with the point force-set `serving=true` the authoritative
  visibility predicate returns visible (retrieval-leak risk).

## 3. Normative requirements

**R1 — Retention.** Superseded points are retained evidence for a bounded
retention window and then become eligible for deletion. Immediate deletion on
supersession is prohibited. The default window and whether it is global or
per-tenant are **owner decisions** (see §9 and ADR-036).

**R2 — Eligibility (fail-closed).** A point is eligible for cleanup only when
ALL hold: tenant scope known; version is `superseded` (not current); point is
not `serving` and not `staging`; the version is not the document's current
version; no open operation references it; no rollback hold applies; the
retention window has elapsed since `superseded_at`; provenance policy permits
deletion; the collection identity is correct; the point belongs only to that
version; and evidence is authoritative (never inferred from missing legacy JSON).
Any incomplete/conflicting/stale/uncertain evidence ⇒ NOT eligible.

**R3 — Durable intent/evidence.** PostgreSQL is the intent/evidence authority.
Cleanup is recorded in `knowledge.qdrant_operations` reusing the existing
op_type `delete_points` (or `delete_document`) and states
`prepared → executing → applied_unverified → verified` (or `failed` /
`reconciliation_required`), scoped by tenant/document/version/collection with an
idempotency key, candidate fingerprint, expected count, retry budget, actor, and
timestamps. No Redis or process-local authority.

**R4 — Safe deletion.** Deletion targets an explicit, revalidated candidate point
set (bounded batch). Collection-wide or unbounded-filter deletion, deleting by
document alone without version constraints, deleting current/staging points,
point-ID rewrite, treating API acceptance as completion, and success without a
read-only zero-point verification are all prohibited.

**R5 — Zero-point postcondition.** After issuing deletion, a read-only check MUST
confirm zero points remain for the exact candidate set and that current serving
points and retrieval are intact. Only then may the operation close as `verified`.

**R6 — Idempotency / retries / crash recovery.** Duplicate requests and
scheduler re-runs are idempotent; bounded retries; no blind replay after an
ambiguous outcome; durable checkpoints; deterministic resume; reconciliation of
ambiguous/partial results; candidate set revalidated between dry-run and apply.

**R7 — Dry-run.** A mandatory read-only dry-run reports eligible versions/points,
age buckets, blocked-by-reason counts, uncertain candidates, collections/tenants
in bounded anonymised form, estimated reclamation, proposed batch count, and the
candidate fingerprint; it mutates neither store.

**R8 — Reconciliation.** Reconciliation classifies (does not auto-delete):
expected retained superseded points, eligible cleanup backlog, deletion in
progress, verified deleted, ambiguous deletion, orphan points, stale serving
flags, missing operation evidence, restore divergence.

**R9 — Security/tenancy.** Tenant-scoped selection; RLS/role boundaries
unchanged; Core-only DML, no runtime DDL; no CRM/Messaging/Gateway/PUBLIC vector
authority; privileged operator context only; no cross-tenant batch; low-
cardinality metrics; bounded CLI/API output.

**R10 — Separation from upload-staging cleanup.** Superseded-vector cleanup MUST
NOT inspect or delete upload staging files. Upload-staging abandoned-file cleanup
remains separate governance (Spec 030 does not implement a startup sweep).

**R11 — No migration (expected).** Reuse existing `qdrant_operations` op types
and states. If a schema change is later required, stop for additional governance.

## 4. Interruption handling

The proposal MUST define a fail-closed recovery for the existing ordering gap
(supersession committed before serving deactivation): reconciliation flags
`superseded_but_serving`; retrieval filters MUST exclude non-current versions
regardless of the serving flag; and a bounded "deactivate-then-delete" repair is
operator- or scheduler-driven.

## 5. Acceptance criteria

- A1 eligibility predicate implemented fail-closed; current/staging never
  eligible; serving superseded blocks cleanup.
- A2 durable intent/evidence via `qdrant_operations`; no new schema.
- A3 zero-point postcondition mandatory; API acceptance alone insufficient.
- A4 idempotent duplicate/dry-run/apply; ambiguous outcomes reconciled, never
  blindly replayed.
- A5 dry-run read-only, complete, and operator-reviewable.
- A6 reconciliation classifications; no auto-delete.
- A7 tenancy/security/RLS preserved; no cross-tenant batch.
- A8 isolated real PostgreSQL + Qdrant lifecycle tests (including interruption,
  crash, retry, duplicate, restart, Redis reconnect).
- A9 Spec 028/029 compatibility; frozen catalog/registry unchanged; no live
  point mutation in the discovery phase.
- A10 Exactly one local Core commit after acceptance; separate deployment prompt.

## 6. Out of scope

Spec 028/029 architecture; upload-staging cleanup; Qdrant collection/index
reconfiguration; re-embedding; GraphIndexer; CRM/Messaging/Gateway; live
deletion; migration (unless separately accepted).

## 7. Owner decision points

D1 retention window (recommend bounded default; options 7/30/90 days in ADR-036);
D2 global vs per-tenant policy; D3 execution model (operator / scheduled /
hybrid — recommended hybrid, ADR-036); D4 uncertain/adopted provenance handling;
D5 migration if a schema change proves necessary.

## 8. Status

`ACCEPTED` (2026-10-07). Owner selected D1–D5: **D1 = 30 days**, **D2 =
global**, **D3 = hybrid (propose scheduled; apply operator-authorized)**, **D4
= retain adopted_uncertain**, **D5 = none (reuse `qdrant_operations`
`delete_points` + states)**. Additional accepted constraints: 30-day interval
is the effective rollback hold; one global policy; apply requires explicitly
privileged operator authorization; adopted_uncertain never auto-eligible;
`superseded_but_serving` is an integrity incident; explicit bounded point IDs and
mandatory exact zero-point postcondition; ambiguous outcomes enter
`reconciliation_required` (no blind replay); upload-staging cleanup out of scope.
Implementation and isolated validation authorized; live deployment/point deletion
NOT authorized.

### 8.1 Operational bounds B1-B7 (owner-approved 2026-10-07)

- **B1** max versions per cleanup operation = **100** (hard max, not a target; a
  batch may be smaller).
- **B2** max explicit point IDs per operation = **2,000** (hard max). A single
  version exceeding 2,000 points fails closed (no per-version partition
  contract is accepted in this revision).
- **B3** Qdrant wait/verification bound = **30 seconds** per cycle. Expiry does
  NOT imply failure or permission to replay; an unresolved operation moves to
  `reconciliation_required`. Exact zero-point verification may safely close an
  already-applied operation.
- **B4** deletion-issue rate limit = **at most one bounded delete every 30
  seconds** (applies to delete issuance only, not read-only dry-run/verification;
  restarts must not permit a burst — the limit is derived from durable
  `qdrant_operations` evidence).
- **B5** apply concurrency = **1 active cleanup operation globally** (durable,
  not a process-local mutex).
- **B6** maintenance window = apply allowed only within the privileged,
  configurable off-peak window; dry-run/proposal may run outside. Unconfigured
  window fails closed.
- **B7** mandatory stop conditions (OR semantics, evaluated before each delete):
  pause when eligible backlog > **5,000** points OR observed p95 latency >
  **2.0×** the captured pre-apply baseline. On trigger: do not issue the next
  delete; allow an in-flight op to reach `verified`/`reconciliation_required`;
  persist a bounded stop reason; leave remaining candidates unmodified; require
  operator review; never auto-raise limits or bypass the window. Missing
  baseline/sample evidence fails closed during apply.

No limit may be overridden from a public request; privileged overrides require a
later governed decision and are not implemented.