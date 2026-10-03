# Constitution v1.2 — section-by-section provenance record

- **Date:** 2026-10-03 (ratification revision round 1 applied)
- **Pre-ratification draft:**
  `retriva-core/docs/governance/constitution-v1.2-draft.md`
  (`**Status:** PROPOSED`; 1213 lines;
  SHA-256 `ae8b9866106898c945b288262c3009619dc…` — full value:
  `ae8b9866106898c945b288262c3009619b3779dc4c9df1a59a2ed5398c71fd06`)
- **Candidate canonical:**
  `retriva-core/docs/governance/constitution-v1.2-canonical-candidate.md`
  (`**Status:** ACTIVE` upon installation; 1215 lines;
  SHA-256 `8b2fb1fe7c26257bf92742d16e8debafd154f65975b10e5c5891d578685f69ca`)
- **Decision:** ADR-028 (recovery as a newly ratified v1.2; v1.1 not
  reconstructed in place; defective v1.1 preserved unchanged in Git
  history)
- **Ratification status:** **RATIFIED** by the owner on 2026-10-03
  (hash `8b2fb1fe7c26257bf92742d16e8debafd154f65975b10e5c5891d578685f69ca`);
  installed byte-for-byte as canonical
  (`retriva-core/.agent/rules/retriva-constitution.md`, installed
  hash verified equal to the ratified hash); integrity pins applied;
  integrity test green (6/6).  Ratification event recorded in
  ADR-028 (`ACCEPTED`).

## Provenance classes

- **SURVIVING:** byte-identical to the committed (truncated) v1.1 —
  accepted, ratified v1.1 content.
- **AMENDED:** surviving text with an explicitly declared metadata
  change (version/date; status; recovery note).
- **NEWLY DRAFTED:** text that did not exist in v1.1; ratified for the
  first time with v1.2.  Never presented as recovered v1.1 content.

## Section-by-section record

| Region | Class | Notes |
|---|---|---|
| YAML frontmatter (`description`, `alwaysApply: true`) | SURVIVING | unchanged |
| Header: `**Version:** 1.1` → `1.2`; `**Date:**` → `2026-10-03` | AMENDED | revision 1 prerequisite; version/date bump |
| Header: `**Status:** Active` → `**Status:** PROPOSED` (draft) / `**Status:** ACTIVE` (candidate canonical) | AMENDED | **revision 1**; `ACTIVE` exists only in the canonical installation after ratification |
| Header: canonical location + `RETRIVA_CONSTITUTION` paragraph | SURVIVING | unchanged |
| Header: **Recovery note (v1.2)** | NEWLY DRAFTED | provenance pointer; ratification/installation semantics |
| §1 Mission … §41 Internal observability (Parts I–V) | SURVIVING | byte-identical; numbering preserved so existing citations remain valid |
| §42 opening + specification-pack layout (through the code fence) | SURVIVING | byte-identical |
| §42 completion — specification review lifecycle (`DRAFT`/`PROPOSED`/`CHANGES_REQUESTED`/`ACCEPTED`/`REJECTED`; "accept with changes" = `CHANGES_REQUESTED`, never implementation authorization; no `ACCEPTED WITH CHANGES`) | NEWLY DRAFTED | **revision 2** replaces the initial draft's lifecycle wording |
| §42 completion — self-containment rule + §§37–40 non-weakening | NEWLY DRAFTED | retained from the initial draft with consistency edits |
| §43 ADRs record significant decisions + prospective project-wide numbering (specs and ADRs; historical preservation; registry `retriva-core/docs/governance/spec-adr-registry.yaml`; allocation before `PROPOSED`; collision detection; rejected/withdrawn retention; CI enforcement) | NEWLY DRAFTED | **revision 4** extends the initial §43 |
| §44 Scope is binding | NEWLY DRAFTED | unchanged from the initial draft (v1.0 §38 lineage; newly ratified text) |
| §45 Licensing boundary | NEWLY DRAFTED | unchanged from the initial draft (v1.0 §39 lineage; newly ratified text) |
| §46 Governance integrity (structural properties; red test blocks acceptance) | NEWLY DRAFTED | consistency edit: repair path now cites §47 |
| §47 Amendments — risk-gated reduction rule (risk analysis, compensating controls, stakeholder approval, migration/rollback, major version bump); permitted clarifications/equivalent-or-stronger replacements; complete-text amendment requirements; explicit unrecoverable-constitution recovery governance | NEWLY DRAFTED | **revision 3** replaces the initial strengthen-only ratchet; recovery requirements enumerated (history preservation; exhausted-recovery evidence; newly versioned draft; provenance; ratification; byte-for-byte installation; pins; no byte-identical-recovery claim) |
| §48 Constitutional verification | NEWLY DRAFTED | unchanged from the initial draft |
| Final invariant (substantive text) | NEWLY DRAFTED | ratified as proposed; identical in draft and candidate |
| Final ratification sentence ("Version 1.2 was ratified on 2026-10-03 and is installed byte-for-byte from the ratified text recorded in ADR-028.") | NEWLY DRAFTED | **candidate canonical only** — the pre-ratification draft omits it and makes no ratification/installation claim |

## Byte-identity and reproducibility evidence

- **Surviving-prefix comparison:** the draft is built from the
  committed v1.1 file; reverse-applying the two declared header
  amendments reproduces the original byte-for-byte (round-trip
  assertion; committed v1.1: 1035 lines, ends at the §42 code fence).
- **Candidate = draft + exactly two ratification substitutions:**
  (1) `**Status:** PROPOSED` → `**Status:** ACTIVE`; (2) appended
  final ratification sentence.  Programmatically asserted; both
  hashes are reproducible from the committed v1.1 plus the declared
  amendments and appended new text.
- **Structural markers:** sections 1–48 sequential; `## 47.
  Amendments`, `## 48. Constitutional verification`, `## Final
  invariant` present; neither artifact ends at a bare code fence; the
  draft contains no `ACCEPTED WITH CHANGES`, no strengthen-only
  ratchet wording, and no ratification/installation claim.

## Integrity-test pins (applied only at installation)

```text
EXPECTED_VERSION = "1.2"
EXPECTED_FINAL_INVARIANT_SUBSTR =
    "prevails until it is formally amended through section 47"
EXPECTED_SHA256 =
    8b2fb1fe7c26257bf92742d16e8debafd154f65975b10e5c5891d578685f69ca
```

Any ratified change to the text invalidates the candidate hash: the
provenance record, ADR-028, and pins are updated and the changed text
re-presented before installation.
