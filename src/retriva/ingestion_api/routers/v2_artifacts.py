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

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from fastapi.responses import FileResponse

from retriva.domain.artifacts import Artifact, ArtifactStatus, ArtifactCapabilities
from retriva.ingestion_api.job_manager import JobManager, JobStatus
from retriva.ingestion_api.schemas_v2 import (
    ArtifactRequestV2, 
    ArtifactResponseV2, 
    ArtifactCapabilitiesResponseV2,
    JobResponseV2
)
from retriva.infrastructure.storage import LocalStorageProvider
from retriva.rendering import get_renderer
from retriva.logger import get_logger

# Import renderers to trigger registration
import retriva.rendering.markdown_renderer       # noqa: F401
import retriva.rendering.pdf_renderer            # noqa: F401
import retriva.rendering.docx_renderer           # noqa: F401
import retriva.rendering.xlsx_renderer           # noqa: F401
import retriva.rendering.opendocument_renderer   # noqa: F401

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v2/artifacts", tags=["v2-artifacts"])

# Extension mapping
SUPPORTED_FORMATS = ["pdf", "markdown", "docx", "xlsx", "odt", "ods", "odp"]
SUPPORTED_TYPES = ["document_list", "basic_report"]

def map_job_status(job_status: JobStatus) -> ArtifactStatus:
    mapping = {
        JobStatus.PENDING: ArtifactStatus.QUEUED,
        JobStatus.RUNNING: ArtifactStatus.RUNNING,
        JobStatus.COMPLETED: ArtifactStatus.READY,
        JobStatus.FAILED: ArtifactStatus.FAILED,
        JobStatus.CANCELLED: ArtifactStatus.FAILED,
        JobStatus.CANCELLING: ArtifactStatus.RUNNING,
    }
    return mapping.get(job_status, ArtifactStatus.FAILED)

# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def process_artifact_v2(
    artifact_id: str,
    artifact_type: str,
    format: str,
    parameters: dict,
    job_id: str,
):
    """Execute the artifact rendering in a background thread."""
    manager = JobManager()
    manager.start_job(job_id)
    
    # Use LocalStorageProvider (can be overridden by Pro)
    storage = LocalStorageProvider()
    
    ext_map = {
        "pdf": ".pdf",
        "markdown": ".md",
        "docx": ".docx",
        "xlsx": ".xlsx",
        "odt": ".odt",
        "ods": ".ods",
        "odp": ".odp",
    }
    ext = ext_map.get(format, f".{format}")
    
    output_path = Path(storage.base_path) / f"{artifact_id}{ext}"
    
    try:
        renderer = get_renderer(format)
        
        cancel_check = lambda: manager.is_cancel_requested(job_id)
        
        success = renderer.render(
            artifact_type=artifact_type,
            parameters=parameters,
            output_path=output_path,
            cancel_check=cancel_check,
        )
        
        if success:
            manager.complete_job(job_id)
            logger.info(f"Artifact job {job_id} completed: {output_path}")
        else:
            manager.fail_job(job_id, "Rendering failed or was cancelled")
            
    except Exception as e:
        manager.fail_job(job_id, str(e))
        logger.error(f"Artifact job {job_id} failed: {e}")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/capabilities", response_model=ArtifactCapabilitiesResponseV2)
async def get_artifact_capabilities() -> ArtifactCapabilitiesResponseV2:
    """Returns supported artifact types and formats."""
    return ArtifactCapabilitiesResponseV2(
        supported_formats=SUPPORTED_FORMATS,
        supported_types=SUPPORTED_TYPES,
        templates=[]
    )

@router.post(
    "",
    response_model=ArtifactResponseV2,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_artifact_v2(
    payload: ArtifactRequestV2,
    background_tasks: BackgroundTasks,
) -> ArtifactResponseV2:
    """Initiates an artifact generation job."""
    if payload.format not in SUPPORTED_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {payload.format}")
    
    manager = JobManager()
    artifact_id = uuid.uuid4().hex
    job = manager.create_job(source=f"artifact:{artifact_id}", job_type="v2_artifact")
    
    background_tasks.add_task(
        process_artifact_v2,
        artifact_id,
        payload.artifact_type,
        payload.format,
        payload.parameters or {},
        job.id,
    )
    
    return ArtifactResponseV2(
        status="accepted",
        message="Artifact generation job accepted",
        job_id=job.id,
        artifact_id=artifact_id,
    )


@router.get(
    "/{artifact_id}",
    response_model=JobResponseV2,
    responses={
        404: {"description": "Artifact not found"},
    }
)
async def get_artifact_v2(artifact_id: str):
    """Returns metadata and status for the artifact job."""
    manager = JobManager()
    
    target_job = None
    for job in manager.list_jobs():
        if job.source == f"artifact:{artifact_id}":
            target_job = job
            break
            
    if not target_job:
        raise HTTPException(status_code=404, detail="Artifact job not found")
        
    return JobResponseV2(**target_job.to_dict())


@router.get(
    "/{artifact_id}/content",
    responses={
        200: {"description": "Artifact download"},
        202: {"description": "Job still in progress"},
        404: {"description": "Artifact not found"},
    }
)
async def download_artifact_content_v2(artifact_id: str):
    """Downloads the rendered content if ready."""
    manager = JobManager()
    
    target_job = None
    for job in manager.list_jobs():
        if job.source == f"artifact:{artifact_id}":
            target_job = job
            break
            
    if not target_job:
        raise HTTPException(status_code=404, detail="Artifact job not found")
        
    if target_job.status == JobStatus.COMPLETED:
        storage = LocalStorageProvider()
        file_path = storage.get_path(artifact_id)
        if not file_path:
             raise HTTPException(status_code=404, detail="Artifact file not found")
             
        return FileResponse(
            path=str(file_path),
            filename=file_path.name,
            media_type="application/octet-stream"
        )
    
    if target_job.status == JobStatus.FAILED:
        raise HTTPException(status_code=410, detail=f"Artifact generation failed: {target_job.error}")
        
    raise HTTPException(status_code=202, detail="Artifact generation still in progress")


@router.delete("/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_artifact_v2(artifact_id: str):
    """Idempotently deletes an artifact and cancels its job if running."""
    manager = JobManager()
    storage = LocalStorageProvider()
    
    target_job = None
    for job in manager.list_jobs():
        if job.source == f"artifact:{artifact_id}":
            target_job = job
            break
            
    if target_job:
        manager.request_cancel(target_job.id)
        
    storage.delete(artifact_id)
    return
