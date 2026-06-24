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

import hashlib
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
        
        chunk_id = hashlib.md5(f"{document.canonical_doc_id}_img_{idx}".encode("utf-8")).hexdigest()
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
            logger.info(f"Paragraph too long ({len(para)} chars), splitting recursively...")
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

        chunk_id = hashlib.md5(f"{document.canonical_doc_id}_{idx}".encode("utf-8")).hexdigest()
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
