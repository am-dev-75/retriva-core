# Copyright (C) 2026 Andrea Marson (am.dev.75@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Storage-neutral graph contracts for Retriva's GraphRAG platform service.

All models are Pydantic ``BaseModel`` subclasses.  No graph-database-
specific types (Neo4j, Memgraph, ArangoDB, Apache AGE, etc.) are exposed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EntityCategory(str, Enum):
    """Generic Core entity categories.

    Extensions register additional namespaced types via
    :class:`VocabularyRegistration`; those are stored in
    :attr:`Entity.entity_type` (e.g. ``"crm:Opportunity"``) while
    :attr:`Entity.category` remains one of these generic values.
    """

    PERSON = "retriva:Person"
    ORGANIZATION = "retriva:Organization"
    PRODUCT = "retriva:Product"
    PROJECT = "retriva:Project"
    LOCATION = "retriva:Location"
    EVENT = "retriva:Event"
    TECHNOLOGY = "retriva:Technology"
    REGULATION = "retriva:Regulation"
    CONCEPT = "retriva:Concept"
    UNKNOWN = "retriva:Unknown"


class AssertionClass(str, Enum):
    """Trust category of an assertion.

    Not all categories carry the same trust level.  Extracted assertions
    are LLM output and must not be treated as authoritative facts.
    """

    SOURCE_METADATA = "source_metadata"      # deterministic from document metadata
    EXTRACTED = "extracted"                   # LLM-extracted
    INFERRED = "inferred"                     # derived via reasoning
    HUMAN_VALIDATED = "human_validated"       # reviewed and approved
    OPERATIONAL = "operational"               # materialized projection


