# Copyright (C) 2026 Andrea Marson (am-dev-75@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Session Document Processing — domain models.

Generic, CRM-agnostic models for chat-session-scoped attachments and
artifacts.  Nothing in this module mentions CRM, prospects, ICPs, or
candidate companies.

An :class:`AttachmentRecord` is the metadata row for an uploaded file that
is processed (parsed) but **never** ingested into the persistent knowledge
base.  A :class:`SessionArtifactRecord` is a generated output file scoped
to the same session.  Both expire after a configurable TTL and are
inaccessible from other sessions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AttachmentStatus(str, Enum):
    """Lifecycle status of a session attachment."""

    UPLOADED = "uploaded"            # validated + stored, not yet parsed
    PARSING = "parsing"
    PARSED = "parsed"                 # parsed representation available
    FAILED = "failed"
    EXPIRED = "expired"
    DELETED = "deleted"


class ArtifactStatus(str, Enum):
    """Lifecycle status of a session artifact (generated output)."""

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    EXPIRED = "expired"
    DELETED = "deleted"


class AttachmentRecord(BaseModel):
    """Metadata for a session-scoped uploaded attachment.

    The raw local filesystem path is NOT exposed through public contracts;
    ``storage_ref`` is an opaque, backend-relative reference.
    """

    attachment_id: str
    session_id: str
    original_filename: str
    media_type: str
    size: int
    checksum: str
    upload_time: str = Field(default_factory=_utcnow_iso)
    expiration_time: str
    status: AttachmentStatus = AttachmentStatus.UPLOADED
    selected_parser: Optional[str] = None
    owner: Optional[str] = None
    storage_ref: str
    failure_info: Optional[str] = None
    # Parsed representation summary (populated after parsing)
    parsed_element_count: int = 0
    parse_warnings: List[str] = Field(default_factory=list)


class ParsedElement(BaseModel):
    """A single normalized element from a parsed attachment.

    This is a thin, serializable projection of
    :class:`retriva.domain.models.CanonicalRecord` that preserves source
    locations without coupling callers to the full canonical model.
    """

    element_type: str
    text: str
    page: Optional[int] = None
    heading_path: List[str] = Field(default_factory=list)
    table_markdown: Optional[str] = None
    table_html: Optional[str] = None
    source_uri: str = ""
    parser_name: str = ""
    confidence: Optional[float] = None
    # Spreadsheet-specific location
    sheet: Optional[str] = None
    row: Optional[int] = None
    column: Optional[str] = None
    # Text/markdown line range
    line_start: Optional[int] = None
    line_end: Optional[int] = None


class ParsedAttachment(BaseModel):
    """Normalized parsed representation of a session attachment.

    Produced by invoking an existing Retriva parser on the stored file
    **without** running the INDEXING stage.  This is the contract handed
    to extensions for format-independent processing.
    """

    attachment_id: str
    session_id: str
    media_type: str
    elements: List[ParsedElement] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    parser_name: str = ""
    parsed_at: str = Field(default_factory=_utcnow_iso)


class SessionArtifactRecord(BaseModel):
    """Metadata for a session-scoped generated output artifact."""

    artifact_id: str
    session_id: str
    filename: str
    media_type: str
    size: int
    created_at: str = Field(default_factory=_utcnow_iso)
    expiration_time: str
    status: ArtifactStatus = ArtifactStatus.PENDING
    storage_ref: str
    source_attachment_id: Optional[str] = None
    artifact_kind: str = "report"
    metadata: Dict[str, Any] = Field(default_factory=dict)
    failure_info: Optional[str] = None
