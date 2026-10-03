---
description: Retriva project constitution, the non-negotiable governing law for every retriva-* repository, specification, and change
alwaysApply: true
---

# Retriva Project Constitution

**Version:** 1.2  
**Date:** 2026-10-03  
**Status:** PROPOSED

**Canonical location:**  
`retriva-core/.agent/rules/retriva-constitution.md`

Every governed repository MUST reference this canonical constitution through
its `AGENTS.md` order-of-authority chain or another explicitly documented
mechanism.

The deployment setting `RETRIVA_CONSTITUTION`, when supported, MUST resolve to
this canonical document or to a verified copy with the same version and
content hash.

**Recovery note (v1.2):** version 1.1 was committed truncated midway through
section 42 and the accepted full text was not recoverable from any
authoritative source.  Version 1.2 carries every surviving section of v1.1
(sections 1 through 42, through the specification-pack layout) forward
verbatim, completes section 42, and adds newly drafted sections 43 through 48
and the final invariant.  Section-level provenance is recorded in
`retriva-core/docs/governance/constitution-v1.2-provenance.md`; the recovery
decision is ADR-028.  Version 1.2 becomes canonical upon owner ratification
recorded in ADR-028, installed byte-for-byte from the ratified text.

---

## 1. Mission

Retriva is a privacy-first enterprise conversational RAG system that provides
accurate, attributable answers grounded in authorized customer knowledge and
business data, subject to the customer's security, privacy, residency, and
data-sovereignty requirements.

Retriva is a multi-repository system comprising:

- **Retriva Core**, licensed under Apache-2.0, which provides the data plane:
  ingestion, parsing, chunking, embeddings, Qdrant vector storage, retrieval,
  reranking, LLM request construction, OpenAI-compatible chat APIs, and
  modular GraphRAG.

- **Retriva Gateway**, licensed under Apache-2.0, which provides the control
  plane and backend-for-frontend functions: policy enforcement,
  orchestration, identifier mapping, capability routing, and guardrails.

- **Retriva WebUI**, licensed under Apache-2.0, which provides the reference
  frontend.

- **Retriva Pro extensions**, distributed under proprietary terms, which may
  provide IAM integration, connectors, CRM Assistant, Web Research, email and
  messaging integrations, and other commercial capabilities.

- **Deployment tooling**, which provides supported containerized deployments
  and shared infrastructure such as Qdrant, PostgreSQL, Tika, Redis, and
  extension services.

Every specification, architecture decision, plan, implementation, test,
deployment, and document governed by this constitution MUST serve this
mission.

---

## 2. Scope

This constitution governs all current and future `retriva-*` repositories,
including:

- specifications;
- architecture;
- source code;
- public and internal APIs;
- extension contracts;
- tests;
- persistent schemas;
- migrations;
- deployment;
- security;
- privacy;
- observability;
- documentation;
- operational acceptance.

Feature-level constitutions and repository-level `AGENTS.md` files MAY impose
stricter or more specific constraints. They MUST NOT contradict or weaken
this constitution.

---

## 3. Normative language

The terms **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are
normative.

- **MUST** and **MUST NOT** express constitutional requirements.
- **SHOULD** and **SHOULD NOT** express strong defaults. A deviation requires
  documented justification and evidence that constitutional protections are
  preserved.
- **MAY** expresses permitted behavior.

Words such as "never", "always", "required", and "forbidden" have the same
normative force as MUST or MUST NOT when used in a governing statement.

---

## 4. Order of authority

The order of authority is:

1. **This constitution**
2. **Approved feature constitutions and repository `AGENTS.md` files**
3. **Accepted `spec.md`**
4. **Accepted `architecture.md` and Architecture Decision Records**
5. **Accepted `plan.md`, `tasks.md`, and `acceptance.md`**
6. **Code and deployed behavior**

More-specific governing documents implement more-general ones. They MUST NOT
contradict higher-authority documents.

Code is authoritative evidence of current runtime behavior. It is not
normatively superior to this constitution, an accepted specification, or an
ADR.

When code and governing documents disagree:

1. the disagreement MUST be recorded as a defect;
2. the code MUST be changed unless the governing requirement is explicitly
   amended through its required process;
3. a governing document MUST NOT be amended retroactively merely to
   legitimize an accidental or non-compliant implementation;
4. the code, tests, and governing documents MUST return to agreement within
   the same change whenever practical.

---

