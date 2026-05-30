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
MediaWiki v2 parser — server-side processing for MediaWiki XML exports.

Processes an entire export directory (XML files + ``assets/`` subtree)
with per-page document granularity, content-hash deduplication, and
VLM image enrichment.

Pages are processed concurrently via a thread pool so that VLM calls
(the dominant latency) overlap instead of running sequentially.

Reuses:
    - ``mediawiki_export_parser``: XML streaming, wikitext→plaintext
    - ``mediawiki_assets``: asset index, file reference resolution
    - ``dedup``: per-page content-hash deduplication
    - ``chunker``: text + image chunk creation
"""

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from retriva.domain.models import DocRecord, ImageContext, ParsedDocument
from retriva.indexing.qdrant_store import (
    COLLECTION_NAME,
    get_client,
    update_payload_by_doc_id,
    upsert_chunks,
)
from retriva.ingestion.dedup import (
    DeduplicationStore,
    derive_doc_id,
    merge_metadata,
    merge_source_paths,
)
from retriva.ingestion.mediawiki_assets import (
    build_asset_index,
    find_assets_dirs,
    is_image_asset,
    resolve_file_reference,
)
from retriva.ingestion.mediawiki_export_parser import (
    DEFAULT_NAMESPACES,
    is_mediawiki_export,
    parse_export,
    wikitext_to_plaintext,
)
from retriva.ingestion_api.job_manager import CancellationError, JobManager
from retriva.ingestion_api.schemas_v2 import JobStage
from retriva.logger import get_logger
from retriva.registry import CapabilityRegistry

# Import to trigger default registration
import retriva.ingestion.chunker  # noqa: F401

logger = get_logger(__name__)

# Maximum number of pages processed concurrently.  The bottleneck is
# the remote VLM call (~2 s each), so 20 in-flight requests provide
# good throughput without overwhelming the upstream API.
_MAX_WORKERS = 20


# ---------------------------------------------------------------------------
# Per-page content hashing
# ---------------------------------------------------------------------------

def _compute_page_content_hash(plaintext: str) -> str:
    """Compute ``sha256:<hex>`` hash from the page's converted plaintext."""
    hex_digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    return f"sha256:{hex_digest}"


# ---------------------------------------------------------------------------
# Per-page result container
# ---------------------------------------------------------------------------

@dataclass
class _PageResult:
    """Outcome of processing a single wiki page."""
    is_new: bool
    is_dedup: bool


# ---------------------------------------------------------------------------
# Per-page worker (runs inside a thread pool)
# ---------------------------------------------------------------------------