class AssertionStatus(str, Enum):
    """Lifecycle status of an assertion."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    INVALIDATED = "invalidated"


class RetrievalMode(str, Enum):
    """GraphRAG retrieval mode.

    - ``VECTOR``: backward-compatible default (Qdrant only).
    - ``GRAPH_LOCAL``: entity-centric graph expansion.
    - ``GRAPH_GLOBAL``: community-report-based search (Phase 2).
    - ``HYBRID``: vector + graph.
    - ``AUTO``: automatic mode selection (Phase 2).
    """

    VECTOR = "vector"
    GRAPH_LOCAL = "graph_local"
    GRAPH_GLOBAL = "graph_global"
    HYBRID = "hybrid"
    AUTO = "auto"


class GraphEventType(str, Enum):
    """Graph-change event types."""

    ENTITY_CREATED = "GraphEntityCreated"
    ENTITY_UPDATED = "GraphEntityUpdated"
    ENTITY_MERGED = "GraphEntityMerged"
    ASSERTION_CREATED = "GraphAssertionCreated"
    ASSERTION_SUPERSEDED = "GraphAssertionSuperseded"
    ASSERTION_INVALIDATED = "GraphAssertionInvalidated"
    NEIGHBORHOOD_CHANGED = "GraphNeighborhoodChanged"
    COMMUNITY_UPDATED = "GraphCommunityUpdated"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_id(prefix: str = "") -> str:
    return f"{prefix}{uuid4().hex}"


# ---------------------------------------------------------------------------
# Core graph models
# ---------------------------------------------------------------------------

class Entity(BaseModel):
    """A canonical entity in the knowledge graph.

    The ``entity_id`` is assigned by :class:`EntityResolutionService`
    and is stable across re-ingestion of the same source documents.
    """

    entity_id: str = Field(default_factory=lambda: _gen_id("ent_"))
    tenant_id: str = Field(..., description="Collection name (tenant boundary).")
    kb_id: str = Field(..., description="Knowledge-space ID.")
    name: str
    name_normalized: str = ""
    category: EntityCategory = EntityCategory.UNKNOWN
    entity_type: Optional[str] = Field(
        None,
        description="Namespaced type, e.g. 'crm:Opportunity'. "
                    "None means the generic category is the type.",
    )
    namespace: str = "retriva"
    aliases: List[str] = Field(default_factory=list)
    external_ids: Dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of external system → ID.",
    )
    description: Optional[str] = None
    properties: Dict[str, Any] = Field(default_factory=dict)
    security_scope: List[str] = Field(
        default_factory=list,
        description="Authorized scopes (kb_id values in Phase 1).",
    )
    created_at: str = Field(default_factory=_utcnow_iso)
    updated_at: str = Field(default_factory=_utcnow_iso)


class Assertion(BaseModel):
    """An evidence-backed assertion (not an unqualified fact).

    Extracted assertions (``assertion_class = EXTRACTED``) are LLM output
    and must not be treated as authoritative.  Conflicting assertions are
    preserved with their evidence rather than silently overwriting one
    another.
    """

    assertion_id: str = Field(default_factory=lambda: _gen_id("ast_"))
    tenant_id: str
    kb_id: str
    subject_entity_id: str
    predicate: str = Field(..., description="Namespaced, e.g. 'retriva:worksFor'.")
    object_entity_id: Optional[str] = Field(
        None, description="Object entity ID. None for literal assertions."
    )
    object_value: Optional[str] = Field(
        None, description="Literal value when object_entity_id is None."
    )
    assertion_class: AssertionClass = AssertionClass.EXTRACTED
    source_document_ids: List[str] = Field(default_factory=list)
    source_chunk_ids: List[str] = Field(default_factory=list)
    source_spans: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="List of {{chunk_id, start, end}} dicts.",
    )
    extraction_confidence: float = 0.0
    extractor_profile: str = "retriva:default"
    extractor_version: str = "0.1.0"
    observed_at: Optional[str] = None
    ingested_at: str = Field(default_factory=_utcnow_iso)
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    status: AssertionStatus = AssertionStatus.ACTIVE
    superseded_by: Optional[str] = None
    security_scope: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class Relationship(BaseModel):
    """A relationship between two entities, backed by one or more assertions."""

    relationship_id: str = Field(default_factory=lambda: _gen_id("rel_"))
    tenant_id: str
    kb_id: str
    source_entity_id: str
    target_entity_id: str
    predicate: str
    assertion_ids: List[str] = Field(default_factory=list)
    properties: Dict[str, Any] = Field(default_factory=dict)
    security_scope: List[str] = Field(default_factory=list)


class GraphEvent(BaseModel):
    """A temporal event associated with graph entities."""

    event_id: str = Field(default_factory=lambda: _gen_id("gev_"))
    tenant_id: str
    kb_id: str
    entity_id: Optional[str] = None
    event_type: str = Field(..., description="Namespaced event type.")
    description: str
    observed_at: Optional[str] = None
    source_assertion_ids: List[str] = Field(default_factory=list)
    security_scope: List[str] = Field(default_factory=list)


class EvidenceReference(BaseModel):
    """A reference to the source evidence for a graph-derived result."""

    document_id: str
    chunk_id: str
    span: Optional[Dict[str, Any]] = Field(
        None, description="{{start, end}} if available."
    )
    text_snippet: Optional[str] = None


class Community(BaseModel):
    """A community of related entities.

    Phase 1 stores the structure only; ``summary`` is populated when
    community reports are implemented in Phase 2.
    """

    community_id: str = Field(default_factory=lambda: _gen_id("com_"))
    tenant_id: str
    kb_id: str
    entity_ids: List[str] = Field(default_factory=list)
    level: int = 0
    title: Optional[str] = None
    summary: Optional[str] = None
    security_scope: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Mutation & Query contracts
# ---------------------------------------------------------------------------

class GraphMutationRequest(BaseModel):
    """A batch mutation request for the graph store."""

    tenant_id: str
    kb_id: str
    entities: List[Entity] = Field(default_factory=list)
    assertions: List[Assertion] = Field(default_factory=list)
    relationships: List[Relationship] = Field(default_factory=list)
    events: List[GraphEvent] = Field(default_factory=list)
    evidence: List[EvidenceReference] = Field(default_factory=list)
    source_document_id: str
    source_chunk_ids: List[str] = Field(default_factory=list)


class GraphQueryRequest(BaseModel):
    """A storage-neutral graph query."""

    tenant_id: str
    kb_id: str
    entity_ids: List[str] = Field(default_factory=list)
    entity_name: Optional[str] = None
    predicates: List[str] = Field(default_factory=list)
    max_depth: int = 2
    max_nodes: int = 50
    max_edges: int = 100
    as_of: Optional[str] = Field(
        None, description="Temporal filter: only assertions active at this time."
    )
    security_scope: List[str] = Field(
        default_factory=list,
        description="Authorized scopes. Results are filtered before return.",
    )


class GraphPath(BaseModel):
    """A path between two entities in the graph."""

    start_entity_id: str
    end_entity_id: str
    entity_ids: List[str]
    predicates: List[str]
    assertion_ids: List[str] = Field(default_factory=list)


class GraphRetrievalResult(BaseModel):
    """The result of a graph query."""

    entities: List[Entity] = Field(default_factory=list)
    assertions: List[Assertion] = Field(default_factory=list)
    relationships: List[Relationship] = Field(default_factory=list)
    paths: List[GraphPath] = Field(default_factory=list)
    evidence: List[EvidenceReference] = Field(default_factory=list)
    graph_score: float = 0.0
    warnings: List[str] = Field(default_factory=list)
    truncated: bool = False


# ---------------------------------------------------------------------------
# Extraction & vocabulary contracts
# ---------------------------------------------------------------------------

class ExtractionProfile(BaseModel):
    """Describes an extraction profile that an extension provides."""

    profile_id: str = Field(..., description="e.g. 'retriva:default', 'crm:opportunity'.")
    namespace: str
    supported_source_types: List[str] = Field(default_factory=list)
    entity_types: List[str] = Field(default_factory=list)
    predicates: List[str] = Field(default_factory=list)
    description: str = ""


class VocabularyRegistration(BaseModel):
    """A namespaced vocabulary contribution from an extension."""

    namespace: str = Field(..., description="e.g. 'crm', 'cyber', 'example'.")
    entity_types: List[str] = Field(
        default_factory=list,
        description="e.g. ['crm:Opportunity', 'crm:Contact'].",
    )
    predicates: List[str] = Field(
        default_factory=list,
        description="e.g. ['crm:blockedBy', 'crm:ownedBy'].",
    )
    event_types: List[str] = Field(default_factory=list)
    description: str = ""


# ---------------------------------------------------------------------------
# Graph-change events
# ---------------------------------------------------------------------------

class GraphChangeEvent(BaseModel):
    """A storage-neutral graph-change domain event."""

    event_id: str = Field(default_factory=lambda: _gen_id("gce_"))
    event_type: GraphEventType
    tenant_id: str
    kb_id: str
    entity_ids: List[str] = Field(default_factory=list)
    assertion_ids: List[str] = Field(default_factory=list)
    source_document_id: Optional[str] = None
    timestamp: str = Field(default_factory=_utcnow_iso)
    metadata: Dict[str, Any] = Field(default_factory=dict)