## 5. Handling conflicts and violations

When a requested change appears to conflict with this constitution, the agent
or contributor MUST stop the conflicting portion of the work and identify the
specific rule involved.

It MUST then propose one of:

1. a constitution-compliant implementation;
2. a formal amendment;
3. a temporary exception where this constitution permits one;
4. a narrower scope that avoids the conflict.

A violation MUST NOT be concealed through:

- undocumented feature flags;
- metadata fields;
- silent fallbacks;
- test exclusions;
- permissive defaults;
- hidden Pro dependencies;
- implementation-specific behavior not represented in the specification.

---

# Part I: Product law

## 6. Grounded substantive answers only

Every substantive factual answer MUST be grounded in evidence obtained from
an authorized Retriva source.

Authorized evidence sources MAY include:

- retrieved Knowledge Base content;
- authoritative relational business stores;
- approved tool results;
- approved qualification assessments;
- controlled external evidence explicitly permitted by the active workflow;
- user-provided content in the current authorized context.

The answer MUST identify the evidence class and MUST carry source references
where the response contract supports them.

When authorized sources do not contain sufficient information, Retriva MUST
say so explicitly.

The model MUST NOT present any of the following as customer-specific fact
without supporting evidence:

- parametric model knowledge;
- unsupported synthesis;
- assumptions;
- plausible inferences;
- provider-generated metadata;
- search snippets;
- unresolved assertions.

Operational responses such as tool results, workflow status, import
reconciliation, and error explanations MAY be grounded in authoritative
runtime or business-store data rather than Knowledge Base documents.

---

## 7. Identity-preserving documents

Every document MUST retain the identity assigned at upload or source
registration.

Retriva MUST NOT automatically merge, collapse, or deduplicate distinct
documents solely because their content is identical or similar.

User intent, lifecycle independence, tenant boundaries, deletion semantics,
and metadata correctness take precedence over storage economy.

Content hashes MAY be used for:

- processing caches;
- OCR caches;
- integrity checks;
- duplicate warnings;
- idempotency detection.

A content hash MUST NOT silently replace document identity.

---

## 8. Deterministic semantics by default

Given the same:

- normalized request;
- tenant and knowledge scope;
- source snapshot;
- configuration;
- provider versions;
- accepted policies;

Retriva MUST produce the same:

- business decisions;
- dispositions;
- ordering rules;
- state transitions;
- identity-resolution outcomes;
- authorization outcomes.

Stable tie-breakers MUST be used wherever scores can tie.

The following are exempt from byte-for-byte determinism:

- generated identifiers;
- timestamps;
- provider-generated prose;
- externally changing evidence;
- floating-point representation differences;
- nondeterminism imposed by external model providers.

These differences MUST NOT change deterministic business decisions without an
explicit, observable, and recorded cause.

Tests MUST replace nondeterministic providers with deterministic fakes,
mocks, or recorded fixtures and SHOULD assert semantic outcomes rather than
incidental formatting.

---

## 9. Control plane and data plane remain separate

The WebUI MUST communicate with Gateway rather than relying directly on Core
internal APIs.

Gateway is the policy and orchestration choke point for public frontend
traffic.

Core owns canonical ingestion, retrieval, and document-processing semantics.

Uploads MUST NOT implicitly cause:

- LLM calls;
- Web Research;
- qualification;
- campaign enrollment;
- paid-provider calls;
- canonical business mutations beyond the explicitly requested ingestion
  operation.

Every consequential action MUST be represented by an explicit workflow or
contract.

---

## 10. Frontend agnosticism

Documented public chat and ingestion APIs are the supported contracts.

Frontend-specific behavior MUST NOT be implemented in Core.

WebUI, Open WebUI, custom applications, and future interfaces MUST remain
replaceable without requiring changes to Core semantics.

Gateway MAY adapt public frontend contracts to Core contracts, but it MUST NOT
make Core depend on a specific frontend.

---

## 11. Model and provider agnosticism

Providers and models MUST be selected through validated configuration and
provider-neutral interfaces.

Model routing MUST NOT be inferred from:

- arbitrary user content;
- user-provided custom metadata;
- model-generated suggestions;
- untrusted request fields.

The currently accepted reranker policy is deployment-global, as defined by
the governing ADR.

Introducing tenant-specific, KB-specific, user-specific, or request-specific
reranker selection requires:

