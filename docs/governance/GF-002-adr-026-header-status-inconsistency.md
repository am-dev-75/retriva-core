# GF-002: ADR-026 artifact header said PROPOSED while the registry records it accepted

- **Identifier:** GF-002 (governance follow-up record)
- **Type:** governance defect — artifact status vs registry status
  inconsistency
- **Status:** **CLOSED (2026-10-23)** — resolution 1 (evidence-backed
  metadata correction) applied through the accepted governance process;
  see "Resolution" below.
- **Recorded:** 2026-10-23
- **Discovered during:** Spec 038 / ADR-043 revision 3 (campaign
  governance), revision task §15 ("ADR-026 status inconsistency")
- **Affected documents:**
  `retriva-crm-assistant/docs/adr/adr-026-confirmation-ready-outcome-contract.md`
  (header) and `retriva-core/docs/governance/spec-adr-registry.yaml`
  (crm-assistant ADR `026` entry)
- **Scope of this record:** governance documentation only. Resolving it
  changed no application code, tests, deployment configuration,
  prompts, routes, metrics, or container images, and no ADR-026
  decision, requirement, or contract. It has no effect on Spec 038 /
  ADR-043 semantics.

## Resolution (2026-10-23)

Resolution 1 — evidence-backed metadata correction through the accepted
governance process (recommended and applied):

- The ADR-026 artifact header status line was corrected from the stale
  `PROPOSED (Phase C0-A documentation gate, 2026-10-01; awaiting owner
  acceptance)` to `ACCEPTED (accepted 2026-10-04 through the
  project-wide registry backfill …; this artifact header was corrected
  … under governance follow-up GF-002, with no decision, requirement,
  or contract change; the contract is implemented in
  `src/retriva_crm_assistant/postgres/qualification/confirmation.py`
  with tests in `tests/test_confirmation_ready_outcome.py`)`.
  No other line of ADR-026 was altered.
- The registry entry was NOT changed: it already recorded `accepted`
  (backfill 2026-10-04), which the correction brings the artifact
  header into agreement with; changing the registry would have erased
  the accepted governance record.
- Registry/header consistency is now covered by a deterministic
  regression test: `retriva-core/tests/test_governance_registry.py::
  SpecAdrRegistryIntegrity.test_adr026_header_matches_registry`
  (asserts the artifact header status matches the registry entry and
  contains no `PROPOSED`); the full registry-integrity suite is green
  (15 passed, 2026-10-23).
- Evidence establishing acceptance, recorded for the trail: the
  aggregate registry backfill (2026-10-04, `spec-numbering-followup-
  2026-10-03.md` disposition F-2/F-3) records the entry as accepted
  with note `historical backfill`; the contract is implemented and
  tested in the CRM Assistant; and `retriva-gateway/specs/001-hybrid-
  intent-routing/` consumes ADR-026 (spec.md:576 and related
  plan/tasks/acceptance references) under its accepted Phase C work.

## Defect (historical record as filed)

The two documents disagreed about ADR-026's lifecycle status:

- the artifact header read:
  `Status: PROPOSED (Phase C0-A documentation gate, 2026-10-01;
  awaiting owner acceptance)`;
- the project-wide registry recorded ADR-026 (retriva-crm-assistant)
  as `status: accepted`, `allocated: "2026-10-04"`, notes
  `historical backfill` (the 2026-10-04 backfill recorded "their
  statuses as of backfill" per the registry header).

Under Constitution §4 (disagreements between governing documents and
the documented state MUST be recorded as defects), this record was
filed without amending either artifact; the resolution above then
corrected the stale header explicitly (never silently).

## Verified facts (as filed)

- ADR-026's contract is **implemented and tested** in the CRM
  Assistant (`src/retriva_crm_assistant/postgres/qualification/
  confirmation.py`; `tests/test_confirmation_ready_outcome.py`), which
  is consistent with the registry's `accepted` status and with the
  gateway Spec 001 Phase C dependence on the contract; the artifact
  header was never updated (single commit `faf0d62`).
- `retriva-gateway/specs/001-hybrid-intent-routing/` consumes ADR-026
  (spec.md:576 and related plan/tasks/acceptance references).
- The registry's `historical backfill` note does not itself include an
  owner-acceptance citation; the backfill entry is the registry-era
  record of the acceptance the implementation implies.
- The inconsistency is documentary: no behavioral contradiction
  between code, the ADR's content, and the registry entry was found.

## Resolution options (as filed; option 1 applied, see Resolution above)

1. **Evidence-backed metadata correction (recommended).** Through the
   accepted governance process, correct the ADR-026 artifact header
   status line (an explicit, recorded correction — not a silent edit)
   to reflect the accepted/implemented state the registry already
   records, citing this record and the implementation/test evidence.
2. **Registry correction.** If the owner concludes formal acceptance
   never occurred, correct the registry entry to `proposed` through the
   registry's governing process and route ADR-026 through the normal
   acceptance path.

## Constraints

- This record MUST NOT be resolved by silently editing ADR-026 or the
  registry; the chosen correction is an explicit, auditable act.
- No ADR-026 behavior, contract, code, or test may change as part of
  resolving this record.
- Spec 038 / ADR-043 (campaign governance) MUST NOT depend on or
  pre-empt the resolution; this record is referenced there for
  transparency only.

## Cross-references

- `retriva-crm-assistant/specs/038-campaign-governance-grace-exclusions-history/current-state-audit.md`
  §4 item 8 (disposition recorded) and `spec.md` §20/§22.
- `retriva-core/docs/governance/GF-001-dangling-reranker-adr-reference.md`
  (the pattern this record follows).
- `retriva-core/docs/governance/spec-numbering-followup-2026-10-03.md`
  (F-2/F-3; backfill policy the registry header cites).
