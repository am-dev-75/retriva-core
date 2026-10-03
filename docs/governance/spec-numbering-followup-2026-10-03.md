# Spec-numbering governance follow-up (F-2, F-3)

- **Date:** 2026-10-03 (updated after the Constitution v1.2 recovery
  revision round, ADR-028)
- **Class:** governance defect records (kept separate from ADR-027
  and Spec 023; no historical renumbering performed)
- **Status:** F-2 **governed** by the prospective rule of Constitution
  v1.2 §43 (ratification pending); F-3 **governed** by the same rule
  with an explicit disposition to record in the registry when
  implemented

## Update (2026-10-03): disposition under Constitution v1.2 §43

Constitution v1.2 §43 (newly drafted, ADR-028) makes the numbering
requirement **prospective from v1.2** and mandates a canonical
project-wide registry:

- one project-wide sequence for future specifications and one for
  future ADRs;
- existing accepted specifications and ADRs retain their historical
  numbers and paths and MUST NOT be renumbered solely to conform;
- the registry is `retriva-core/docs/governance/spec-adr-registry.yaml`
  (separate `specifications`/`adrs` sections; number, title,
  repository, path, status, allocation date, supersession);
- allocation occurs before an artifact is first presented as
  `PROPOSED`; numbers are never silently reused; rejected/withdrawn
  numbers keep their entries; a deterministic CI check fails on
  duplicate future allocations, missing registry entries for
  `PROPOSED`/`ACCEPTED` artifacts, and dangling paths.

Therefore:

- **F-2** is governed by the constitutional rule; the remaining open
  item is **implementation of the registry and the CI check after
  v1.2 ratification** (the gateway's local 001-series stays
  grandfathered as historical, recorded in the registry with its
  repository/path).
- **F-3** is governed by the same rule: no silent renumbering; the
  `014` collision is recorded in the registry when implemented, with
  an explicit owner decision on which artifact the number represents
  going forward (recommendation recorded below stands for that
  decision).

## F-2: Project-global vs repository-local spec and ADR numbering

### Observed state (evidence)

- `retriva-core/specs/`: 001–018 (one number, `017`, unused; `014`
  collides — see F-3).
- `retriva-crm-assistant/specs/`: 019–022 — **continues the core
  series** (project-global numbering).
- `retriva-gateway/specs/`: `001-hybrid-intent-routing` — **restarts
  numbering at 001**, and `retriva-gateway/docs/adr/`: 0001–0002 — a
  parallel ADR series (crm-assistant holds adr-001–adr-026; ADR-026
  references "retriva-gateway Spec 001 / ADR-0002").
- `retriva-messaging-extension`, `retriva-webui`,
  `retriva-local-containerized-deployment`: no spec/ADR series
  observed yet.

### Defect / ambiguity

Two concurrent numbering conventions (project-wide shared series vs
repository-local series) with no collision detection: nothing
prevents a future `retriva-crm-assistant/specs/001` or a second
`specs/001` in another repository from silently colliding with the
project-wide series, and readers cannot tell which convention a
number belongs to without checking the repository path.

### Disposition

Governed prospectively by Constitution v1.2 §43 (registry +
allocation-before-PROPOSED + CI collision detection).  The gateway's
local series is preserved as historical (no renumbering).  Open
item: **implement the registry file and the CI check after
ratification** (allocate and record all existing artifacts, including
the grandfathered gateway series).

## F-3: Core `specs/014` file/directory collision

### Observed state (evidence)

- `retriva-core/specs/014-structured-citations-v2.md` (file)
- `retriva-core/specs/014-user-metadata-ingestion-v1/` (directory
  pack; the repository `AGENTS.md` order-of-authority cites
  `specs/014-user-metadata-ingestion-v1/spec.md`)

Two distinct accepted artifacts share the number `014`.

### Defect

Ambiguous citation ("Spec 014" is ambiguous); tooling and humans
cannot resolve the reference deterministically; the collision also
breaks any `specs/NNN-*` globbing assumption.

### Disposition

No silent renumbering.  When the registry is implemented, record both
artifacts with an explicit owner decision on the canonical owner of
`014` (standing recommendation: the accepted directory pack
`014-user-metadata-ingestion-v1`, cited by `AGENTS.md`; the other
recorded as a historical colliding entry, or migrated to a new
registry-allocated number in a future governed change with
cross-references updated).  The CI check enforces no future
collisions.

## Cross-references

- Constitution v1.2 §43 (candidate canonical, ADR-028); Spec 023 §18;
  ADR-027 "Follow-ups".
- The constitution integrity record (F-1) is separate:
  `constitution-integrity-2026-10-03.md`.
