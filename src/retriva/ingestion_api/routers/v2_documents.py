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
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Response, UploadFile, status

from retriva.config import settings
from retriva.domain.models import CanonicalRecord, ParsedDocument
from retriva.indexing.qdrant_store import get_client, upsert_chunks, delete_chunks_by_source_path, delete_chunks_by_metadata, COLLECTION_NAME, list_documents as list_documents_store, count_documents as count_documents_store
from qdrant_client.models import Filter, FieldCondition, MatchValue
from retriva.ingestion.normalize import normalize_text
from retriva.ingestion_api.job_manager import CancellationError, JobManager
from retriva.ingestion_api.schemas import UserMetadataValidationError, validate_user_metadata, DeleteMetadataRequest
from retriva.ingestion_api.schemas_v2 import (
    DocumentIngestRequestV2,
    IngestResponseV2,
    JobStage,
    DocumentListResponse,
    DocumentCountResponse,
    DocumentResponse,
)
from retriva.logger import get_logger
from retriva.registry import CapabilityRegistry

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
):
    """Execute the 6-stage v2 ingestion pipeline in a background thread.

    Stages: DETECTING → PREPROCESSING → PARSING → NORMALIZATION → CHUNKING → INDEXING
    """
    manager = JobManager()
    manager.start_job(job_id)
    cancel_check = lambda: manager.is_cancel_requested(job_id)
    parse_source = temp_path or source_uri
    ocr_temp_path = None  # Track OCR output for cleanup

    try:
        registry = CapabilityRegistry()

        # ── Stage 1: DETECTING ───────────────────────────────────────
        manager.advance_stage(job_id, JobStage.DETECTING.value)

        tika = registry.get_instance("tika_client")
        if tika.health_check():
            detection = tika.detect(parse_source)
            logger.debug(
                f"Job {job_id}: Tika detected type={detection.content_type}, "
                f"scanned={detection.is_scanned}"
            )
        else:
            # Tika unavailable — fall back to extension-based detection
            from retriva.ingestion.parser_router import DefaultParserRouter
            fallback_router = DefaultParserRouter()
            detected_mime = fallback_router.detect_content_type(
                source_uri, hint=content_type
            )
            from retriva.ingestion.tika_client import TikaDetectionResult
            detection = TikaDetectionResult(content_type=detected_mime)
            logger.warning(
                f"Job {job_id}: Tika unavailable, using extension-based detection: "
                f"{detected_mime}"
            )

        # Override with explicit content_type hint if provided
        if content_type:
            detection.content_type = content_type

        # Validate source exists for local paths
        if not temp_path and not os.path.exists(parse_source):
            raise FileNotFoundError(f"Source not found: {parse_source}")

        if cancel_check():
            raise CancellationError("Job cancelled during detection")

        # ── Stage 2: PREPROCESSING ───────────────────────────────────
        manager.advance_stage(job_id, JobStage.PREPROCESSING.value)

        preprocessor = registry.get_instance("ocrmypdf_preprocessor")
        if preprocessor.needs_ocr(detection):
            ocr_fd, ocr_temp_path = tempfile.mkstemp(suffix=".pdf")
            os.close(ocr_fd)
            success = preprocessor.preprocess(
                parse_source, ocr_temp_path, cancel_check
            )
            if success:
                parse_source = ocr_temp_path
                logger.info(f"Job {job_id}: using OCR'd PDF for parsing")
            else:
                logger.warning(
                    f"Job {job_id}: OCR preprocessing failed, "
                    f"continuing with original"
                )

        if cancel_check():
            raise CancellationError("Job cancelled during preprocessing")

        # ── Stage 3: PARSING ─────────────────────────────────────────
        manager.advance_stage(job_id, JobStage.PARSING.value)

        if detection.content_type.startswith("image/"):
            logger.info(f"Job {job_id}: file is a standalone image, bypassing primary parser.")
            parser_key = "v2_image_handler"
            records = [
                CanonicalRecord(
                    document_id=source_uri,
                    element_type="image",
                    text="",
                    source_uri=source_uri,
                    parser_name="v2_image_handler",
                    image_path=parse_source,
                )
            ]
        else:
            parser_key = f"parser:{parser_hint}" if parser_hint else f"parser:{settings.v2_primary_parser}"
            try:
                parser = registry.get_instance(parser_key)
            except KeyError:
                if parser_hint:
                    logger.warning(
                        f"Job {job_id}: unknown parser_hint '{parser_hint}', "
                        f"falling back to '{settings.v2_primary_parser}'"
                    )
                    try:
                        parser = registry.get_instance(
                            f"parser:{settings.v2_primary_parser}"
                        )
                    except KeyError:
                        logger.warning(
                            f"Job {job_id}: primary parser '{settings.v2_primary_parser}' "
                            f"not available, using built-in default"
                        )
                        parser = registry.get_instance("parser:default")
                else:
                    logger.warning(
                        f"Job {job_id}: primary parser '{settings.v2_primary_parser}' "
                        f"not available, using built-in default"
                    )
                    parser = registry.get_instance("parser:default")

            records: List[CanonicalRecord] = parser.parse(
                parse_source, detection.content_type, cancel_check
            )

        if cancel_check():
            raise CancellationError("Job cancelled after parsing")

        logger.info(
            f"Job {job_id}: parser '{parser_key}' produced {len(records)} records"
        )

        # ── Stage 4: NORMALIZATION ───────────────────────────────────
        manager.advance_stage(job_id, JobStage.NORMALIZATION.value)

        # 4a. VLM enrichment for image records
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
                            record.confidence = 1.0
                            logger.debug(f"VLM enriched image: {image_file.name}")
        except KeyError:
            logger.debug("No vlm_describer registered — skipping VLM enrichment")

        # 4b. Derive title from Tika metadata or parser output
        page_title = (
            detection.metadata.get("dc:title", "")
            or detection.metadata.get("title", "")
        )

        # 4c. Convert CanonicalRecords → ParsedDocument
        language = detection.language or "en"
        normalized = records_to_parsed_document(
            records, source_uri, user_metadata, language, page_title
        )

        # 4d. Normalize text content
        normalized.content_text = normalize_text(normalized.content_text)

        if not normalized.content_text.strip() and not normalized.images:
            logger.warning(
                f"Job {job_id}: empty content and no images after normalization — skipping."
            )
            manager.complete_job(job_id)
            return

        if cancel_check():
            raise CancellationError("Job cancelled during normalization")

        # ── Stage 5: CHUNKING ────────────────────────────────────────
        manager.advance_stage(job_id, JobStage.CHUNKING.value)
        chunks = registry.get_instance("chunker").create_chunks(normalized)

        if cancel_check():
            raise CancellationError("Job cancelled during chunking")

        # ── Stage 6: INDEXING ────────────────────────────────────────
        manager.advance_stage(job_id, JobStage.INDEXING.value)
        client = get_client()
        upsert_chunks(client, chunks, cancel_check=cancel_check)

        manager.complete_job(job_id)
        logger.info(
            f"Job {job_id} completed for '{source_uri}' "
            f"({len(chunks)} chunks indexed)"
        )

    except CancellationError:
        manager.mark_cancelled(job_id)
        logger.info(f"Job {job_id} cancelled during v2 processing")
    except Exception as e:
        manager.fail_job(job_id, str(e))
        logger.error(f"Job {job_id} failed: {e}")
    finally:
        # Clean up all temp files
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
    logger.debug(f"v2 ingest request: source_uri={payload.source_uri}")
    manager = JobManager()
    job = manager.create_job(source=payload.source_uri, job_type="v2_document")
    background_tasks.add_task(
        process_document_v2,
        payload.source_uri,
        payload.content_type,
        payload.user_metadata,
        payload.parser_hint,
        job.id,
    )
    return IngestResponseV2(
        status="accepted",
        message="Document accepted for processing",
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
) -> IngestResponseV2:
    """Generic multi-parser ingestion (multipart file upload)."""
    logger.debug(f"v2 upload request: filename={file.filename}")

    # Deserialise JSON-encoded user_metadata from form field
    parsed_metadata = None
    if user_metadata:
        try:
            parsed_metadata = _json.loads(user_metadata)
        except _json.JSONDecodeError:
            raise HTTPException(
                status_code=422,
                detail=[{"field": "user_metadata", "msg": "Invalid JSON in user_metadata form field"}],
            )
        try:
            validate_user_metadata(parsed_metadata)
        except UserMetadataValidationError as e:
            raise HTTPException(status_code=422, detail=e.details)

    manager = JobManager()
    job = manager.create_job(source=source_path, job_type="v2_upload")

    # Save the uploaded file to a temporary location
    suffix = os.path.splitext(file.filename or "")[1] or ""
    temp_fd, temp_path = tempfile.mkstemp(suffix=suffix)
    os.close(temp_fd)

    with open(temp_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    background_tasks.add_task(
        process_document_v2,
        source_path,
        content_type,
        parsed_metadata,
        None,  # parser_hint
        job.id,
        temp_path=temp_path,
    )

    return IngestResponseV2(
        status="accepted",
        message=f"File '{file.filename}' accepted for processing",
        job_id=job.id,
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
        # Check if any chunks exist for this source_path
        hits, _ = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(
                must=[
                    FieldCondition(
                        key="source_path",
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

        delete_chunks_by_source_path(client, doc_id)
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
