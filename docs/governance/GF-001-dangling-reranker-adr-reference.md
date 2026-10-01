# GF-001: Constitution §11 references a governing reranker ADR that does not exist

- **Identifier:** GF-001 (governance follow-up record)
- **Type:** governance defect — dangling normative reference
- **Status:** OPEN (awaiting owner resolution decision)
- **Recorded:** 2026-10-01
- **Discovered during:** retriva-gateway spec pack `001-hybrid-intent-routing` /
  ADR-0002 governance review (hybrid intent routing)
- **Affected document:** `retriva-core/.agent/rules/retriva-constitution.md` §11
  ("Model and provider agnosticism"), canonical constitution v1.1
- **Scope of this record:** governance documentation only. Resolving it changes no
  application code, tests, deployment configuration, prompts, routes, metrics, or
  container images unless the owner's chosen resolution explicitly requires it.

## Defect

Constitution §11 states:

> The currently accepted reranker policy is deployment-global, as defined by
> the governing ADR.

No governing reranker ADR exists:

- `retriva-core/docs/adr/` does not exist (verified 2026-10-01);
- the reranking subsystem is governed only by `retriva-core/docs/reranking.md`
  (accepted engineering documentation: deployment-global provider/model
  selection through the global settings system, provider-neutral
  `retriva.protocols.Reranker` interface, strict startup validation, EU-region
  enforcement with no fallback), by the implementation, and by its tests.

The constitutional phrase "the governing ADR" therefore dangles: §11 cites an
order-of-authority level 4 document (ADR, constitution §4) as the definition
of a level 1 constitutional statement, and that document cannot be inspected.

Under constitution §4 (disagreements between governing documents and the
documented state MUST be recorded as defects) this record is filed without
amending the constitution: an amendment is a separate governance process and
was not part of the task that discovered the defect.

## Verified facts

- The reranker implementation, its configuration surface, and
  `docs/reranking.md` agree on the substantive policy the constitution
  summarizes: **deployment-global** selection, provider-neutral interface,
  EU-region enforcement without fallback, strict startup validation. No
  behavioral contradiction between code and the constitution's description
  was found.
- The defect is documentary: the referenced ADR file does not exist, so the
  constitutional claim rests on evidence that is not locatable at its cited
  level of authority.

## Required resolution (owner must choose exactly one)

1. **Create the missing reranker ADR (recommended).** An ADR in this
   repository (`docs/adr/NNNN-reranking.md` or the owner's chosen numbering)
   documenting the accepted reranking implementation, configuration surface,
   security model, provider-neutral interface, EU-routing policy, and
   operational acceptance, accepted through the normal ADR process. §11's
   reference then resolves to a real governing document.
2. **Amend the dangling reference through the proper governance process.** A
   constitution amendment, or an explicitly accepted reference correction,
   that points §11 at the document that actually governs reranking policy
   today (`docs/reranking.md`, if the owner formally accepts it as the
   governing document for this purpose).

## Constraints

- This record MUST NOT be resolved by silently editing the reference inside
  Gateway ADR-0002 or inside any other ADR.
- Gateway ADR-0002 (hybrid intent routing) MUST NOT be expanded to govern
  reranking.
- No reranker behavior, configuration, or code may change as part of
  resolving this record unless the chosen resolution explicitly requires it.
- The constitution itself MUST NOT be amended as part of the hybrid-intent-
  routing governance task; any amendment follows its own process (option 2
  above).
- `retriva-core/docs/reranking.md` is not a substitute for an ADR unless an
  accepted governance decision says so.

## Cross-references

- `retriva-gateway/docs/adr/0002-hybrid-intent-routing.md` — records GF-001 as
  a newly discovered governance defect; does not depend on its resolution and
  does not resolve it.
- `retriva-gateway/specs/001-hybrid-intent-routing/spec.md` — defects
  recorded section references GF-001.
