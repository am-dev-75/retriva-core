---
description: Retriva project constitution — non-negotiable law for every retriva-* repository, spec, and change
alwaysApply: true
---

# Retriva Project Constitution

v1.0 — 2026-09-26
Canonical location: `retriva-core/.agent/rules/retriva-constitution.md` (referenced by repo `AGENTS.md` order-of-authority chains and the `RETRIVA_CONSTITUTION` setting).

## Mission

Retriva is a privacy-first, enterprise conversational RAG system that gives
accurate, attributable answers grounded strictly in customer-owned knowledge
bases, under the customer's data-sovereignty requirements.

It is a multi-repo system:

- **Retriva Core** (Apache-2.0, OSS) — the data plane: ingestion, chunking,
  embeddings, Qdrant vector storage, retrieval and reranking, LLM request
  construction, OpenAI-compatible chat API, modular GraphRAG.
- **Retriva Gateway** (Apache-2.0, OSS) — the control plane / BFF: policy,
  orchestration, identifier mapping, guardrails.
- **Retriva WebUI** (Apache-2.0, OSS) — the reference frontend.
- **Retriva Pro extensions** (proprietary) — IAM (Entra), MediaWiki connector,
  email-agent connector, CRM assistant, web research, messaging.
- **Deployment tooling** — local containerized deployment (Qdrant, Tika,
  Redis, and extension services).

Every spec, plan, and change serves this mission. Everything else in this
document is law about how.

## Scope

This constitution governs all `retriva-*` repositories, current and future,
including specs, architecture, code, tests, deployment, and documentation.

Feature-level constitutions (e.g. SDD-pack `memory/constitution.md` files)
and repository `AGENTS.md` files refine this document. They may add stricter
constraints; they may never contradict it.

## Order of authority

1. **This constitution** — non-negotiable project law.
2. **Feature constitutions and repo `AGENTS.md`** — stricter refinements and
   task-specific working orders.
3. **`spec.md`** — what and why: goal, scope, requirements.
4. **`architecture.md` and ADRs** — how, and the recorded decisions behind it.
5. **`plan.md` and `tasks.md`** — execution sequencing.
6. **Code** — what the system actually does today.

More specific documents implement more general ones; none may contradict this
constitution. Where code and documents disagree, treat the disagreement as a
defect and fix one or the other in the same change — never let them drift.

## Non-negotiable principles

### Product law

### 1. Grounded answers only
Responses are generated strictly from retrieved Knowledge Base content. When
the KB does not contain sufficient information, the system says so
explicitly. Synthesis beyond the KB is a defect, not a feature.

### 2. Identity-preserving documents
Every document keeps the identity it was given at upload time. Retriva never
collapses, merges, or deduplicates documents automatically based on content.
User intent, lifecycle independence, and metadata correctness win over storage
economy.

### 3. Nearly-deterministic behavior
Given the same request and identical Knowledge Bases, the system produces the
same output. Nondeterminism is admitted only where an external LLM provider
forces it, and tests neutralize it with deterministic providers or mocks.

### 4. Control plane and data plane are separate
WebUI talks only to Gateway; Gateway is the policy choke point; Core is the
system of record for documents, chunks, metadata, and retrieval. At no point
do uploads implicitly cause LLM calls.

### 5. Frontend agnosticism
The OpenAI-compatible chat API and the documented ingestion APIs are the
public contract. No frontend-specific logic lives below Gateway; any frontend
(WebUI, Open WebUI, custom) is replaceable without Core changes.

### 6. Model agnosticism
Providers and models are selected through configuration. The reranker is
deliberately global — one provider and one model for every knowledge base,
user, and customer — and there is no per-KB reranker policy.

### 7. Compatibility is additive
Existing public-API clients (e.g. `ingestion_api_v1`) must keep working
unchanged. New capabilities are additive and versioned; breaking changes
require a new, explicitly versioned contract — never an in-place break.

### Architecture law

### 8. LangChain-free
Retriva Core does not depend on LangChain. Keep it that way.