def _process_page(
    page_title: str,
    page_id: str,
    page_text: str,
    file_references: List[str],
    xml_path: Path,
    user_metadata: Optional[Dict[str, str]],
    kb_id: str,
    asset_index: Dict[str, Path],
    dedup_store: DeduplicationStore,
    chunker,
    vlm,
    cancel_check: Callable[[], bool],
    job_id: str,
) -> _PageResult:
    """Process a single wiki page: dedup, VLM enrichment, chunk, index.

    This function is designed to be called from a :class:`ThreadPoolExecutor`.
    All shared state (``dedup_store``, ``QdrantClient``) is thread-safe.
    """
    if cancel_check():
        raise CancellationError("Job cancelled during page processing")

    # Convert wikitext → plaintext
    plaintext = wikitext_to_plaintext(page_text)
    if not plaintext.strip():
        logger.debug(f"Job {job_id}: Skipping empty page: {page_title}")
        return _PageResult(is_new=False, is_dedup=False)

    # ── Per-page dedup ──────────────────────────────────────────────────
    page_hash = _compute_page_content_hash(plaintext)
    hex_digest = page_hash.split(":", 1)[1]
    doc_id = derive_doc_id(kb_id, hex_digest)
    source_path_str = f"{xml_path}#{page_id}"

    existing = dedup_store.get_by_hash(
        kb_id, page_hash, collection_name=COLLECTION_NAME
    )

    if existing is not None:
        # Duplicate — merge metadata/paths
        merged_meta, meta_changed = merge_metadata(
            existing.user_metadata, user_metadata, doc_id, kb_id
        )
        merged_paths, paths_changed = merge_source_paths(
            existing.source_paths, source_path_str, doc_id, kb_id
        )
        if meta_changed or paths_changed:
            dedup_store.update_record(
                doc_id=doc_id,
                merged_metadata=merged_meta,
                merged_source_paths=merged_paths,
            )
            now_iso = datetime.now(timezone.utc).isoformat()
            client = get_client()
            update_payload_by_doc_id(client, doc_id, {
                "user_metadata": merged_meta,
                "source_paths": merged_paths,
                "content_hash": page_hash,
                "content_hash_algorithm": "sha256",
                "metadata_updated_at": now_iso,
            })
            logger.info(
                f"Job {job_id}: Page '{page_title}' deduplicated "
                f"(metadata updated), doc_id={doc_id}"
            )
        else:
            logger.info(
                f"Job {job_id}: Page '{page_title}' deduplicated "
                f"(unchanged), doc_id={doc_id}"
            )
        return _PageResult(is_new=False, is_dedup=True)

    # ── New page — create record, chunk, index ──────────────────────────
    now_iso = datetime.now(timezone.utc).isoformat()

    record = DocRecord(
        doc_id=doc_id,
        kb_id=kb_id,
        collection_name=COLLECTION_NAME,
        content_hash=page_hash,
        content_size=len(plaintext.encode("utf-8")),
        mime_type="application/mediawiki-export+xml",
        filename=f"{page_title}.wiki",
        source_paths=[source_path_str],
        user_metadata=user_metadata,
        ingestion_status="pending",
        created_at=now_iso,
        updated_at=now_iso,
    )
    dedup_store.create_record(record)

    # Resolve [[File:…]] references → ImageContext for VLM
    images: List[ImageContext] = []
    for ref in file_references:
        if cancel_check():
            raise CancellationError("Job cancelled during VLM processing")
        resolved = resolve_file_reference(ref, asset_index)
        if resolved and is_image_asset(resolved):
            description = ""
            if vlm:
                try:
                    description = vlm.describe(resolved) or ""
                except Exception as e:
                    logger.warning(
                        f"Job {job_id}: VLM failed for '{ref}': {e}"
                    )
            images.append(ImageContext(
                src=str(resolved),
                alt=ref,
                caption="",
                surrounding_text="",
                vlm_description=description,
            ))

    # Build ParsedDocument for the chunker
    doc = ParsedDocument(
        source_path=source_path_str,
        canonical_doc_id=source_path_str,
        page_title=page_title,
        content_text=plaintext,
        images=images,
        user_metadata=user_metadata,
        doc_id=doc_id,
        kb_id=kb_id,
        content_hash=page_hash,
        source_paths=[source_path_str],
        filename=f"{page_title}.wiki",
        content_size=len(plaintext.encode("utf-8")),
        ingestion_status="completed",
        created_at=now_iso,
    )

    chunks = chunker.create_chunks(doc)

    client = get_client()
    upsert_chunks(client, chunks, cancel_check=cancel_check)

    try:
        dedup_store.finalize_record(doc_id, chunk_count=len(chunks))
    except KeyError:
        pass

    logger.info(
        f"Job {job_id}: Indexed page '{page_title}' — "
        f"{len(chunks)} chunk(s), doc_id={doc_id}"
    )

    return _PageResult(is_new=True, is_dedup=False)


# ---------------------------------------------------------------------------
# Main processing function
# ---------------------------------------------------------------------------

