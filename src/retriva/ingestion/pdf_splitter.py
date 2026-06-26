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
PDF splitting utility for large documents.

Splits a PDF into smaller N-page chunks so that downstream parsers
(e.g. Docling) can process each chunk in memory without exceeding
available RAM.

Uses ``pikepdf`` (already installed as an OCRmyPDF dependency).
"""

import os
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from retriva.logger import get_logger

logger = get_logger(__name__)


def get_page_count(pdf_path: str) -> int:
    """Return the number of pages in a PDF."""
    import pikepdf
    with pikepdf.open(pdf_path) as pdf:
        return len(pdf.pages)


def split_pdf(
    pdf_path: str,
    chunk_size: int,
    output_dir: Optional[str] = None,
) -> List[Tuple[str, int, int]]:
    """Split a PDF into smaller chunks.

    Args:
        pdf_path:    Path to the input PDF.
        chunk_size:  Maximum number of pages per chunk.
        output_dir:  Directory for chunk files. If None, uses a temp dir
                     next to the input file.

    Returns:
        List of ``(chunk_path, start_page, end_page)`` tuples.
        Page numbers are 1-based inclusive.
    """
    import pikepdf

    pdf_name = Path(pdf_path).stem
    if output_dir is None:
        output_dir = os.path.join(
            os.path.dirname(pdf_path) or ".",
            f"_split_{pdf_name}",
        )
    os.makedirs(output_dir, exist_ok=True)

    chunks: List[Tuple[str, int, int]] = []

    with pikepdf.open(pdf_path) as src_pdf:
        total_pages = len(src_pdf.pages)
        if total_pages <= chunk_size:
            logger.debug(
                f"PDF '{pdf_name}' has {total_pages} pages — no split needed "
                f"(threshold={chunk_size})"
            )
            return [(pdf_path, 1, total_pages)]

        num_chunks = (total_pages + chunk_size - 1) // chunk_size
        logger.info(
            f"Splitting PDF '{pdf_name}' ({total_pages} pages) into "
            f"{num_chunks} chunks of up to {chunk_size} pages each"
        )

        for chunk_idx in range(num_chunks):
            start_page = chunk_idx * chunk_size       # 0-based
            end_page = min(start_page + chunk_size, total_pages)
            start_1 = start_page + 1                   # 1-based inclusive
            end_1 = end_page                            # 1-based inclusive

            chunk_pdf = pikepdf.new()
            for page_idx in range(start_page, end_page):
                chunk_pdf.pages.append(src_pdf.pages[page_idx])

            chunk_path = os.path.join(
                output_dir,
                f"{pdf_name}_part{chunk_idx + 1:04d}.pdf",
            )
            chunk_pdf.save(chunk_path)
            chunk_pdf.close()
            chunks.append((chunk_path, start_1, end_1))
            logger.debug(
                f"  chunk {chunk_idx + 1}/{num_chunks}: pages {start_1}-{end_1} "
                f"({os.path.getsize(chunk_path)} bytes)"
            )

    logger.info(
        f"Split complete: {len(chunks)} chunks written to '{output_dir}'"
    )
    return chunks


def cleanup_chunks(chunks: List[Tuple[str, int, int]]) -> None:
    """Delete chunk files and their parent split directory."""
    if not chunks:
        return
    # Delete all chunk files
    for chunk_path, _, _ in chunks:
        try:
            if os.path.exists(chunk_path):
                os.remove(chunk_path)
        except Exception:
            pass
    # Try to remove the parent directory if empty
    parent_dir = os.path.dirname(chunks[0][0])
    try:
        if parent_dir and os.path.exists(parent_dir):
            # Only remove if empty (don't delete original file's dir)
            remaining = os.listdir(parent_dir)
            if not remaining and parent_dir.startswith(
                os.path.join(os.path.dirname(chunks[0][0]))
            ):
                os.rmdir(parent_dir)
    except Exception:
        pass
