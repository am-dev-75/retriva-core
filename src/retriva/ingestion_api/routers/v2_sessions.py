# Copyright (C) 2026 Andrea Marson (am-dev-75@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
v2 session attachment & artifact endpoints.

Generic, CRM-agnostic chat-session document processing.  Attachments are
parsed but NEVER ingested into the persistent knowledge base.  Artifacts
are session-scoped generated output files.

Routes:
- POST   /api/v2/sessions/{session_id}/attachments          (upload)
- GET    /api/v2/sessions/{session_id}/attachments          (list)
- GET    /api/v2/sessions/{session_id}/attachments/{aid}    (metadata)
- POST   /api/v2/sessions/{session_id}/attachments/{aid}/parse  (parse)
- GET    /api/v2/sessions/{session_id}/attachments/{aid}/parsed (get parsed repr)
- DELETE /api/v2/sessions/{session_id}/attachments/{aid}    (delete)
- GET    /api/v2/sessions/{session_id}/artifacts            (list)
- GET    /api/v2/sessions/{session_id}/artifacts/{art_id}   (metadata)
- GET    /api/v2/sessions/{session_id}/artifacts/{art_id}/content  (download)
- DELETE /api/v2/sessions/{session_id}/artifacts/{art_id}   (delete)
"""

from typing import List, Optional

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import Response

from retriva.logger import get_logger
from retriva.session.artifacts import SessionArtifactService
from retriva.session.attachments import AttachmentService, AttachmentValidationError
from retriva.session.models import (
    ArtifactStatus,
    AttachmentRecord,
    AttachmentStatus,
    ParsedAttachment,
    SessionArtifactRecord,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v2/sessions", tags=["v2-sessions"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _attachment_to_response(rec: AttachmentRecord) -> dict:
    return {
        "attachment_id": rec.attachment_id,
        "session_id": rec.session_id,
        "original_filename": rec.original_filename,
        "media_type": rec.media_type,
        "size": rec.size,
        "checksum": rec.checksum,
        "upload_time": rec.upload_time,
        "expiration_time": rec.expiration_time,
        "status": rec.status.value,
        "selected_parser": rec.selected_parser,
        "parsed_element_count": rec.parsed_element_count,
        "parse_warnings": rec.parse_warnings,
        "failure_info": rec.failure_info,
    }


def _artifact_to_response(rec: SessionArtifactRecord) -> dict:
    return {
        "artifact_id": rec.artifact_id,
        "session_id": rec.session_id,
        "filename": rec.filename,
        "media_type": rec.media_type,
        "size": rec.size,
        "created_at": rec.created_at,
        "expiration_time": rec.expiration_time,
        "status": rec.status.value,
        "source_attachment_id": rec.source_attachment_id,
        "artifact_kind": rec.artifact_kind,
        "metadata": rec.metadata,
        "failure_info": rec.failure_info,
    }


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

@router.post(
    "/{session_id}/attachments",
    status_code=status.HTTP_201_CREATED,
)
async def upload_attachment(
    session_id: str,
    file: UploadFile = File(...),
    owner: Optional[str] = Form(None),
):
    """Upload a session-scoped attachment.

    The file is validated, malware-scanned, and stored.  It is NOT parsed
    yet and NOT ingested into the knowledge base.  Call the ``/parse``
    endpoint to obtain a normalized parsed representation.
    """
    service = AttachmentService()
    content = await file.read()
    try:
        record = service.upload(
            session_id=session_id,
            filename=file.filename or "upload.bin",
            media_type=file.content_type or "application/octet-stream",
            content=content,
            owner=owner,
        )
    except AttachmentValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Attachment upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return _attachment_to_response(record)


@router.get("/{session_id}/attachments")
async def list_attachments(session_id: str):
    service = AttachmentService()
    return [_attachment_to_response(r) for r in service.list(session_id)]


@router.get("/{session_id}/attachments/{attachment_id}")
async def get_attachment(session_id: str, attachment_id: str):
    service = AttachmentService()
    rec = service.get(attachment_id, session_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return _attachment_to_response(rec)


@router.post("/{session_id}/attachments/{attachment_id}/parse")
async def parse_attachment(session_id: str, attachment_id: str):
    """Parse a stored attachment WITHOUT ingesting it.

    Reuses the existing Retriva parser registry.  Returns a normalized
    parsed representation (elements with source locations).
    """
    service = AttachmentService()
    try:
        parsed = service.parse(attachment_id, session_id)
    except AttachmentValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Attachment parse failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "attachment_id": parsed.attachment_id,
        "session_id": parsed.session_id,
        "media_type": parsed.media_type,
        "parser_name": parsed.parser_name,
        "parsed_at": parsed.parsed_at,
        "warnings": parsed.warnings,
        "elements": [e.model_dump() for e in parsed.elements],
    }


@router.get("/{session_id}/attachments/{attachment_id}/parsed")
async def get_parsed_attachment(session_id: str, attachment_id: str):
    """Re-parse and return the normalized representation (idempotent)."""
    return await parse_attachment(session_id, attachment_id)


@router.delete(
    "/{session_id}/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_attachment(session_id: str, attachment_id: str):
    service = AttachmentService()
    if not service.delete(attachment_id, session_id):
        raise HTTPException(status_code=404, detail="Attachment not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

@router.get("/{session_id}/artifacts")
async def list_artifacts(session_id: str):
    service = SessionArtifactService()
    return [_artifact_to_response(r) for r in service.list(session_id)]


@router.get("/{session_id}/artifacts/{artifact_id}")
async def get_artifact(session_id: str, artifact_id: str):
    service = SessionArtifactService()
    rec = service.get(artifact_id, session_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return _artifact_to_response(rec)


@router.get("/{session_id}/artifacts/{artifact_id}/content")
async def download_artifact(session_id: str, artifact_id: str):
    service = SessionArtifactService()
    rec = service.get(artifact_id, session_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if rec.status != ArtifactStatus.READY:
        raise HTTPException(
            status_code=409,
            detail=f"Artifact not ready (status={rec.status.value})",
        )
    content = service.read(artifact_id, session_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Artifact file missing")
    return Response(
        content=content,
        media_type=rec.media_type,
        headers={"Content-Disposition": f'attachment; filename="{rec.filename}"'},
    )


@router.delete(
    "/{session_id}/artifacts/{artifact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_artifact(session_id: str, artifact_id: str):
    service = SessionArtifactService()
    if not service.delete(artifact_id, session_id):
        raise HTTPException(status_code=404, detail="Artifact not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