- an ADR;
- explicit security and residency analysis;
- cache-isolation analysis;
- compatibility assessment;
- deterministic tests;
- operational acceptance.

Model failure MUST NOT silently route confidential data to a provider, region,
or endpoint that is disallowed by the active policy.

---

## 12. Compatibility is additive

Existing supported public API clients MUST continue to work unchanged within
their declared compatibility window.

New capabilities MUST be additive and versioned.

Breaking changes require:

- a new explicitly versioned contract;
- a migration path;
- documented deprecation;
- compatibility tests;
- operational acceptance.

A public contract MUST NOT be broken in place merely because all in-tree
clients have been updated.

---

# Part II: Architecture law

## 13. LangChain-free Core

Retriva Core MUST NOT depend on LangChain.

A proposal to introduce LangChain requires a constitution amendment rather
than an ordinary ADR.

Extensions MUST NOT introduce a hidden LangChain dependency into Core through
shared packages or runtime coupling.

---

## 14. Vendor-neutral public contracts

Public and extension-facing models MUST use storage-neutral, provider-neutral
contracts.

Vendor-specific types, including the following, MUST NOT leak into public
APIs or extension SDK and SPI contracts:

- Qdrant payload objects;
- Neo4j or Memgraph driver types;
- PostgreSQL driver objects;
- cloud-provider SDK responses;
- model-provider request or response objects.

Storage and provider backends MUST be accessed through explicit protocols or
interfaces so they can be changed without altering public contracts.

---

## 15. Internal fields do not become public implicitly

Internal diagnostic, ranking, provider, cost, and provenance fields MUST NOT
become public API merely because an internal dictionary or model is
serialized.

Public response construction MUST use:

- explicit allowlists;
- typed public contracts;
- deliberate mapping functions.

Raw internal dictionaries MUST NOT be passed directly to public response
models unless the contract explicitly allows every field.

---

## 16. Ingestion converges on Core

Static, dynamic, connector-driven, and import-driven document ingestion MUST
enter through canonical Gateway and Core ingestion contracts.

No component outside Core may independently implement canonical:

- chunking;
- embedding;
- vector upsert;
- document-catalog semantics;
- deletion semantics;
- retrieval metadata propagation.

Connectors are source adapters. They fetch, normalize, map metadata, preserve
source identity, and submit through canonical contracts.

---

## 17. Extensions extend, never fork

Extensions MUST register capabilities through `RETRIVA_EXTENSIONS` and
approved registries.

Extension vocabularies MUST be namespaced, for example:

- `retriva:`
- `crm:`
- provider-specific prefixes.

Extensions MUST use Core-owned shared services rather than reimplementing
canonical services such as identity resolution or document ingestion.

The first implementation of a capability MUST NOT become an architectural
special case. Future connectors and extensions MUST fit the same contract or
amend the contract transparently.

---

## 18. Optional capabilities are safe by default

New optional capabilities MUST ship disabled by default.

When disabled, established behavior MUST remain unchanged.

When enabled, optional capability failures MUST be isolated according to the
accepted specification.

For example, optional graph-indexing failure MUST NOT corrupt or roll back a
successful vector ingestion unless the governing specification explicitly
requires atomic behavior.

Changing an accepted optional capability to enabled by default requires:

- an ADR;
- compatibility analysis;
- privacy and resource-impact analysis;
- rollback capability;
- operational acceptance.

---

## 19. Identifier ownership is explicit

Identifier ownership MUST be documented.

At minimum:

- Core owns `doc_id`, collections, and `kb_id` tags.
- Gateway owns mappings between public or UI identifiers and Core
  identifiers.
- PostgreSQL business domains own their canonical relational identifiers as
  declared by ADR.
- External source identifiers remain namespaced by source system.

No component may infer, reconstruct, or repurpose another component's
internal identifiers.

Identifiers MUST be treated as opaque unless their contract explicitly
defines structure and parsing semantics.

---

## 20. Every store of record is declared

Each persistent store MUST have an explicit ownership boundary documented by
ADR.

Currently:

- Qdrant is the vector system of record.
- PostgreSQL is the authoritative relational store for business identity,
  lifecycle, import, campaign, qualification, and audit domains when declared
  by the applicable ADR.
- Artifact storage owns uploaded source files and generated artifacts
  according to retention policy.

Introducing a persistent store or changing the ownership of existing data
requires:

- an ADR;
- backup and restore design;
- migration design;
- tenant-isolation analysis;
- deletion semantics;
- operational acceptance.

