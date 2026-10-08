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

import re
from datetime import datetime, timezone
from typing import List, Tuple
from retriva.domain.models import Chunk, ChunkMetadata, ParsedDocument
from retriva.logger import get_logger

from retriva.config import settings

logger = get_logger(__name__)

# Matches markdown-style headings: # through ####
# Only fires on lines produced by Docling's records_to_parsed_document()
# which prefixes headings with '#'. MediaWiki plaintext never contains
# these markers (headings are stripped to bare text), so this is inert
# for the MediaWiki ingestion path.
_RE_MD_HEADING = re.compile(r"^(#{1,4})\s+(.+)$")

# A markdown table row: starts with an optional '|' and contains at least
# one more '|'.  Used by the table-aware splitting path.
_RE_TABLE_ROW = re.compile(r"^\s*\|.*\|")


def _document_point_id(document: ParsedDocument, idx: int, *,
                       image: bool = False) -> str:
    """Deterministic point id for one chunk of a parsed document.

    Uses the persisted version ``chunk_id_seed`` when present (native
    knowledge ingestion; Spec 028 §3.5).  The seed embeds the content
    fingerprint, so a changed-content replacement of the same document
    yields point ids distinct from the prior version's manifest and can
    never collide with the tenant-wide unique ``version_chunks.point_id``.
    Legacy callers without a seed keep the historical
    ``canonical_doc_id`` derivation verbatim (reconcilable with existing
    points)."""
    seed = getattr(document, "chunk_id_seed", None) or \
        document.canonical_doc_id
    from retriva.knowledge.ids import derive_point_id
    return derive_point_id(seed, idx,
                           chunk_type="image" if image else "text")


