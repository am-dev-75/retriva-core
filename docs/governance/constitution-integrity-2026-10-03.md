# Constitution integrity record — canonical v1.1 truncation

- **Date:** 2026-10-03
- **Defect class:** governance-integrity (blocking for constitution repair;
  recorded separately from Spec 023 changes)
- **File:** `retriva-core/.agent/rules/retriva-constitution.md`
- **Status:** BLOCKED — accepted v1.1 text not recoverable unambiguously;
  canonical file left untouched per the governing task instruction
  ("do not reconstruct it from memory").  **Path B invoked**
  (2026-10-03 governance task, decision tree §1): no
  owner-authoritative complete v1.1 source was provided; the canonical
  file remains unmodified; this defect report and the deliberately
  failing integrity checks are preserved; **owner restoration or
  formal recovery and re-ratification is required** and remains the
  only blocking gate for Spec 023 / ADR-027 acceptance and Phase 1
  authorization.  A task instruction, acceptance request, or
  implementation schedule is not an authoritative source for missing
  constitutional content.
  **Update (2026-10-03):** formal recovery drafted per the owner's
  instruction — **Constitution v1.2** (ADR-028), carrying §§1–42 of
  v1.1 forward byte-for-byte with newly drafted §§43–48 + final
  invariant, re-presented for owner ratification
  (`docs/governance/constitution-v1.2-draft.md`).  The canonical v1.1
  file remains untouched; the integrity checks remain deliberately
  failing until the ratified v1.2 is installed (runbook:
  `constitution-v1.2-reference-migration.md`).
  **RESOLVED (2026-10-03):** the owner ratified Constitution v1.2
  (revision round applied first: PROPOSED/ACTIVE status handling,
  §42 lifecycle correction, §47 amendment-policy correction, §43
  prospective numbering; ratification hash
  `8b2fb1fe7c26257bf92742d16e8debafd154f65975b10e5c5891d578685f69ca`).
  The candidate canonical was installed byte-for-byte at
  `.agent/rules/retriva-constitution.md` (installed hash verified
  equal to the ratified hash before any status change); the integrity
  test was pinned (version 1.2, final-invariant substring, full-file
  SHA-256) and passes 6/6.  The defective v1.1 is preserved unchanged
  in Git history.  This defect record is closed; ADR-028 is
  `ACCEPTED`.  **Phase 1 implementation remains unauthorized.**

## Evidence

- Working-tree file: 1035 lines, frontmatter `Version: 1.1`,
  `alwaysApply: true`; ends mid–section 42 at the specification-pack code
  fence. No amendment section, no verification section, no final
  invariant.
- HEAD commit `3c949c2` ("Constitution reviewed by external LLM",
  2026-09-26) introduced the truncation: diffstat `1029 insertions(+),
  283 deletions(-)` against the 289-line v1.0; the committed v1.1 is the
  same truncated 1034-line blob. The working tree matches HEAD (no
  local modification).
- Complete predecessor: blob `83cb6a4` (v1.0, 289 lines) — a different,
  complete document (unnumbered top-level sections; ends with an
  `## Amendment` section). v1.1 restructured and expanded v1.0; v1.0 is
  NOT the accepted v1.1 and is not a substitute for it.

## Recovery attempts (all exhausted)

1. Full git history of the path (`git log --follow`): two commits —
   `e45c3c6` (v1.0, complete) and `3c949c2` (v1.1, truncated).
2. All refs and remote branches (`git rev-list --all --objects`):
   every historical blob of the path enumerated; only v1.0 (complete,
   superseded) and v1.1 (truncated) exist. Smaller predecessors
   (4–36 lines) are feature-scoped documents.
3. Unreachable/dangling objects (`git fsck --full --unreachable`):
   4 unreachable commits, each carrying the same truncated 1034-line
   v1.1 blob; no unreachable constitution-like blobs.
4. Filesystem: `retriva-constitution.md` exists only at the canonical
   path; no content copies of the v1.1 text anywhere under
   `/mnt/devel/retriva` (documents, configs, specs).
5. Deployment: no `RETRIVA_CONSTITUTION` copy or hash pin exists in
   `retriva-local-containerized-deployment`.

## Consequence

Restoring the accepted v1.1 requires the owner to supply the accepted
source (e.g., the reviewed full text from the external-LLM review of
2026-09-26 or an authoritative copy). Until then:

- the canonical file remains as-is (readable through §42);
- the deterministic integrity check
  (`retriva-core/tests/test_constitution_integrity.py`) FAILS,
  identifying: missing amendment section, missing verification section,
  missing final invariant, and premature EOF — by design, so the
  defect cannot be silently carried forward;
- after the owner restores the complete accepted v1.1, the check turns
  green when the expected version, section sequence, required
  governance sections, final-invariant marker, and (optionally) the
  pinned full-file SHA-256 match.

## Owner action required

1. Provide the accepted Constitution v1.1 full text (or its canonical
   location/hash).
2. Restore it into `.agent/rules/retriva-constitution.md` (or instruct
   the code agent to, from the supplied source only).
3. Pin the final-invariant marker and, optionally, the full-file
   SHA-256 in `tests/test_constitution_integrity.py`
   (`EXPECTED_FINAL_INVARIANT_SUBSTR` / `EXPECTED_SHA256`).

## Governance follow-up references

- Follow-up F-1 (this record): constitution integrity repair.
- Follow-up F-2: project-global vs repository-local spec numbering and
  collision detection (gateway series 001… vs core/crm series; recorded
  in Spec 023 §24 and ADR-027).
- Follow-up F-3: Core `specs/014` file/directory name collision
  (`014-structured-citations-v2.md` vs `014-user-metadata-ingestion-v1/`).