The same authoritative fact MUST NOT have two permanent systems of record.

---

# Part III: Data and knowledge law

## 21. Assertions, not unqualified facts

LLM-extracted knowledge MUST be modeled as evidence-backed assertions rather
than unqualified facts.

Assertions MUST support:

- confidence;
- provenance;
- observation time;
- temporal validity;
- lifecycle status;
- attribution status.

Applicable lifecycle states SHOULD include:

- active;
- superseded;
- retracted;
- invalidated;
- disputed.

Conflicting assertions MUST be preserved. They MUST NOT be silently
overwritten.

Accepted current values MUST remain traceable to the observations and
decisions that established them.

---

## 22. Mandatory provenance

Every entity, assertion, relationship, qualification result, provider
resource, and retrievable result MUST be traceable to its authorized source.

Chat answers MUST carry citations where the response contract supports them.

Business imports MUST preserve source lineage such as:

- source system;
- source file;
- import batch;
- source row;
- source field;
- mapping version;
- decision.

A result without required evidence or provenance references is a defect.

---

## 23. One logical graph per knowledge boundary

Each `(collection, kb_id)` scope has one logical graph.

Extensions MAY add namespaced semantic overlays to that graph.

Extensions MUST NOT create independent semantic graph silos that fragment the
same knowledge boundary.

Physical storage MAY vary by backend, but the logical ownership and
authorization boundary MUST remain singular and explicit.

---

## 24. System metadata and custom user metadata are distinct

System-owned metadata is typed and interpreted according to versioned
contracts.

System-owned metadata includes:

- tenant identity;
- collection;
- KB identity;
- document identity;
- lifecycle;
- provenance;
- authorization;
- security;
- source identity.

User-provided custom metadata is accepted as data, persisted at document
level, propagated according to the ingestion contract, and exposed for
authorized filtering and citation.

Core MUST NOT:

- assign hidden semantics to custom metadata;
- derive authorization policy from custom metadata;
- route providers based on custom metadata;
- reinterpret custom metadata as system metadata.

A custom field may become system-interpreted only through a namespaced,
versioned contract and compatibility process.

---

## 25. Web evidence is never a search snippet

Search snippets are URL-discovery aids only.

An authoritative web evidence item requires:

- controlled source retrieval;
- normalized extraction;
- source URL;
- retrieval timestamp;
- content hash;
- attribution;
- applicable freshness or lifecycle status.

Evidence lifecycle MUST distinguish at least:

- session-only evidence;
- persistent evidence admitted through standard ingestion and indexing.

Persistent admission MUST use the canonical ingestion contracts.

---

## 26. Data movement is idempotent and resumable

Every synchronization, retry, import, and crash-recovery path MUST use stable
source identity.

Stable source identity SHOULD include:

- source item ID;
- source revision;
- source namespace;
- content hash where appropriate.

Processing the same source item twice MUST NOT duplicate or lose canonical
data.

Long-running operations MUST use checkpoints when practical.

Cancellation MUST be cooperative.

Retry behavior MUST be bounded and MUST NOT multiply provider calls or
business mutations.

---

## 27. Initial synchronization is completeness-safe

For sources supporting watermarks or deltas, initial synchronization MUST use
an algorithm equivalent to:

1. capture baseline start watermark;
2. perform full baseline scan;
3. apply catch-up delta from the watermark;
4. save checkpoint;
5. activate only after catch-up completes.

For sources without reliable delta semantics, the connector specification
MUST define an equivalent no-gap activation protocol and prove it through
acceptance tests.

A source in baseline, catch-up, initialization, or reconciliation MUST NOT
present itself as complete.

---

## 28. Deletion is explicit

Remote deletion MUST default to soft deletion or deactivation unless a
governing policy explicitly requires hard deletion.

Hard deletion requires:

- explicit policy;
- authorization;
- audit;
- propagation design;
- treatment of derived data;
- confirmation or retention-rule enforcement.

Deletion MUST NOT be inferred from temporary source unavailability.

---

## 29. Data minimization is mandatory

Only data required for the declared workflow may be:

- parsed;
- transmitted;
- staged;
- persisted;
- indexed;
- sent to a model;
- included in an artifact.

Out-of-scope sensitive fields MUST be discarded or redacted before they enter
general-purpose staging, logging, model, or audit surfaces.

