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
Pydantic schemas for the Retriva Core API v2 ingestion pipeline.

These models are fully decoupled from the v1 schemas. Metadata validation
is shared via ``validate_user_metadata`` from ``schemas.py``.
"""

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from retriva.ingestion_api.schemas import validate_user_metadata


# ---------------------------------------------------------------------------
# Pipeline stage enum
# ---------------------------------------------------------------------------

class JobStage(str, Enum):
    """Ordered stages of the v2 ingestion pipeline."""

    DETECTING = "DETECTING"
    PREPROCESSING = "PREPROCESSING"
    PARSING = "PARSING"
    NORMALIZATION = "NORMALIZATION"
    CHUNKING = "CHUNKING"
    INDEXING = "INDEXING"


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class DocumentIngestRequestV2(BaseModel):
    """JSON-body request for generic document ingestion."""

    source_uri: str = Field(
        ...,
        description="Path or URI to the document to ingest.",
    )
    content_type: Optional[str] = Field(
        None,
        description=(
            "Explicit MIME type hint. Takes precedence over extension-based "
            "detection when provided."
        ),
    )
    user_metadata: Optional[Dict[str, str]] = Field(
        None,
        description="Optional user-provided key/value metadata to attach to every chunk.",
    )
    parser_hint: Optional[str] = Field(
        None,
        description=(
            "Force a specific parser backend (e.g. 'docling', 'ocrmypdf'). "
            "Ignored if the backend is not registered."
        ),
    )

    @field_validator("user_metadata")
    @classmethod
    def _validate_metadata(
        cls, v: Optional[Dict[str, str]],
    ) -> Optional[Dict[str, str]]:
        return validate_user_metadata(v)


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class IngestResponseV2(BaseModel):
    """Acknowledgement returned when a v2 ingestion job is accepted."""

    status: str = Field(..., description="Result status, e.g. 'accepted'.")
    message: str = Field(..., description="Human-readable summary.")
    job_id: str = Field(..., description="Unique job identifier for status polling.")


class JobResponseV2(BaseModel):
    """Extended job status including pipeline stage information."""

    job_id: str
    status: str
    source: str
    job_type: str
    current_stage: Optional[str] = Field(
        None,
        description="The pipeline stage currently executing.",
    )
    stages_completed: List[str] = Field(
        default_factory=list,
        description="Ordered list of stages that have finished.",
    )
    created_at: str
    updated_at: str
    error: Optional[str] = None

# ---------------------------------------------------------------------------
# Artifact schemas
# ---------------------------------------------------------------------------

class ArtifactRequestV2(BaseModel):
    """Request to generate a new artifact."""

    artifact_type: str = Field(..., description="Type of artifact, e.g. 'document_list'.")
    format: str = Field(..., description="Target format, e.g. 'pdf', 'markdown'.")
    parameters: Optional[Dict[str, str]] = Field(
        default_factory=dict,
        description="Format-specific generation parameters.",
    )
    user_metadata: Optional[Dict[str, str]] = Field(
        None,
        description="Optional user-provided metadata to associate with the artifact.",
    )

    @field_validator("user_metadata")
    @classmethod
    def _validate_metadata(
        cls, v: Optional[Dict[str, str]],
    ) -> Optional[Dict[str, str]]:
        return validate_user_metadata(v)


class ArtifactResponseV2(BaseModel):
    """Acknowledgement returned when an artifact job is accepted."""

    status: str = Field(..., description="Result status, e.g. 'accepted'.")
    message: str = Field(..., description="Human-readable summary.")
    job_id: str = Field(..., description="Unique job identifier for status polling.")
    artifact_id: str = Field(..., description="Unique artifact identifier for download.")


class ArtifactCapabilitiesResponseV2(BaseModel):
    """Response containing supported artifact types and formats."""

    supported_formats: List[str]
    supported_types: List[str]
    templates: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Document and Retrieval schemas
# ---------------------------------------------------------------------------

from typing import Any

class DocumentResponse(BaseModel):
    """Document representation retrieved from vector store."""
    doc_id: str
    source_path: str
    page_title: str
    user_metadata: Optional[Dict[str, str]] = None

class DocumentListResponse(BaseModel):
    """Response containing a list of documents."""
    documents: List[DocumentResponse]
    total: int

class DocumentCountResponse(BaseModel):
    """Response containing the count of documents."""
    count: int

class MetadataSchemaResponse(BaseModel):
    """Response containing available metadata keys."""
    keys: List[str]

class MetadataValuesResponse(BaseModel):
    """Response containing available values for a metadata key."""
    key: str
    values: List[str]

class RetrievalRequest(BaseModel):
    """Request to retrieve chunks based on vector search and metadata filter."""
    query: str = Field(..., description="Query text to embed and search.")
    top_k: int = Field(5, description="Number of top chunks to return.")
    user_metadata_filter: Optional[Dict[str, str]] = Field(
        None, description="Optional metadata filter to apply before ranking."
    )

class RetrievalResponse(BaseModel):
    """Response containing retrieved chunks."""
    chunks: List[Dict[str, Any]]
