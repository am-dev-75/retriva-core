# Constitution v1.2 — repository-reference migration and installation runbook

- **Date:** 2026-10-03 (ratification revision round 1 applied)
- **Decision:** ADR-028 (ratification-gated installation)
- **Scope:** governance-only; no application code or runtime
  configuration changes.  All existing §-number citations remain valid
  because v1.2 keeps §§1–42 numbering and appends §§43–48 + the final
  invariant.

## Reference inventory and required actions

| # | Reference | Form | Action at installation |
|---|---|---|---|
| 1 | `retriva-core/.agent/rules/retriva-constitution.md` | canonical file | **Install**: copy the candidate canonical byte-for-byte; verify SHA-256 `8b2fb1fe7c26257bf92742d16e8debafd154f65975b10e5c5891d578685f69ca` BEFORE any other status change |
| 2 | `retriva-core/tests/test_constitution_integrity.py` | integrity check | **Pin** (same change): `EXPECTED_VERSION = "1.2"`; `EXPECTED_FINAL_INVARIANT_SUBSTR = "prevails until it is formally amended through section 47"`; `EXPECTED_SHA256 = 8b2fb1fe…f69ca`; run and require all checks green (never skip/xfail/weaken/delete) |
| 3 | `retriva-core/docs/governance/constitution-integrity-2026-10-03.md` | F-1 defect record | **Close**: status → resolved via ratified v1.2 (ADR-028); history preserved |
| 4 | `retriva-core/AGENTS.md` | order-of-authority citation (path) | None (path-based, version-agnostic) |
| 5 | `retriva-core/src/retriva/config.py` (`retriva_constitution` setting) | canonical-path resolution | None (path-based).  Optional future work (out of scope): hash verification of "verified copies" per v1.2 §48 |
| 6 | `retriva-gateway/specs/001-hybrid-intent-routing/` (+ `docs/adr/0002…`, `eval/…/methodology.md`) | path + §-number citations (§4, §5, §11, §18, §41, …) | None (numbers unchanged).  GF-001 (§11 dangling reranker ADR reference) remains a separate open follow-up |
| 7 | `retriva-crm-assistant/docs/acp-cohorts-phase1-delivery.md` | path citation | None |
| 8 | `plan/use_constitution.md` (owner prompt template) | path citation | None |
| 9 | Spec 023 pack + ADR-027 | governance-context notes; status lines | **Update** at installation (steps 10): mark Spec 023 `ACCEPTED` and ADR-027 `ACCEPTED`, recording production weight values, CCO family weights, and verdict thresholds as intentionally unconfigured (four-score runtime fail-closed until their approved artifacts are active).  Before installation Spec 023 stays `CHANGES_REQUESTED` and ADR-027 `PROPOSED` (corrected §42 lifecycle; the `ACCEPTED WITH CHANGES` status does not exist) |
| 10 | `retriva-core/docs/governance/constitution-v1.2-provenance.md` | provenance record | **Update**: record the ratification event and the installed hash |
| 11 | Deployment (`retriva-local-containerized-deployment`) | no `RETRIVA_CONSTITUTION` copy exists (verified); the core container resolves the canonical path from its image | None now; the next core image rebuild picks up the installed v1.2 automatically |
| 12 | `retriva-core/docs/governance/spec-numbering-followup-2026-10-03.md` | F-2/F-3 records | **Update** at installation: record that the prospective rule is constitutional (v1.2 §43); registry + CI implementation remains an open follow-up |

## Installation runbook (executed only after owner ratification)

1. Verify the owner ratification references the exact candidate
   canonical hash
   (`8b2fb1fe7c26257bf92742d16e8debafd154f65975b10e5c5891d578685f69ca`).
2. Verify ADR-028 is the ratified recovery decision.
3. Copy the candidate canonical Constitution v1.2 byte-for-byte to:
   `retriva-core/.agent/rules/retriva-constitution.md`.
4. Verify the installed SHA-256 **before any other governance status
   change**.  **Rollback:** if the hash differs, abort and restore the
   prior canonical file; mark no dependent artifact accepted.
5. Update and pin the integrity test:
   - `EXPECTED_VERSION = "1.2"`;
   - final-invariant substring
     `"prevails until it is formally amended through section 47"`;
   - full-file SHA-256
     `8b2fb1fe7c26257bf92742d16e8debafd154f65975b10e5c5891d578685f69ca`.
6. Run the constitution integrity test and require all checks to pass.
7. Update governed repository references only where a version/hash pin
   exists (inventory above).
8. Mark ADR-028 `ACCEPTED`.
9. Close F-1 (`constitution-integrity-2026-10-03.md`).
10. Mark Spec 023 and ADR-027 `ACCEPTED`, recording that production
    policy values are intentionally unconfigured and that four-score
    runtime behavior remains fail-closed until their approved
    artifacts are active.
11. Run relevant governance-reference tests.
12. Verify working trees and record commits.
13. Stop before Phase 1 implementation.