Data ignored for privacy reasons MAY retain only safe structural metadata such
as:

- record type;
- source row;
- ignored status;
- redaction reason.

---

## 30. Business-critical mutations are auditable

Business stores MUST keep append-oriented audit trails.

Risky operations require explicit review or approval according to governing
policy, including:

- identity merges;
- role changes;
- exclusions;
- canonical imports;
- qualification lifecycle transitions;
- campaign-audience commits;
- paid-provider requests.

Analysis and proposal generation MUST NOT mutate canonical business state.

A canonical or destructive mutation requires:

- validated identity;
- authorization;
- approved state where applicable;
- explicit intent;
- idempotency;
- transactional execution;
- reconciliation;
- audit.

---

# Part IV: Security and privacy law

## 31. Data sovereignty by design

On-premises, hybrid, EU-restricted, sovereign-cloud, and confidential-
computing deployments are first-class architectural models.

No component may assume:

- a trusted third-party cloud;
- permission to send data outside the customer boundary;
- permission to use a global endpoint;
- permission to cross regions;
- permission to retain provider data.

Regional requirements MUST be enforced by endpoint, provider, configuration,
or platform policy.

Failure of an allowed regional route MUST NOT trigger fallback to a
disallowed endpoint, provider, or region.

---

## 32. Security trimming before return

No record or derived result may cross an unauthorized tenant, knowledge, or
business-data boundary.

This includes:

- documents;
- chunks;
- embeddings;
- graph objects;
- assertions;
- relationships;
- organizations;
- identifiers;
- addresses;
- campaign records;
- qualification records;
- import records;
- provider resources;
- audit events.

Authorization filters MUST be applied in the authoritative store query or
before materialization into an externally visible result.

Filtering only after an unauthorized result has been assembled is
insufficient.

Every tenant-owned persistent record MUST carry `tenant_id` from its first
migration.

---

## 33. No content leakage into operational surfaces

Document content, retrieved chunks, prompts, answers, evidence bodies,
embedding vectors, and raw provider exceptions MUST NOT appear in:

- logs;
- metrics;
- traces;
- health endpoints;
- exception messages;
- crash reports;
- uncontrolled build outputs;
- uncontrolled test artifacts.

User-authorized product records and exports MAY contain content only where
their declared schema and purpose require it.

Such records inherit:

- tenant isolation;
- authorization;
- retention;
- deletion;
- encryption;
- audit requirements.

Sanitization MUST occur before data crosses into operational or observability
surfaces.

Diagnostic identifiers MUST be opaque, redacted, or hashed where necessary.

---

## 34. Secrets are referenced, never stored

Credentials MUST come from:

- environment variables;
- mounted secret files;
- secret backends;
- workload identity;
- another approved secret mechanism.

Persistent records MUST store only secret references when persistence is
needed.

Secrets MUST NOT appear in:

- database payloads;
- checkpoints;
- state files;
- telemetry;
- logs;
- exception messages;
- URL query strings;
- browser storage;
- run summaries;
- artifacts.

Provider cache fingerprints MUST exclude secret values.

---

## 35. Egress is bounded and SSRF-safe

Outbound retrieval MUST enforce:

- protocol restrictions;
- domain allow and block policies;
- DNS and IP validation;
- redirect limits;
- private-address protections;
- size limits;
- time limits;
- rate limits;
- content-type checks;
- bounded retries.

No arbitrary URL crawling is permitted.

Provider-neutral fetch contracts MUST be used where applicable.

---

## 36. AuthN/AuthZ is pluggable; insecure exposure is forbidden

Identity-provider integration is provided through extensions or deployment
configuration.

Core remains provider-agnostic.

A no-auth mode MAY be supported only as an explicit local-development or
otherwise isolated deployment profile.

No-auth mode:

- MUST bind to a restricted network boundary;
- MUST be identified clearly in health and startup output;
- MUST NOT be the implicit production default;
- MUST NOT disable store-level tenant isolation.

Tenant isolation, database security, service-to-service authorization, and
network-boundary requirements remain applicable regardless of identity
provider.

Trusted service identity MUST NOT rely solely on an externally forgeable
header.

---

# Part V: Verification law

## 37. Done means accepted, not code-complete

A feature is done only when its acceptance criteria pass.

Code completion is not acceptance.

Operational acceptance is a separate mandatory gate and MUST include, where
applicable:

