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

import json
from pydantic import BaseModel, Field, field_validator
from typing import Any, Dict, List, Optional
from retriva.domain.models import Chunk

# ---------------------------------------------------------------------------
# User-metadata validation constants
# ---------------------------------------------------------------------------

MAX_METADATA_KEYS = 20
MAX_METADATA_VALUE_LENGTH = 256
MAX_METADATA_SERIALIZED_BYTES = 4096


class UserMetadataValidationError(ValueError):
    """Raised when user_metadata violates hard limits.

    Carries a structured ``details`` list so FastAPI can return a
    descriptive 422 response.
    """

    def __init__(self, details: List[Dict[str, str]]):
        self.details = details
        msgs = "; ".join(d["msg"] for d in details)
        super().__init__(msgs)


def validate_user_metadata(
    value: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Validate user_metadata against hard limits.

    Returns the value unchanged if valid, or raises
    ``UserMetadataValidationError`` with structured details.
    """
    if value is None:
        return value

    errors: List[Dict[str, str]] = []

    # --- type check ---------------------------------------------------------
    for k, v in value.items():
        if not isinstance(k, str):
            errors.append({
                "field": "user_metadata",
                "msg": f"Key {k!r} is not a string",
            })
        # ``kb_ids`` is the only key that may carry a list of strings;
        # all other values must be scalar strings.
        if k == "kb_ids":
            if not isinstance(v, list):
                errors.append({
                    "field": "user_metadata",
                    "msg": f"Value for key {k!r} must be a list of strings (got {type(v).__name__})",
                })
            elif not all(isinstance(item, str) for item in v):
                errors.append({
                    "field": "user_metadata",
                    "msg": f"Value for key {k!r} contains non-string items",
                })
        elif not isinstance(v, str):
            errors.append({
                "field": "user_metadata",
                "msg": f"Value for key {k!r} is not a string (got {type(v).__name__})",
            })

    # --- key count ----------------------------------------------------------
    if len(value) > MAX_METADATA_KEYS:
        errors.append({
            "field": "user_metadata",
            "msg": (
                f"Too many keys: {len(value)} exceeds maximum of "
                f"{MAX_METADATA_KEYS}"
            ),
        })

    # --- per-value length ---------------------------------------------------
    for k, v in value.items():
        if k == "kb_ids" and isinstance(v, list):
            for item in v:
                if isinstance(item, str) and len(item) > MAX_METADATA_VALUE_LENGTH:
                    errors.append({
                        "field": "user_metadata",
                        "msg": (
                            f"Value for key {k!r} item {item!r} is {len(item)} characters, "
                            f"exceeding maximum of {MAX_METADATA_VALUE_LENGTH}"
                        ),
                    })
        elif isinstance(v, str) and len(v) > MAX_METADATA_VALUE_LENGTH:
            errors.append({
                "field": "user_metadata",
                "msg": (
                    f"Value for key {k!r} is {len(v)} characters, "
                    f"exceeding maximum of {MAX_METADATA_VALUE_LENGTH}"
                ),
            })

    # --- total serialized size ----------------------------------------------
    try:
        serialized = json.dumps(value)
        if len(serialized.encode("utf-8")) > MAX_METADATA_SERIALIZED_BYTES:
            errors.append({
                "field": "user_metadata",
                "msg": (
                    f"Serialized metadata is {len(serialized.encode('utf-8'))} bytes, "
                    f"exceeding maximum of {MAX_METADATA_SERIALIZED_BYTES}"
                ),
            })
    except (TypeError, ValueError):
        errors.append({
            "field": "user_metadata",
            "msg": "Metadata is not JSON-serializable",
        })

    if errors:
        raise UserMetadataValidationError(errors)

    return value


# ---------------------------------------------------------------------------
# Ingestion request schemas
# ---------------------------------------------------------------------------

class HtmlIngestRequest(BaseModel):
    source_path: str = Field(..., description="Canonical URL or source path.")
    page_title: str = Field("", description="Title of the page.")
    html_content: str = Field(..., description="Raw HTML content.")
    origin_file_path: str = Field("", description="Original filesystem path of the HTML file (for resolving relative image paths).")
    user_metadata: Optional[Dict[str, str]] = Field(
        None,
        description="Optional user-provided key/value metadata to attach to every chunk.",
    )

    @field_validator("user_metadata")
    @classmethod
    def _validate_metadata(cls, v: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
        return validate_user_metadata(v)

class TextIngestRequest(BaseModel):
    source_path: str = Field(..., description="Canonical URL or source path.")
    page_title: str = Field("", description="Title of the page.")
    content_text: str = Field(..., description="Plain text to chunk.")
    user_metadata: Optional[Dict[str, str]] = Field(
        None,
        description="Optional user-provided key/value metadata to attach to every chunk.",
    )

    @field_validator("user_metadata")
    @classmethod
    def _validate_metadata(cls, v: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
        return validate_user_metadata(v)

class ImageIngestRequest(BaseModel):
    source_path: str = Field(..., description="Canonical URL or source path.")
    page_title: str = Field("", description="Title of the page.")
    file_path: str = Field(..., description="Absolute path to the image file on disk.")
    user_metadata: Optional[Dict[str, str]] = Field(
        None,
        description="Optional user-provided key/value metadata to attach to every chunk.",
    )

    @field_validator("user_metadata")
    @classmethod
    def _validate_metadata(cls, v: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
        return validate_user_metadata(v)

class MediaWikiIngestRequest(BaseModel):
    source_path: str = Field(..., description="Path to the source XML export file.")
    page_title: str = Field(..., description="MediaWiki page title.")
    content_text: str = Field(..., description="Plain text extracted from wikitext.")
    page_id: int = Field(0, description="MediaWiki page ID.")
    namespace: int = Field(0, description="MediaWiki namespace number.")
    linked_assets: List[str] = Field(default_factory=list, description="Resolved local asset paths.")
    user_metadata: Optional[Dict[str, str]] = Field(
        None,
        description="Optional user-provided key/value metadata to attach to every chunk.",
    )

    @field_validator("user_metadata")
    @classmethod
    def _validate_metadata(cls, v: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
        return validate_user_metadata(v)

class PdfIngestRequest(BaseModel):
    source_path: str = Field(..., description="Absolute path to the PDF file.")
    page_title: str = Field(..., description="Derived document title.")
    content_text: str = Field(..., description="Plain text from one PDF page.")
    page_number: int = Field(..., description="1-indexed page number.")
    total_pages: int = Field(0, description="Total pages in the PDF.")
    user_metadata: Optional[Dict[str, str]] = Field(
        None,
        description="Optional user-provided key/value metadata to attach to every chunk.",
    )

    @field_validator("user_metadata")
    @classmethod
    def _validate_metadata(cls, v: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
        return validate_user_metadata(v)

class MarkdownSection(BaseModel):
    heading: str = Field("", description="The heading of the section.")
    content: str = Field(..., description="The textual content of the section.")

class MarkdownIngestRequest(BaseModel):
    source_path: str = Field(..., description="Absolute path to the Markdown file.")
    page_title: str = Field(..., description="Derived document title.")
    sections: List[MarkdownSection] = Field(..., description="Parsed sections of the Markdown file.")
    user_metadata: Optional[Dict[str, str]] = Field(
        None,
        description="Optional user-provided key/value metadata to attach to every chunk.",
    )

    @field_validator("user_metadata")
    @classmethod
    def _validate_metadata(cls, v: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
        return validate_user_metadata(v)

class ChunkIngestRequest(BaseModel):
    chunks: List[Chunk] = Field(..., description="List of completely raw Chunk objects.")

class IngestResponse(BaseModel):
    status: str
    message: str
    job_id: Optional[str] = None

class JobResponse(BaseModel):
    job_id: str
    status: str
    source: str
    job_type: str
    created_at: str
    updated_at: str
    error: Optional[str] = None

class DeleteMetadataRequest(BaseModel):
    user_metadata_filter: Dict[str, str] = Field(..., description="Filter chunks by metadata for deletion")