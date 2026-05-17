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

from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional, Tuple


class ChunkMetadata(BaseModel):
    doc_id: str
    source_path: str
    page_title: str
    section_path: str
    chunk_id: str
    chunk_index: int
    chunk_type: str = "text"
    language: str = "en"
    image_path: Optional[str] = None
    ingestion_timestamp: Optional[str] = None
    kb_id: str = "default"
    filename: Optional[str] = None
    content_size: Optional[int] = None
    ingestion_status: str = "completed"
    created_at: Optional[str] = None
    user_metadata: Optional[Dict[str, str]] = None
    # --- Deduplication fields (v2, optional for backward compat) ---
    content_hash: Optional[str] = None
    content_hash_algorithm: Optional[str] = None
    source_paths: Optional[List[str]] = None


class Chunk(BaseModel):
    text: str
    metadata: ChunkMetadata


class ImageContext(BaseModel):
    src: str
    alt: str
    caption: str
    surrounding_text: str
    vlm_description: str = ""


class ParsedDocument(BaseModel):
    source_path: str
    canonical_doc_id: str
    page_title: str
    content_text: str
    language: str = "en"
    chunks: List[Chunk] = Field(default_factory=list)
    images: List[ImageContext] = Field(default_factory=list)
    user_metadata: Optional[Dict[str, str]] = None
    kb_id: str = "default"
    filename: Optional[str] = None
    content_size: Optional[int] = None
    ingestion_status: str = "completed"
    created_at: Optional[str] = None
    # --- Deduplication fields (v2, optional for backward compat) ---
    doc_id: Optional[str] = None
    content_hash: Optional[str] = None
    source_paths: Optional[List[str]] = None


class DocRecord(BaseModel):
    """Per-KB document catalog entry — the source of truth for deduplication.

    Stored in the DeduplicationStore keyed by (kb_id, content_hash).
    """

    doc_id: str
    kb_id: str
    content_hash: str
    content_hash_algorithm: str = "sha256"
    content_size: int
    mime_type: Optional[str] = None
    filename: str
    source_paths: List[str] = Field(default_factory=list)
    user_metadata: Optional[Dict[str, Any]] = None
    chunk_count: int = 0
    ingestion_status: str = "pending"
    created_at: str
    updated_at: str
    metadata_updated_at: Optional[str] = None


class CanonicalRecord(BaseModel):
    """Unified output format from any v2 parser.

    This is the contract between the PARSING and NORMALIZATION stages.
    Every parser (Tika, Docling, Unstructured, etc.) emits a list of
    ``CanonicalRecord`` objects, regardless of the source format.
    """

    document_id: str                          # Source URI or derived ID
    element_type: str                         # "text", "table", "image", "heading", "page_break"
    text: str                                 # Extracted text or markdown
    page: Optional[int] = None                # 1-indexed page number
    bbox: Optional[Tuple[float, float, float, float]] = None  # (x0, y0, x1, y1)
    heading_path: List[str] = Field(default_factory=list)      # ["Chapter 1", "Section 1.2"]
    table_html: Optional[str] = None          # HTML table representation
    table_markdown: Optional[str] = None      # Markdown table representation
    source_uri: str = ""                      # Original file path/URI
    parser_name: str = ""                     # "tika", "docling", "unstructured", etc.
    confidence: Optional[float] = None        # Parser confidence (0.0–1.0)
    ocr_applied: bool = False                 # Whether OCR was used for this element
    image_path: Optional[str] = None          # Local path to extracted image (for VLM enrichment)