def process_mediawiki_export(
    staged_dir: str,
    user_metadata: Optional[Dict[str, str]],
    kb_id: str,
    cancel_check: Callable[[], bool],
    job_id: str,
    namespaces: Optional[Set[int]] = None,
) -> None:
    """Process a MediaWiki export directory with per-page granularity.

    This function:
    1. Discovers XML export files under *staged_dir*.
    2. Builds an asset index from ``assets/`` subdirectories.
    3. For each wiki page in each XML file (concurrently):
       a. Converts wikitext to plaintext.
       b. Computes a per-page content hash for dedup.
       c. If new: creates chunks, upserts to Qdrant, finalises catalog.
       d. If duplicate: merges metadata/source_paths.
       e. Resolves ``[[File:…]]`` references for VLM image enrichment.
    4. Reports progress via :class:`JobManager`.

    Pages are dispatched to a thread pool (up to ``_MAX_WORKERS``
    concurrent pages) so that remote VLM calls overlap.

    Args:
        staged_dir:     Local directory containing XML + ``assets/``.
        user_metadata:  Optional user-provided key/value metadata.
        kb_id:          Knowledge base to ingest into.
        cancel_check:   Cancellation callback.
        job_id:         Job identifier for progress tracking.
        namespaces:     MediaWiki namespace IDs to index (default: ``{0, 6}``).
    """
    manager = JobManager()
    manager.start_job(job_id)

    if namespaces is None:
        namespaces = DEFAULT_NAMESPACES

    staged_path = Path(staged_dir)
    dedup_store = DeduplicationStore()

    try:
        # ── DETECTING ───────────────────────────────────────────────────────
        manager.advance_stage(job_id, JobStage.DETECTING.value)

        xml_files: List[Path] = []
        if staged_path.is_file():
            if staged_path.suffix.lower() == ".xml" and is_mediawiki_export(staged_path):
                xml_files.append(staged_path)
            else:
                raise ValueError(f"'{staged_path}' is not a valid MediaWiki XML export.")
        else:
            for path in sorted(staged_path.rglob("*.xml")):
                if is_mediawiki_export(path):
                    xml_files.append(path)

        if not xml_files:
            logger.warning(f"Job {job_id}: No MediaWiki XML exports found in '{staged_dir}'.")
            manager.complete_job(job_id)
            return

        logger.info(f"Job {job_id}: Found {len(xml_files)} MediaWiki XML export file(s).")

        if cancel_check():
            raise CancellationError("Job cancelled during detection")

        # ── PREPROCESSING (asset index) ─────────────────────────────────────
        manager.advance_stage(job_id, JobStage.PREPROCESSING.value)

        search_root = staged_path if staged_path.is_dir() else staged_path.parent
        asset_index: Dict[str, Path] = {}
        for assets_dir in find_assets_dirs(search_root):
            asset_index.update(build_asset_index(assets_dir))
        logger.info(f"Job {job_id}: Asset index: {len(asset_index)} file(s).")

        if cancel_check():
            raise CancellationError("Job cancelled during preprocessing")

        # ── PARSING + CHUNKING + INDEXING (per page, concurrent) ────────────
        manager.advance_stage(job_id, JobStage.PARSING.value)

        registry = CapabilityRegistry()
        chunker = registry.get_instance("chunker")

        # Try to get VLM describer (optional)
        vlm = None
        try:
            vlm = registry.get_instance("vlm_describer")
        except KeyError:
            pass

        total_pages = 0
        total_new = 0
        total_dedup = 0

        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futures = []

            for xml_path in xml_files:
                logger.info(f"Job {job_id}: Parsing {xml_path}...")

                for page in parse_export(xml_path, namespaces=namespaces):
                    future = pool.submit(
                        _process_page,
                        page_title=page.title,
                        page_id=page.page_id,
                        page_text=page.text,
                        file_references=page.file_references,
                        xml_path=xml_path,
                        user_metadata=user_metadata,
                        kb_id=kb_id,
                        asset_index=asset_index,
                        dedup_store=dedup_store,
                        chunker=chunker,
                        vlm=vlm,
                        cancel_check=cancel_check,
                        job_id=job_id,
                    )
                    futures.append(future)

            # Collect results as they complete
            for future in as_completed(futures):
                result = future.result()   # propagates exceptions
                if result.is_new or result.is_dedup:
                    total_pages += 1
                if result.is_new:
                    total_new += 1
                if result.is_dedup:
                    total_dedup += 1

        manager.complete_job(job_id)
        logger.info(
            f"Job {job_id}: MediaWiki export processing complete — "
            f"{total_pages} page(s), {total_new} new, {total_dedup} deduplicated."
        )

    except CancellationError:
        manager.mark_cancelled(job_id)
        logger.info(f"Job {job_id} cancelled during MediaWiki processing")
    except Exception as e:
        manager.fail_job(job_id, str(e))
        logger.error(f"Job {job_id} failed: {e}")