- a live run through the deployed interface;
- representative data;
- inspection through authoritative stores;
- persistence across container recreation;
- repeated execution proving semantic idempotency;
- failure-path validation;
- reconciliation.

A feature MUST NOT be called operationally complete based only on unit tests
or local service calls.

---

## 38. Phase gating

The next implementation phase MUST NOT begin until the current phase's
required acceptance gate passes.

Code-complete status does not unlock the next phase.

A phase MAY proceed in parallel only when the accepted plan explicitly proves
that the phases do not depend on one another and do not weaken rollback or
acceptance.

---

## 39. Every requirement is testable

Functional and non-functional requirements MUST have verifiable acceptance
criteria.

Deterministic tests are the default.

They SHOULD use:

- mock providers;
- deterministic fakes;
- recorded fixtures;
- containerized PostgreSQL;
- controlled Qdrant instances.

Tests that access real external providers MUST be:

- explicitly marked;
- opt-in;
- excluded from ordinary deterministic CI;
- based on non-confidential synthetic data;
- cost-bounded;
- region-bounded where required.

---

## 40. Regressions are protected

Existing accepted tests MUST continue to pass.

Intentional behavior changes MUST update:

- specification;
- architecture where applicable;
- implementation;
- tests;
- documentation.

Changes affecting parsing, chunking, embedding, retrieval, or multilingual
behavior MUST rerun applicable bilingual and cross-language regression
validation.

Baseline failures MUST be identified explicitly. New changes MUST NOT hide
new failures inside an existing failure count.

---

## 41. Internal observability stays internal

Debug endpoints, profilers, internal status endpoints, and diagnostic fields
are not public APIs.

They MUST:

- remain bounded;
- remain sanitized;
- disclose no secrets or content;
- be protected by network or authorization boundaries appropriate to the
  deployment;
- never become an implicit compatibility contract.

Publishing an internal service port is a security-relevant deployment
decision and MUST be documented.

---

# Part VI: Change-process law

## 42. Spec before code

Substantial work MUST begin with a numbered, self-contained specification
pack.

A standard pack contains:

```text
specs/NNN-name/
    spec.md
    architecture.md
    plan.md
    tasks.md
    acceptance.md
    openapi.yaml, when the public API changes
```

A substantial change MUST NOT begin implementation before its specification
pack carries the status `ACCEPTED`.

The specification review lifecycle is:

- `DRAFT`: not yet presented or still under revision;
- `PROPOSED`: presented for review;
- `CHANGES_REQUESTED`: reviewed but requiring revision;
- `ACCEPTED`: approved and authorized to proceed through its defined phase
  gates;
- `REJECTED`: declined.

An owner decision of "accept with changes" records `CHANGES_REQUESTED`, not
`ACCEPTED`. After the required changes are applied, the pack MUST be
re-presented. Implementation is authorized only after the revised pack is
explicitly marked `ACCEPTED`.

An accepted pack is reopened only through the same class of review that
accepted it, never silently.

A pack is self-contained: it states its own scope, decisions, and acceptance
criteria, and cites its governing documents by number and path. A pack MAY
carry supplementary evidence documents, for example a source-evidence
inventory, alongside the required files; evidence documents never weaken the
normative `spec.md`.

Implementation phases, acceptance gates, and the done-meaning-accepted rule
are governed by sections 37 through 40; no pack may weaken them.

## 43. ADRs record significant decisions

Significant architectural, persistence, provider, security, privacy,
compatibility, tenancy, and deployment decisions MUST be recorded in a
numbered Architecture Decision Record in the established project-wide ADR
series before implementation.

An ADR states its status (`PROPOSED` -> `ACCEPTED` / `REJECTED`, later
`SUPERSEDED`), its context, its decision, and its consequences.  Decision
numbers are unique across the project series.  An accepted ADR is amended or
superseded only by another ADR, never edited in place after acceptance.

A decision recorded in two places is a defect: one decision, one ADR, one
authoritative explanation.

Specifications and ADRs each use ONE project-wide numbering sequence for
future artifacts.  The project-wide numbering requirement applies
prospectively from Constitution v1.2.

Existing accepted specifications and ADRs retain their historical numbers
and paths. They MUST NOT be renumbered solely to conform to this rule.

A canonical project-wide registry allocates future numbers and records the
repository and path of every governed specification and ADR. Allocation MUST
detect collisions before a new artifact is proposed.

