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
v2 document ingestion endpoints.

Provides a multi-tool, routing-based ingestion pipeline:

    DETECTING → PREPROCESSING → PARSING → NORMALIZATION → CHUNKING → INDEXING

- **DETECTING**:      Tika REST — MIME detection + metadata + scanned-PDF heuristic
- **PREPROCESSING**:   OCRmyPDF — add text layer to scanned PDFs
- **PARSING**:         Docling (or configurable primary parser) — structural extraction
- **NORMALIZATION**:   CanonicalRecord → ParsedDocument + VLM image enrichment
- **CHUNKING**:        DefaultChunker — recursive text splitting
- **INDEXING**:        Qdrant upsert — embed + store
"""

import json as _json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Response, UploadFile, status

from retriva.config import settings
from retriva.domain.models import CanonicalRecord, ParsedDocument
from retriva.indexing.qdrant_store import (
    get_client, upsert_chunks, delete_chunks_by_doc_id,
    delete_chunks_by_metadata, update_payload_by_doc_id, COLLECTION_NAME,
    list_documents as list_documents_store,
    count_documents as count_documents_store,
)
from retriva.ingestion.dedup import (
    DeduplicationStore, compute_content_hash, derive_doc_id,
    merge_metadata, merge_source_paths,
)
from qdrant_client.models import Filter, FieldCondition, MatchValue
from retriva.ingestion.normalize import normalize_text
from retriva.ingestion_api.job_manager import CancellationError, JobManager
from retriva.ingestion_api.schemas import UserMetadataValidationError, validate_user_metadata, DeleteMetadataRequest
from retriva.ingestion_api.deps import require_kb_exists, require_kbs_exist
from retriva.ingestion_api.schemas_v2 import (
    DocumentIngestRequestV2,
    IngestResponseV2,
    JobStage,
    DocumentListResponse,
    DocumentCountResponse,
    DocumentResponse,
    DocumentFilterRequest,
    DocumentSearchRequest,
    MediaWikiExportRequestV2,
)
from retriva.logger import get_logger
from retriva.registry import CapabilityRegistry
from retriva.profiler import Profiler
import time

# Import modules to trigger default registrations
import retriva.ingestion.chunker              # noqa: F401 — registers DefaultChunker
import retriva.ingestion.tika_client          # noqa: F401 — registers TikaClient
import retriva.ingestion.ocrmypdf_preprocessor  # noqa: F401 — registers OCRmyPDFPreprocessor
import retriva.ingestion.parser_router        # noqa: F401 — registers parser:default
import retriva.ingestion.docling_parser       # noqa: F401 — registers parser:docling

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v2/documents", tags=["v2-documents"])


# ---------------------------------------------------------------------------
# CanonicalRecord → ParsedDocument conversion
# ---------------------------------------------------------------------------

def records_to_parsed_document(
    records: List[CanonicalRecord],
    source_uri: str,
    metadata: Optional[Dict[str, str]],
    language: str = "en",
    page_title: str = "",
) -> ParsedDocument:
    """Convert a list of ``CanonicalRecord`` objects to a ``ParsedDocument``.

    Groups text/heading/table records into a single content string,
    preserving heading hierarchy as section markers.  Image records
    are included separately for image-chunk creation.

    Args:
        records:    CanonicalRecords from the PARSING stage.
        source_uri: Original document path/URI.
        metadata:   User-provided metadata to propagate.
        language:   Detected language (from Tika or parser).
        page_title: Document title (from Tika metadata or parser).

    Returns:
        A ``ParsedDocument`` ready for the chunker.
    """
    from retriva.domain.models import ImageContext

    text_parts: List[str] = []
    images: list = []

    for record in records:
        if record.element_type == "image":
            # Image records are handled as ImageContext for the chunker
            images.append(ImageContext(
                src=record.image_path or record.source_uri,
                alt="",
                caption="",
                surrounding_text=record.text[:200] if record.text else "",
                vlm_description=record.text if record.text else "",
            ))
        elif record.element_type == "heading":
            # Preserve headings as markdown-style markers
            level = len(record.heading_path) + 1
            prefix = "#" * min(level, 4)
            text_parts.append(f"{prefix} {record.text}")
        elif record.element_type == "table":
            # Use markdown table if available, otherwise raw text
            table_text = record.table_markdown or record.text
            text_parts.append(table_text)
        else:
            text_parts.append(record.text)

    full_text = "\n\n".join(text_parts)

    # Derive title: use explicit page_title, or first heading, or filename
    if not page_title:
        for r in records:
            if r.element_type == "heading" and r.text.strip():
                page_title = r.text.strip()
                break
        if not page_title:
            page_title = Path(source_uri).stem.replace("_", " ").replace("-", " ").title()

    return ParsedDocument(
        source_path=source_uri,
        canonical_doc_id=source_uri,
        page_title=page_title,
        content_text=full_text,
        language=language,
        images=images,
        user_metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Shared background worker — multi-tool pipeline
# ---------------------------------------------------------------------------

def process_document_v2(
    source_uri: str,
    content_type: Optional[str],
    user_metadata: Optional[Dict[str, str]],
    parser_hint: Optional[str],
    job_id: str,
    temp_path: Optional[str] = None,
    doc_id: Optional[str] = None,
    content_hash: Optional[str] = None,
    kb_id: str = "default",
    source_paths: Optional[List[str]] = None,
    content_size: Optional[int] = None,
    ingestion_status: str = "completed",
    created_at: Optional[str] = None,
):
    """Execute the 6-stage v2 ingestion pipeline in a background thread."""
    manager = JobManager()
    manager.start_job(job_id)
    cancel_check = lambda: manager.is_cancel_requested(job_id)
    parse_source = temp_path or source_uri
    ocr_temp_path = None
    dedup_store = DeduplicationStore()

    try:
        registry = CapabilityRegistry()

        manager.advance_stage(job_id, JobStage.DETECTING.value)
        tika = registry.get_instance("tika_client")
        if tika.health_check():
            detection = tika.detect(parse_source)
        else:
            from retriva.ingestion.parser_router import DefaultParserRouter
            fallback_router = DefaultParserRouter()
            detected_mime = fallback_router.detect_content_type(source_uri, hint=content_type)
            from retriva.ingestion.tika_client import TikaDetectionResult
            detection = TikaDetectionResult(content_type=detected_mime)
            logger.warning(f"Job {job_id}: Tika unavailable, fallback: {detected_mime}")

        if content_type:
            detection.content_type = content_type
        if not temp_path and not os.path.exists(parse_source):
            raise FileNotFoundError(f"Source not found: {parse_source}")
        if cancel_check():
            raise CancellationError("Job cancelled during detection")

        manager.advance_stage(job_id, JobStage.PREPROCESSING.value)
        preprocessor = registry.get_instance("ocrmypdf_preprocessor")
        if preprocessor.needs_ocr(detection):
            ocr_fd, ocr_temp_path = tempfile.mkstemp(suffix=".pdf")
            os.close(ocr_fd)
            if preprocessor.preprocess(parse_source, ocr_temp_path, cancel_check):
                parse_source = ocr_temp_path
        if cancel_check():
            raise CancellationError("Job cancelled during preprocessing")

        manager.advance_stage(job_id, JobStage.PARSING.value)
        if detection.content_type.startswith("image/"):
            parser_key = "v2_image_handler"
            records = [CanonicalRecord(
                document_id=source_uri, element_type="image", text="",
                source_uri=source_uri, parser_name="v2_image_handler",
                image_path=parse_source,
            )]
        else:
            parser_key = f"parser:{parser_hint}" if parser_hint else f"parser:{settings.v2_primary_parser}"
            try:
                parser = registry.get_instance(parser_key)
            except KeyError:
                parser = registry.get_instance("parser:default")
            records: List[CanonicalRecord] = parser.parse(parse_source, detection.content_type, cancel_check)

        if cancel_check():
            raise CancellationError("Job cancelled after parsing")
        logger.info(f"Job {job_id}: parser '{parser_key}' produced {len(records)} records")

        manager.advance_stage(job_id, JobStage.NORMALIZATION.value)
        try:
            vlm = registry.get_instance("vlm_describer")
            for record in records:
                if cancel_check():
                    raise CancellationError("Cancelled during VLM enrichment")
                if record.element_type == "image" and record.image_path:
                    image_file = Path(record.image_path)
                    if image_file.is_file():
                        description = vlm.describe(image_file)
                        if description:
                            record.text = description
        except KeyError:
            pass

        page_title = detection.metadata.get("dc:title", "") or detection.metadata.get("title", "")
        language = detection.language or "en"
        normalized = records_to_parsed_document(records, source_uri, user_metadata, language, page_title)
        # Attach dedup fields to the ParsedDocument so the chunker can propagate them
        normalized.doc_id = doc_id
        normalized.kb_id = kb_id
        normalized.content_hash = content_hash
        normalized.source_paths = source_paths or [source_uri]
        normalized.filename = Path(source_uri).name
        normalized.content_size = content_size
        normalized.ingestion_status = ingestion_status
        normalized.created_at = created_at
        normalized.content_text = normalize_text(normalized.content_text)

        if not normalized.content_text.strip() and not normalized.images:
            logger.warning(f"Job {job_id}: empty content after normalization — skipping.")
            manager.complete_job(job_id)
            return
        if cancel_check():
            raise CancellationError("Job cancelled during normalization")

        manager.advance_stage(job_id, JobStage.CHUNKING.value)
        chunks = registry.get_instance("chunker").create_chunks(normalized)
        if cancel_check():
            raise CancellationError("Job cancelled during chunking")

        manager.advance_stage(job_id, JobStage.INDEXING.value)
        client = get_client()
        upsert_chunks(client, chunks, cancel_check=cancel_check)

        # Finalise the catalog record
        if doc_id:
            try:
                dedup_store.finalize_record(doc_id, chunk_count=len(chunks))
            except KeyError:
                pass  # record may not exist for non-upload paths

        manager.complete_job(job_id)
        logger.info(f"new_document_ingestion_started: job={job_id}, doc_id={doc_id}, "
                    f"kb_id={kb_id}, content_hash={content_hash}, source_path={source_uri}, "
                    f"chunks={len(chunks)}, deduplicated=false, metadata_updated=false")

    except CancellationError:
        manager.mark_cancelled(job_id)
    except Exception as e:
        manager.fail_job(job_id, str(e))
        logger.error(f"Job {job_id} failed: {e}")
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
        if ocr_temp_path and os.path.exists(ocr_temp_path):
            os.remove(ocr_temp_path)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
from fastapi import Query, Request

def _parse_and_validate_metadata(user_metadata_filter: Optional[str], request: Request) -> Optional[dict]:
    parsed = {}
    if user_metadata_filter:
        try:
            parsed = _json.loads(user_metadata_filter)
        except _json.JSONDecodeError:
            raise HTTPException(
                status_code=422,
                detail=[{"field": "user_metadata_filter", "msg": "Invalid JSON"}],
            )
            
    for key, value in request.query_params.items():
        if key.startswith("metadata."):
            metadata_key = key[len("metadata."):]
            parsed[metadata_key] = value
            
    if not parsed:
        return None
        
    try:
        validate_user_metadata(parsed)
    except UserMetadataValidationError as e:
        raise HTTPException(status_code=422, detail=e.details)
        
    return parsed

@router.get("", response_model=DocumentListResponse)
async def list_documents(
    request: Request,
    user_metadata_filter: Optional[str] = Query(None, description="JSON-encoded user metadata filter")
):
    """List unique documents from the vector store, optionally filtered by user_metadata."""
    parsed_filter = _parse_and_validate_metadata(user_metadata_filter, request)
    
    try:
        client = get_client()
        docs = list_documents_store(client, metadata_filter=parsed_filter)
        
        return DocumentListResponse(
            documents=[DocumentResponse(**d) for d in docs],
            total=len(docs)
        )
    except Exception as e:
        logger.error(f"Error listing documents: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )


@router.post("/filter", response_model=DocumentListResponse)
async def list_documents_filtered(
    payload: DocumentFilterRequest
):
    """List unique documents from the vector store using a POST filter body."""
    metadata = {}
    if payload.metadata_filter and payload.metadata_filter.user_metadata:
        metadata = payload.metadata_filter.user_metadata
    
    # Validate metadata if present
    if metadata:
        try:
            validate_user_metadata(metadata)
        except UserMetadataValidationError as e:
            raise HTTPException(status_code=422, detail=e.details)

    try:
        client = get_client()
        docs = list_documents_store(client, metadata_filter=metadata)
        
        return DocumentListResponse(
            documents=[DocumentResponse(**d) for d in docs],
            total=len(docs)
        )
    except Exception as e:
        logger.error(f"Error filtering documents: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )


@router.get("/count", response_model=DocumentCountResponse)
async def count_documents(
    request: Request,
    user_metadata_filter: Optional[str] = Query(None, description="JSON-encoded user metadata filter")
):
    """Count unique documents in the vector store, optionally filtered by user_metadata."""
    parsed_filter = _parse_and_validate_metadata(user_metadata_filter, request)
    
    try:
        client = get_client()
        count = count_documents_store(client, metadata_filter=parsed_filter)
        return DocumentCountResponse(count=count)
    except Exception as e:
        logger.error(f"Error counting documents: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )


@router.get("/{doc_id}", response_model=DocumentResponse)
async def get_document(doc_id: str):
    """Get a specific document by ID."""
    try:
        client = get_client()
        docs = list_documents_store(client, doc_id=doc_id)
        if not docs:
            raise HTTPException(status_code=404, detail="Document not found")
        return DocumentResponse(**docs[0])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting document {doc_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )


@router.post("/search", response_model=DocumentListResponse)
async def search_documents_v2(request: DocumentSearchRequest):
    """
    Search for unique documents based on semantic query and metadata filters.
    Returns document-level results with match reasons.
    """
    # KB enforcement (SDD): every kb_id must exist; empty list → "all KBs"
    # (existing semantics preserved).
    require_kbs_exist(request.kb_ids, allow_empty=True)
    start_time = time.time()
    
    try:
        # Start profiler for structured logging and request_id propagation
        profiler = Profiler.start_request()
        
        logger.info(
            f"[{profiler.request_id}] document_search_requested: query='{request.query[:50]}...', "
            f"mode={request.metadata_filter_mode}, "
            f"filters_count={len(request.metadata_filters)}, "
            f"is_discovery={request.is_discovery}"
        )
        if request.is_discovery:
            logger.debug(
                f"[{profiler.request_id}] discovery mode: metadata_filter_mode is ignored "
                f"(tags are always applied as strict filters)"
            )
        
        client = get_client()
        filters = [f.model_dump() for f in request.metadata_filters]
        
        from retriva.indexing.qdrant_store import search_documents
        docs = search_documents(
            client=client,
            query=request.query,
            limit=request.limit,
            metadata_filters=filters,
            metadata_filter_mode=request.metadata_filter_mode.value,
            kb_ids=request.kb_ids,
            is_discovery=request.is_discovery,
            case_sensitive=request.case_sensitive
        )
        
        duration_ms = int((time.time() - start_time) * 1000)
        profiler.mark_phase("document_search_finished")
        profiler.finalize()
        
        logger.info(
            f"[{profiler.request_id}] document_search_completed: results={len(docs)}, "
            f"duration_ms={duration_ms}"
        )
        
        return DocumentListResponse(
            documents=[DocumentResponse(**d) for d in docs],
            total=len(docs)
        )
    except Exception as e:
        import traceback
        error_msg = f"{e}\n{traceback.format_exc()}"
        logger.error(f"Error searching documents: {error_msg}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_msg,
        )



@router.post(
    "",
    response_model=IngestResponseV2,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_document_v2(
    payload: DocumentIngestRequestV2,
    background_tasks: BackgroundTasks,
) -> IngestResponseV2:
    """Generic multi-parser ingestion (JSON body with ``source_uri``)."""
    # KB enforcement (SDD): unknown kb_id → 404 before any work is scheduled.
    require_kb_exists(payload.kb_id)
    logger.debug(f"v2 ingest request: source_uri={payload.source_uri} kb_id={payload.kb_id}")
    manager = JobManager()
    job = manager.create_job(source=payload.source_uri, job_type="v2_document")
    background_tasks.add_task(
        process_document_v2,
        payload.source_uri,
        payload.content_type,
        payload.user_metadata,
        payload.parser_hint,
        job.id,
        kb_id=payload.kb_id,
        # No size/status/created_at for generic ingest (will use defaults)
    )
    return IngestResponseV2(
        status="accepted",
        message="Document accepted for processing",
        job_id=job.id,
    )


@router.post(
    "/mediawiki",
    response_model=IngestResponseV2,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_mediawiki_export_v2(
    payload: MediaWikiExportRequestV2,
    background_tasks: BackgroundTasks,
) -> IngestResponseV2:
    """Ingest a MediaWiki XML export directory with per-page granularity.

    Accepts a local ``staged_dir`` path containing XML export files and
    optional ``assets/`` subdirectories. Each wiki page is processed as
    a separate document with content-hash deduplication.
    """
    require_kb_exists(payload.kb_id)
    logger.info(
        f"v2 MediaWiki export request: staged_dir={payload.staged_dir} "
        f"kb_id={payload.kb_id}"
    )
    manager = JobManager()
    job = manager.create_job(source=payload.staged_dir, job_type="v2_mediawiki")

    from retriva.ingestion.mediawiki_v2_parser import process_mediawiki_export

    background_tasks.add_task(
        process_mediawiki_export,
        payload.staged_dir,
        payload.user_metadata,
        payload.kb_id,
        lambda: manager.is_cancel_requested(job.id),
        job.id,
    )
    return IngestResponseV2(
        status="accepted",
        message="MediaWiki export accepted for processing",
        job_id=job.id,
    )


@router.post(
    "/upload",
    response_model=IngestResponseV2,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document_v2(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    source_path: str = Form(...),
    content_type: str = Form(None),
    user_metadata: str = Form(None),
    kb_id: str = Form("default"),
) -> IngestResponseV2:
    """Multipart file upload with content-hash deduplication.

    On first upload: full 6-stage pipeline, returns status='accepted'.
    On duplicate (same kb_id + SHA-256): merges metadata/paths, patches
    Qdrant payloads, returns status='already_exists' or 'metadata_updated'.
    """
    # KB enforcement (SDD): unknown kb_id → 404 before any I/O.
    require_kb_exists(kb_id)
    # -- 1. Parse metadata --------------------------------------------------
    parsed_metadata = None
    if user_metadata:
        try:
            parsed_metadata = _json.loads(user_metadata)
        except _json.JSONDecodeError:
            raise HTTPException(status_code=422, detail=[{"field": "user_metadata", "msg": "Invalid JSON"}])
        try:
            validate_user_metadata(parsed_metadata)
        except UserMetadataValidationError as e:
            raise HTTPException(status_code=422, detail=e.details)

    # -- 2. Read bytes + compute hash BEFORE saving to temp -----------------
    file_bytes = await file.read()
    content_hash = compute_content_hash(file_bytes)
    hex_digest = content_hash.split(":", 1)[1]  # strip "sha256:"
    doc_id = derive_doc_id(kb_id, hex_digest)
    content_size = len(file_bytes)
    filename = file.filename or Path(source_path).name

    logger.info(
        f"file_hash_computed: kb_id={kb_id}, doc_id={doc_id}, "
        f"content_hash={content_hash}, source_path={source_path}, "
        f"filename={filename}, size={content_size}"
    )

    # -- 3. Dedup lookup ----------------------------------------------------
    dedup_store = DeduplicationStore()
    existing = dedup_store.get_by_hash(kb_id, content_hash, collection_name=COLLECTION_NAME)

    if existing is not None:
        # ── Duplicate path ──────────────────────────────────────────────────
        logger.info(
            f"duplicate_document_detected: doc_id={doc_id}, kb_id={kb_id}, "
            f"content_hash={content_hash}, source_path={source_path}, "
            f"deduplicated=true"
        )

        merged_meta, meta_changed = merge_metadata(
            existing.user_metadata, parsed_metadata, doc_id, kb_id
        )
        merged_paths, paths_changed = merge_source_paths(
            existing.source_paths, source_path, doc_id, kb_id
        )
        any_changed = meta_changed or paths_changed

        if any_changed:
            dedup_store.update_record(
                doc_id=doc_id,
                merged_metadata=merged_meta,
                merged_source_paths=merged_paths,
            )
            # Patch Qdrant payloads (no re-embedding)
            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).isoformat()
            client = get_client()
            update_payload_by_doc_id(client, doc_id, {
                "user_metadata": merged_meta,
                "source_paths": merged_paths,
                "content_hash": content_hash,
                "content_hash_algorithm": "sha256",
                "metadata_updated_at": now_iso,
            })
            logger.info(
                f"duplicate_document_ingestion_skipped: doc_id={doc_id}, kb_id={kb_id}, "
                f"content_hash={content_hash}, source_path={source_path}, "
                f"deduplicated=true, metadata_updated=true"
            )
            return IngestResponseV2(
                status="metadata_updated",
                message="Document already exists; metadata and source paths were updated.",
                doc_id=doc_id,
                content_hash=content_hash,
                deduplicated=True,
                chunks_reused=True,
                metadata_updated=True,
            )
        else:
            logger.info(
                f"duplicate_document_ingestion_skipped: doc_id={doc_id}, kb_id={kb_id}, "
                f"content_hash={content_hash}, source_path={source_path}, "
                f"deduplicated=true, metadata_updated=false"
            )
            return IngestResponseV2(
                status="already_exists",
                message="Document already exists in this knowledge base.",
                doc_id=doc_id,
                content_hash=content_hash,
                deduplicated=True,
                chunks_reused=True,
                metadata_updated=False,
            )

    # ── New document path ───────────────────────────────────────────────────
    from retriva.domain.models import DocRecord
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()

    record = DocRecord(
        doc_id=doc_id,
        kb_id=kb_id,
        collection_name=COLLECTION_NAME,
        content_hash=content_hash,
        content_size=content_size,
        mime_type=content_type,
        filename=filename,
        source_paths=[source_path],
        user_metadata=parsed_metadata,
        ingestion_status="pending",
        created_at=now_iso,
        updated_at=now_iso,
    )
    dedup_store.create_record(record)

    manager = JobManager()
    job = manager.create_job(source=source_path, job_type="v2_upload")

    # Save bytes to temp file
    suffix = os.path.splitext(filename)[1] or ""
    temp_fd, temp_path = tempfile.mkstemp(suffix=suffix)
    os.close(temp_fd)
    with open(temp_path, "wb") as f:
        f.write(file_bytes)

    background_tasks.add_task(
        process_document_v2,
        source_path,
        content_type,
        parsed_metadata,
        None,  # parser_hint
        job.id,
        temp_path=temp_path,
        doc_id=doc_id,
        content_hash=content_hash,
        kb_id=kb_id,
        source_paths=[source_path],
        content_size=content_size,
        ingestion_status="completed",
        created_at=record.created_at,
    )

    logger.info(
        f"new_document_ingestion_started: doc_id={doc_id}, kb_id={kb_id}, "
        f"content_hash={content_hash}, source_path={source_path}, "
        f"deduplicated=false, metadata_updated=false"
    )
    return IngestResponseV2(
        status="accepted",
        message=f"File '{filename}' accepted for processing.",
        job_id=job.id,
        doc_id=doc_id,
        content_hash=content_hash,
        deduplicated=False,
        chunks_reused=False,
        metadata_updated=False,
    )


@router.delete("/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document_v2(doc_id: str):
    """
    Delete a document and all its chunks from the vector store (v2).
    
    This endpoint is idempotent. If the document does not exist, it logs 
    an informative message and returns 204 No Content.
    """
    logger.debug(f"Received request to delete document (v2): {doc_id}")
    client = get_client()
    
    try:
        # Check if any chunks exist for this doc_id
        hits, _ = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(
                must=[
                    FieldCondition(
                        key="doc_id",
                        match=MatchValue(value=doc_id),
                    )
                ]
            ),
            limit=1,
            with_payload=False,
            with_vectors=False
        )
        
        if not hits:
            logger.info(f"document not present; skipping doc_id={doc_id} (v2)")
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        delete_chunks_by_doc_id(client, doc_id)
        logger.info(f"retriva_deleted doc_id={doc_id} (v2)")
        return Response(status_code=status.HTTP_204_NO_CONTENT)
        
    except Exception as e:
        logger.error(f"Error during document deletion (v2) for {doc_id}: {e}")
        return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/metadata/filter", status_code=status.HTTP_204_NO_CONTENT)
async def delete_documents_by_metadata_v2(request: DeleteMetadataRequest):
    """
    Delete all chunks from the vector store that match the given user_metadata filter (v2).
    """
    logger.debug(f"Received request to delete chunks by metadata (v2): {request.user_metadata_filter}")
    client = get_client()
    
    try:
        delete_chunks_by_metadata(client, request.user_metadata_filter)
        logger.info(f"retriva_deleted chunks by metadata (v2): {request.user_metadata_filter}")
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except Exception as e:
        logger.error(f"Error during chunk deletion by metadata (v2) {request.user_metadata_filter}: {e}")
        return Response(status_code=status.HTTP_204_NO_CONTENT)
