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
from datetime import datetime, timezone
from typing import List
from retriva.domain.models import Chunk, ChunkMetadata, ParsedDocument
from retriva.logger import get_logger

from retriva.config import settings

logger = get_logger(__name__)

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
    Splits the parsed document text into chunks under the character limit.
    """
    ingestion_timestamp = datetime.now(timezone.utc).isoformat()

    paragraphs = [p.strip() for p in document.content_text.split("\n\n") if p.strip()]
    logger.debug(f"Splitting '{document.source_path}' into {len(paragraphs)} initial paragraphs...")
    
    final_texts = []
    for para in paragraphs:
        if len(para) > settings.max_chunk_chars:
            logger.info(f"Paragraph too long ({len(para)} chars), splitting recursively...")
            split_para = recursive_split_text(para, settings.max_chunk_chars, settings.chunk_overlap)
            final_texts.extend(split_para)
        else:
            final_texts.append(para)
            
    chunks = []
    for idx, text in enumerate(final_texts):
        chunk_id = hashlib.md5(f"{document.canonical_doc_id}_{idx}".encode("utf-8")).hexdigest()
        meta = ChunkMetadata(
            doc_id=document.doc_id or document.canonical_doc_id,
            source_path=document.source_path,
            page_title=document.page_title,
            section_path="",
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
        
        chunk = Chunk(text=text, metadata=meta)
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