The registry is a version-controlled file at
`retriva-core/docs/governance/spec-adr-registry.yaml` with separate
`specifications` and `adrs` sections; each entry records the number (zero-
padded three digits, no prefix for specifications — `NNN`; the `adr-NNN`
prefix is reserved for ADR file names), title, repository, artifact path,
status (`allocated` / `proposed` / `accepted` / `superseded` / `rejected` /
`withdrawn`), allocation date, and supersession where applicable.

Allocation procedure: a governed change adds the registry entry BEFORE the
artifact is first presented as `PROPOSED`; the number is then bound to that
artifact and is never silently reused.  A rejected or abandoned number keeps
its registry entry with status `rejected` or `withdrawn` (released numbers
are never re-allocated).  A deterministic registry check in continuous
integration MUST fail on duplicate future allocations, on a `PROPOSED` or
`ACCEPTED` artifact without a registry entry, and on registry entries whose
artifact path does not exist.  The registry itself is governed by this
constitution's change process (section 42).

## 44. Scope is binding

The scope accepted in a specification pack is binding.  Implementation MUST
deliver exactly the accepted scope: no silent additions, no silent omissions,
no substitutions.  Scope changes require the same specification and
acceptance process as the original change.  Discovery of necessary
out-of-scope work stops the affected portion and reopens governance, never
the code alone.

## 45. Licensing boundary

Retriva Core, Retriva Gateway, and Retriva WebUI are licensed under
Apache-2.0.  Pro extensions and their interfaces remain under the
proprietary Retriva Pro terms.  Proprietary code, contracts, or content MUST
NOT leak into Apache-2.0 surfaces, and no Apache-2.0 surface may acquire a
hidden proprietary dependency.  Every repository declares its license and
keeps the boundary auditable.

## 46. Governance integrity

The canonical constitution MUST remain structurally complete and verifiable:

- valid YAML frontmatter with `alwaysApply: true`;
- the declared version;
- every numbered section present and in sequence;
- the amendment section, the verification section, and the final invariant
  present;
- no premature end of file.

A deterministic integrity test MUST verify these properties and MUST NOT be
silenced, skipped, or marked expected-failure.  A red integrity test blocks
governance acceptance of every pack and phase until the defect is repaired
through the amendment and recovery process of section 47.

## 47. Amendments

An amendment MUST NOT reduce the effective protection of compatibility,
security, privacy, data sovereignty, tenant isolation, auditability, or
verification without:

- explicit risk analysis;
- documented compensating controls;
- affected-stakeholder approval;
- migration and rollback plans;
- a major constitution version bump.

Clarifications, contradiction corrections, recovery of incomplete text, and
replacement by demonstrably equivalent or stronger controls are permitted.

Every amendment requires complete new text, an ADR, provenance, explicit
owner ratification, byte-for-byte canonical installation, and updated
integrity pins in the same governed change.

When the canonical text is damaged and no authoritative copy can be
recovered, reconstruction from memory, inference, or another version is
forbidden.  Recovery of an unrecoverable constitution MUST:

- preserve the defective version in history, unchanged;
- record the evidence that authoritative recovery was exhausted;
- produce a newly versioned complete draft;
- record section-level provenance distinguishing surviving text from new
  text;
- obtain explicit owner ratification of the complete new text;
- install the ratified text byte-for-byte as canonical;
- update the integrity pins in the same governed change;
- never claim that reconstructed text is a byte-identical recovery of the
  damaged version.

## 48. Constitutional verification

Every governed repository MUST reference the canonical constitution through
its `AGENTS.md` order-of-authority chain or another explicitly documented
mechanism, and every governed document cites constitution sections by their
canonical numbers.

Where a deployment setting (for example `RETRIVA_CONSTITUTION`) resolves the
constitution, it MUST resolve to the canonical document or to a verified copy
with the same version and content hash.  Verification is deterministic: the
integrity test of section 46 MUST pass in the repository that owns the
canonical file, and governed repositories may add equivalent checks for
verified copies.

---

## Final invariant

This constitution is the single highest authority for every `retriva-*`
repository, specification, architecture decision, plan, implementation,
test, deployment, and document.  Nothing governed by it may weaken, bypass,
silently reinterpret, or contradict it; every governed artifact MUST remain
traceable to it; and where any artifact disagrees with it, this document
prevails until it is formally amended through section 47.  A defect between
governed artifacts and this constitution is recorded and corrected, never
concealed, never legitimized after the fact.
