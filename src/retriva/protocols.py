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
Protocols (interfaces) for Retriva's pluggable pipeline stages.

Each protocol defines the minimal contract that any implementation —
whether the built-in OSS default or a proprietary extension — must satisfy.
"""

from pathlib import Path
from typing import Dict, List, Protocol, runtime_checkable

from retriva.domain.models import Chunk, ParsedDocument


@runtime_checkable
class Retriever(Protocol):
    """Retrieve relevant context chunks for a user query."""

    def retrieve(self, query: str, top_k: int) -> List[Dict]: ...


@runtime_checkable
class Reranker(Protocol):
    """Re-rank retrieved chunks by relevance (two-stage retrieval).

    The domain contract is provider-neutral: implementations receive the
    retrieved chunk dicts and return the SAME dicts reordered, preserving
    all metadata (chunk IDs, source IDs, citations, retrieval scores);
    only ``_score`` may be overwritten with the provider's relevance
    score. The transport (OpenRouter/Cohere-compatible, Amazon Bedrock,
    ...) is selected globally via Retriva's settings system — see
    ``retriva.qa.reranking``.
    """

    def rerank(self, query: str, chunks: List[Dict], top_n: int) -> List[Dict]: ...


@runtime_checkable
class Chunker(Protocol):
    """Split a parsed document into indexable chunks."""

    def create_chunks(self, document: ParsedDocument) -> List[Chunk]: ...


@runtime_checkable
class HTMLParser(Protocol):
    """Extract structured content and metadata from raw HTML."""

    def extract_content(self, html: str) -> str | None: ...

    def extract_language(self, html: str) -> str: ...


@runtime_checkable
class VLMDescriber(Protocol):
    """Generate a textual description of an image via a vision-language model."""

    def describe(self, image_path: Path) -> str: ...


@runtime_checkable
class PromptBuilder(Protocol):
    """Build the system prompt from a user question and retrieved chunks."""

    def build_prompt(self, question: str, chunks: List[Dict]) -> str: ...


@runtime_checkable
class PdfExtractor(Protocol):
    """Extract text page-by-page from a PDF file.

    Implementations may use different backends (pdfplumber, pymupdf, pypdf).
    The default is resolved via the CapabilityRegistry at key ``pdf_extractor``.
    """

    def extract_pages(self, pdf_path: Path) -> list | None:
        """
        Return a list of page dicts ``{"page_number": int, "text": str}``
        for pages with extractable text, or ``None`` if the PDF is
        unreadable (encrypted, corrupt).
        """
        ...

    def extract_metadata(self, pdf_path: Path) -> dict[str, str]:
        """Return PDF metadata (Title, Author, etc.) as a dict."""
        ...