### 9. Vendor-neutral public contracts
All public and extension-facing models are storage-neutral Pydantic
contracts. No vendor types (Qdrant payloads, Neo4j/Memgraph types, provider
SDK objects) leak through public APIs or the extension SDK/SPI. Storage
backends sit behind protocols (e.g. `GraphStore`) so a backend can be swapped
without changing contracts.

### 10. Ingestion converges on Core
Static, dynamic, and connector-driven ingestion all enter through the
canonical Gateway/Core ingestion contracts. No component outside Core
implements its own chunking, embedding, vector upsert, or document-catalog
semantics. Connectors are source adapters: they fetch, normalize, map
metadata, and submit — nothing else.

### 11. Extensions extend, never fork
Extensions register capabilities through the `RETRIVA_EXTENSIONS` mechanism
and its registries (e.g. `CapabilityRegistry`, `GraphExtensionRegistry`) using
namespaced vocabularies (`retriva:`, `crm:`, vendor prefixes). Core-owned
shared services (e.g. entity resolution) are used, not reimplemented. The
first connector (MediaWiki) is not a special case: every future connector
(SharePoint, OneDrive, Drive, SFTP, …) must fit the same contract.

### 12. Optional capabilities are disabled by default and failure-isolated
Optional capabilities (e.g. GraphRAG) ship disabled; when disabled, system
behavior is exactly what it was before. When enabled, their failures are
isolated (e.g. graph indexing errors never fail an ingestion job or the
vector index).

### 13. Identifier ownership is explicit
Core owns `doc_id`, collections, and `kb_id` tags. Gateway owns the mappings
between UI identifiers and Core identifiers. No component assumes or
reconstructs another component's internal identifiers.

### 14. Every store of record is declared
Qdrant is the vector system of record; PostgreSQL is the authoritative
relational store for business identity, lifecycle, and audit where an ADR
says so. Introducing a new persistent store, or changing what a store owns,
requires an ADR.

### Data and knowledge law

### 15. Assertions, not facts
LLM-extracted knowledge is modelled as evidence-backed assertions with
confidence, provenance, temporal validity, and lifecycle status
(`active`, `superseded`, `retracted`, `invalidated`). Conflicting assertions
are preserved — never silently overwritten.

### 16. Mandatory provenance
Every entity, assertion, relationship, and retrievable result is traceable to
its source documents and chunks. Chat answers carry citations. Results
without evidence references are a defect.

### 17. One logical graph per knowledge boundary
A single logical graph serves each `(collection, kb_id)` scope. Extensions add
namespaced semantic overlays to the common graph — never independent silos.

### 18. User metadata is opaque
User-provided metadata is accepted as-is at ingestion, persisted at document
level, propagated to every chunk, and visible to retrieval, filtering,
citations, and deletion. Core stores it but never interprets, routes on, or
derives policy from it.

### 19. Web evidence is never a snippet
Search snippets are URL discovery only. Authoritative evidence requires
controlled retrieval of the source, normalized extraction, and full
provenance (URL, retrieval timestamp, content hash). Evidence lifecycle is
explicit (`SESSION` vs `PERSISTENT_KB`), and persistent admission happens only
through the standard ingestion/indexing contracts.

### 20. Data movement is idempotent and resumable
Every sync, retry, or crash-recovery path uses stable source identity
(`source_item_id` + `source_revision`), checkpoints, and content-hash caches
(e.g. OCR). Processing the same item twice must not duplicate or lose data;
long jobs must resume from checkpoints, not restart; cancellation is
cooperative.

### 21. First sync is safe by construction
Initial sync uses: baseline start watermark → full baseline scan → catch-up
delta from the watermark → checkpoint save → activation only after catch-up
completes. A source still in baseline/catch-up must not silently present as
complete.

### 22. Deletion is explicit
Remote deletions default to soft-delete/deactivation. Hard deletion requires
explicitly configured policy.