def _is_table_text(text: str) -> bool:
    """Return True if *text* looks like a markdown table (≥2 pipe rows)."""
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return False
    rows = sum(1 for l in lines if _RE_TABLE_ROW.match(l))
    return rows >= max(2, len(lines) // 2)


def split_table_rows(text: str, max_chars: int, header_lines: int = 0) -> List[str]:
    """Split a markdown table into row-boundary-respecting chunks.

    Each chunk contains as many *complete* rows as fit within *max_chars*.
    A row is never split mid-cell; if a single row exceeds ``max_chars`` it
    is emitted on its own (its length is respected rather than truncating
    company data).  When ``header_lines`` > 0 (markdown separator tables),
    the header rows are repeated at the top of every chunk so each chunk is
    self-describing.

    This is critical for spreadsheet-derived tables: a row split across
    chunk boundaries loses its first cells (company name) and pollutes the
    next chunk with orphaned cell fragments.
    """
    lines = text.splitlines()
    if not lines:
        return []

    head = lines[:header_lines] if header_lines else []
    body = lines[header_lines:] if header_lines else lines

    header_prefix = ("\n".join(head) + "\n") if head else ""
    header_len = len(header_prefix)

    chunks: List[str] = []
    current: List[str] = []
    current_len = header_len

    for line in body:
        line_len = len(line) + 1  # +1 for newline
        # A lone oversized row is emitted as its own chunk (never split).
        if line_len + header_len > max_chars:
            if current:
                chunks.append(header_prefix + "\n".join(current))
                current = []
                current_len = header_len
            chunks.append(line)
            continue
        if current_len + line_len > max_chars and current:
            chunks.append(header_prefix + "\n".join(current))
            current = []
            current_len = header_len
        current.append(line)
        current_len += line_len

    if current:
        chunks.append(header_prefix + "\n".join(current))
    return chunks or [text]


def _count_table_header_lines(text: str) -> int:
    """Count leading header rows of a markdown table (up to the separator row).

    Markdown tables produced from spreadsheets have the shape::

        | Col A | Col B | ...
        |---|---|...
        | data | data | ...

    Returns 0 when no separator row is found in the first few lines.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines[:5]):
        if re.fullmatch(r"\s*\|[\s:|-]+\|\s*", line):
            return i + 1
    return 0


def _is_heading(paragraph: str) -> bool:
    """Return True if *paragraph* is a single-line markdown heading."""
    return bool(_RE_MD_HEADING.match(paragraph))


def _extract_heading_text(paragraph: str) -> str:
    """Return the bare heading text from a markdown heading line."""
    m = _RE_MD_HEADING.match(paragraph)
    return m.group(2).strip() if m else ""


def _merge_heading_paragraphs(paragraphs: List[str]) -> List[Tuple[str, str]]:
    """Merge heading paragraphs with the body paragraphs that follow them.

    Returns a list of ``(section_heading, text)`` tuples.

    A run of *consecutive* headings (very common in extracted PDFs, e.g.
    ``# 3.3 CHECKPOINTS`` immediately followed by ``# 3.3.1 Power Supply``)
    is accumulated together and attached to the next body paragraph, so we
    never emit orphaned heading-only micro-chunks. The ``section_heading``
    reported for the merged chunk is the *deepest* (most specific) heading in
    the run, which best identifies the content that follows.

    For content without markdown headings (e.g. MediaWiki plaintext) every
    tuple will have ``section_heading == ""``, preserving current behaviour.
    """
    result: List[Tuple[str, str]] = []
    pending_heading_lines: List[str] = []  # raw heading lines awaiting a body
    current_section = ""                    # most recent (deepest) heading text

    for para in paragraphs:
        if _is_heading(para):
            # Accumulate consecutive headings instead of flushing each one,
            # so we never emit orphaned heading-only micro-chunks. The most
            # recent heading becomes the active section.
            pending_heading_lines.append(para)
            current_section = _extract_heading_text(para)
        else:
            if pending_heading_lines:
                # Merge ALL accumulated heading lines with this body paragraph.
                merged = "\n\n".join(pending_heading_lines + [para])
                result.append((current_section, merged))
                pending_heading_lines = []
            else:
                # No pending headings: this body paragraph still belongs to the
                # currently active section (e.g. the 2nd+ paragraph under a
                # heading), so it inherits ``current_section``.
                result.append((current_section, para))

    # Flush any trailing heading run that had no body after it (so trailing
    # section titles are not silently lost), as a single combined chunk.
    if pending_heading_lines:
        merged = "\n\n".join(pending_heading_lines)
        result.append((current_section, merged))

    return result


def _prepend_section_context(text: str, section_heading: str) -> str:
    """Prepend a ``[Section: …]`` context prefix to *text*.

    If *section_heading* is empty the text is returned unchanged (no-op
    for MediaWiki and other non-heading content).
    """
    if not section_heading:
        return text
    return f"[Section: {section_heading}]\n{text}"


def recursive_split_text(text: str, max_chars: int, overlap: int) -> List[str]:
    """
    Recursively splits text into chunks until each chunk is smaller than max_chars.
    Attempts to split at \n, then at . , then at space.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    # Ensure overlap is reasonable
    actual_overlap = min(overlap, max_chars // 2)

    separators = ["\n", ". ", " "]
    for sep in separators:
        if sep in text:
            # Find the last occurrence of sep that keeps the left part within max_chars
            split_idx = text.rfind(sep, 0, max_chars)
            
            # Ensure we actually make progress (split_idx > 0)
            if split_idx > 0:
                left = text[:split_idx].strip()
                # The next part should include the overlap
                overlap_start = max(0, split_idx - actual_overlap)
                right = text[overlap_start:].strip()
                
                # Check if we made progress
                if len(right) >= len(text):
                    continue

                chunks = [left]
                if right:
                    chunks.extend(recursive_split_text(right, max_chars, actual_overlap))
                return chunks

    # Hard cut if no separators found or they don't help
    left = text[:max_chars].strip()
    right = text[max_chars - actual_overlap:].strip()
    
    if len(right) >= len(text) or not right:
        return [left]
        
    chunks = [left]
    chunks.extend(recursive_split_text(right, max_chars, actual_overlap))
    return chunks

def create_image_chunks(document: ParsedDocument, ingestion_timestamp: str = None) -> List[Chunk]:
    """
    Creates chunks from the extracted images for dense retrieval formatting.
    If VLM description is available, it becomes the primary text content.
    """
    if ingestion_timestamp is None:
        ingestion_timestamp = datetime.now(timezone.utc).isoformat()

    chunks = []
    for idx, img in enumerate(document.images):
        if img.vlm_description:
            # VLM-enriched: use the detailed description as primary content
            text_parts = [f"Image: {img.src}"]
            if img.alt: text_parts.append(f"Alt text: {img.alt}")
            if img.caption: text_parts.append(f"Caption: {img.caption}")
            text_parts.append(f"Description: {img.vlm_description}")
        else:
            # Fallback: HTML metadata only
            text_parts = [f"Image: {img.src}"]
            if img.alt: text_parts.append(f"Alt text: {img.alt}")
            if img.caption: text_parts.append(f"Caption: {img.caption}")
            if img.surrounding_text: text_parts.append(f"Context: {img.surrounding_text}")
        
        text = "\n".join(text_parts)
        
        chunk_id = _document_point_id(document, idx, image=True)
        meta = ChunkMetadata(
            doc_id=document.doc_id or document.canonical_doc_id,
            source_path=document.source_path,
            page_title=document.page_title,
            section_path="",
            chunk_id=chunk_id,
            chunk_index=idx,
            chunk_type="image",
            language=document.language,
            image_path=img.src,
            ingestion_timestamp=ingestion_timestamp,
            user_metadata=document.user_metadata,
            kb_id=document.kb_id,
            filename=document.filename,
            content_size=document.content_size,
            ingestion_status=document.ingestion_status,
            created_at=document.created_at,
            content_hash=document.content_hash,
            content_hash_algorithm="sha256" if document.content_hash else None,
            source_paths=document.source_paths,
        )
        
        chunks.append(Chunk(text=text, metadata=meta))
    
    logger.debug(f"Created {len(chunks)} image chunks.")
    return chunks

def create_chunks(document: ParsedDocument) -> List[Chunk]:
    """
    Splits the parsed document text into section-aware chunks.

    Markdown-style headings (``# … `` through ``#### …``) are detected and
    used in two ways:

    1. **Heading merging** — a heading paragraph is merged with the body
       paragraph that follows it so they stay in the same chunk instead of
       producing an orphaned micro-chunk.
    2. **Section context prefix** — every body chunk is prefixed with
       ``[Section: <heading text>]`` so that the embedding carries the
       semantic identity of its section.

    For content without markdown headings (e.g. MediaWiki plaintext) the
    behaviour is identical to the previous implementation.
    """
    ingestion_timestamp = datetime.now(timezone.utc).isoformat()

    paragraphs = [p.strip() for p in document.content_text.split("\n\n") if p.strip()]
    logger.debug(f"Splitting '{document.source_path}' into {len(paragraphs)} initial paragraphs...")

    # Phase 1: merge headings with following body paragraphs
    merged_paragraphs = _merge_heading_paragraphs(paragraphs)

    # Phase 2: split oversized paragraphs, preserving section info
    final_items: List[Tuple[str, str]] = []  # (section_heading, text)
    for section_heading, para in merged_paragraphs:
        if len(para) > settings.max_chunk_chars:
            logger.info(f"Paragraph too long ({len(para)} chars), splitting...")
            # Table-aware path: spreadsheet-derived markdown tables must be
            # split at row boundaries, never mid-row (a split row loses its
            # first cells — e.g. the company name — and the next chunk starts
            # with orphaned cell fragments).
            if _is_table_text(para):
                header_lines = _count_table_header_lines(para)
                split_parts = split_table_rows(para, settings.max_chunk_chars, header_lines)
                logger.info(
                    f"Table paragraph split into {len(split_parts)} row-aligned chunks "
                    f"(header_lines={header_lines})"
                )
            else:
                split_parts = recursive_split_text(para, settings.max_chunk_chars, settings.chunk_overlap)
            for part in split_parts:
                final_items.append((section_heading, part))
        else:
            final_items.append((section_heading, para))

    # Phase 3: create Chunk objects with section context
    chunks = []
    for idx, (section_heading, text) in enumerate(final_items):
        # Prepend section context for embedding quality
        enriched_text = _prepend_section_context(text, section_heading)

        chunk_id = _document_point_id(document, idx, image=False)
        meta = ChunkMetadata(
            doc_id=document.doc_id or document.canonical_doc_id,
            source_path=document.source_path,
            page_title=document.page_title,
            section_path=section_heading,
            chunk_id=chunk_id,
            chunk_index=idx,
            chunk_type="text",
            language=document.language,
            ingestion_timestamp=ingestion_timestamp,
            user_metadata=document.user_metadata,
            kb_id=document.kb_id,
            filename=document.filename,
            content_size=document.content_size,
            ingestion_status=document.ingestion_status,
            created_at=document.created_at,
            content_hash=document.content_hash,
            content_hash_algorithm="sha256" if document.content_hash else None,
            source_paths=document.source_paths,
        )
        
        chunk = Chunk(text=enriched_text, metadata=meta)
        chunks.append(chunk)
        
    image_chunks = create_image_chunks(document, ingestion_timestamp=ingestion_timestamp)
    chunks.extend(image_chunks)
        
    document.chunks = chunks
    return chunks


class DefaultChunker:
    """OSS default chunker — recursive text splitting with image chunk support."""

    def create_chunks(self, document: ParsedDocument) -> List[Chunk]:
        return create_chunks(document)


# Register as default implementation
from retriva.registry import CapabilityRegistry
CapabilityRegistry().register("chunker", DefaultChunker, priority=100)