### 23. Business-critical mutations are auditable
Business stores keep an append-only audit trail. Risky automations — entity
merges, qualification lifecycles, role changes — require recorded
merge/review candidates and human approval gates.

### Security and privacy law

### 24. Data sovereignty by design
On-prem, hybrid, and confidential-computing deployments are first-class
models. No component assumes a trusted third-party cloud or that data may
leave the customer's boundary.

### 25. Security trimming before return
No graph object, assertion, path, or embedding may cross an unauthorized
`(tenant_id, kb_id)` boundary; filtering happens before results are returned.
Every new persistent record carries its `tenant_id` from its first migration.

### 26. No content leakage
Logs, telemetry, exceptions, run summaries, and artifacts must never include
document content, page bodies, retrieved chunks, prompts, answers, embedding
vectors, or raw exception messages containing any of these.

### 27. Secrets are referenced, never stored
Credentials come from environment variables, mounted files, or secret
backends. Persistence stores only `secret_ref`-style references. Secrets never
appear in checkpoints, state files, telemetry, logs, URL query strings, or
browser storage.

### 28. Egress is bounded and SSRF-safe
All outbound fetching is controlled: allow/blocked-domain policies, size and
time caps, rate limits, and provider-neutral contracts. No arbitrary-URL
crawling.

### 29. AuthN/AuthZ is pluggable and optional
Identity and access management is provided by extensions (e.g. Microsoft
Entra). Core stays agnostic, and a no-auth local mode remains a supported,
explicit deployment configuration.

### Verification law

### 30. Done means acceptance passed, not code-complete
A feature is done only when its spec's acceptance criteria pass. Operational
acceptance is a separate, mandatory gate: a live run through the deployed
interface with representative data, inspection through the real stores,
persistence across container recreation, and a repeated run proving semantic
idempotency.

### 31. Phase gating
The next phase must not begin until the current phase's live acceptance
passes. "Code-complete" never unlocks the next phase by itself.

### 32. Every requirement is testable
Functional requirements come with verifiable acceptance criteria. Deterministic
tests (mock providers, containerized PostgreSQL) are the default; tests that
hit real external providers (e.g. Bedrock smoke tests) are marked and kept
separate from the deterministic suite.

### 33. Regressions are protected
Existing tests must keep passing. Intentional behavior changes update spec and
tests in the same change. Changes affecting parsing, chunking, embedding, or
retrieval re-run bilingual/cross-language regression validation.

### 34. Internal observability stays internal
Debug endpoints and the internal profiler are debug-only. They never become
implicit public API.

### Change process law

### 35. Spec before code
Substantial work happens in numbered, self-contained spec packs
(`specs/NNN-name/`: `spec.md`, `architecture.md`, `plan.md`, `tasks.md`,
`acceptance.md`, plus `openapi.yaml` when the API surface changes). No
substantial change without a spec; no spec without scope boundaries.

### 36. ADRs record significant decisions
Backend choices, provider changes, persistence boundaries, versioning, and
security posture get an Architecture Decision Record before implementation,
not after.

### 37. Documents travel with the change
READMEs, docs, specs, and this constitution are updated in the same change as
the code they describe. The repository's documentation is a truth source, not
an afterthought.

### 38. Scope is binding
In-scope and out-of-scope lists are contracts. Scope changes go through
explicit spec amendment — never silent expansion during implementation.

### 39. Licensing boundary
OSS components (Core, Gateway, WebUI — Apache-2.0) and Pro extensions
(proprietary) never mix: no proprietary code, behavior, or coupling inside
OSS paths, and a default deployment (`up` without the Pro profile) never
pulls, builds, or runs proprietary components.

## Amendment

- Amend this constitution through an ADR-style proposal plus an explicit
  version bump and date on this file.
- Amendments may not weaken product-compatibility, security, privacy, or
  verification law; they may only strengthen a rule or replace it with one at
  least as strict.
- Feature constitutions and `AGENTS.md` files inherit this document and must
  be re-checked whenever it changes.
