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
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.  See the License for the specific language governing
# permissions and limitations under the License.

"""v2 artifact generation API — durable lifecycle (Spec 026).

PostgreSQL (the Spec 025 durable Core jobs subsystem) is the ONLY
authoritative logical job store for artifact generation; the legacy
in-memory JobManager is NOT written by this workflow.  The rendered
file remains owned by the artifact storage provider; the durable row
carries the bounded result reference.
"""

import uuid
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from fastapi.responses import FileResponse

from retriva.ingestion_api import durable_jobs
from retriva.ingestion_api.artifact_store import (
    media_type_for,
)
from retriva.ingestion_api.schemas_v2 import (
    ArtifactRequestV2,
    ArtifactResponseV2,
    ArtifactCapabilitiesResponseV2,
    JobResponseV2,
)
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

#: Bounded artifact progress phases (Spec 026 §12).
ARTIFACT_PHASES = ["fetching_data", "rendering", "finalizing"]

_ARTIFACT_JOB_TYPE = "v2_artifact"


def _artifact_job_or_none(artifact_id: str, tenant_id: str):
    """Durable-first lookup by the server-generated subject id."""
    from retriva.ingestion_api.durable_jobs import jobs_service
    return jobs_service().repo.get_job_by_subject(
        tenant_id=tenant_id, job_type=_ARTIFACT_JOB_TYPE,
        subject_id=artifact_id)


def _project_job(job) -> JobResponseV2:
    """Compatibility projection into the existing ``JobResponseV2``
    shape (status-string family preserved; additive progress fields
    from the durable record)."""
    status_map = {
        "succeeded": "completed",
        "running": "running",
        "failed": "failed",
        "cancelled": "cancelled",
        "cancelling": "cancelling",
        "pending": "pending",
        "dispatching": "pending",
        "queued": "pending",
        "dispatch_unknown": "pending",
        "retry_wait": "pending",
        "manual_review": "pending",
    }
    input_meta = job.input_metadata or {}
    stages_completed: List[str] = []
    current_stage = job.progress_stage
    if current_stage in ARTIFACT_PHASES:
        stages_completed = ARTIFACT_PHASES[
            :ARTIFACT_PHASES.index(current_stage)]
    return JobResponseV2(
        job_id=job.id,
        status=status_map.get(job.status.value, "failed"),
        source=f"artifact:{input_meta.get('artifact_id', '')}",
        job_type=job.job_type,
        current_stage=current_stage,
        stages_completed=stages_completed,
        stage_detail=job.progress_message,
        progress=job.progress,
        created_at=job.created_at.isoformat() if job.created_at else "",
        updated_at=job.updated_at.isoformat() if job.updated_at else "",
        error=job.last_error_summary,
    )


def _emit_missing_file_anomaly_once(tenant_id: str, job_id: str,
                                    artifact_id: str) -> None:
    """Bounded anomaly evidence for a succeeded artifact whose file
    is missing (Spec 026 §15): emitted at most once per job."""
    from retriva.jobs.domain import EventActor
    from retriva.ingestion_api.durable_jobs import jobs_service
    repo = jobs_service().repo
    try:
        for event in repo.events_for(tenant_id=tenant_id, job_id=job_id):
            detail = event.detail or {}
            if (event.event_type.value == "anomaly"
                    and detail.get("reason") == "artifact_file_missing"):
                return
        repo.record_anomaly(
            tenant_id=tenant_id, job_id=job_id,
            detail={"reason": "artifact_file_missing",
                    "artifact_id": artifact_id})
    except Exception as exc:  # noqa: BLE001 - evidence is best-effort
        logger.warning(
            "missing-artifact anomaly evidence failed (best-effort): "
            "job=%s exception=%s", job_id, exc.__class__.__name__)


def _resolve_content_path(job) -> Optional[Path]:
    """Resolve the finalized artifact through the configured storage
    provider, path-safely (server-generated reference; traversal
    rejected).  Returns None when the artifact is missing.  The
    provider class is resolved at CALL TIME through its storage
    module (Pro may substitute the provider; never a module-import
    binding)."""
    from retriva.infrastructure.storage import LocalStorageProvider
    result = job.result_metadata or {}
    storage = LocalStorageProvider()
    base = Path(storage.base_path).resolve()
    storage_ref = result.get("storage_ref")
    candidates: List[Path] = []
    if isinstance(storage_ref, str) and storage_ref:
        candidate = (base / storage_ref).resolve()
        if base != candidate and base not in candidate.parents:
            logger.warning(
                "artifact storage reference escapes the artifact "
                "root (rejected): job=%s", job.id)
            return None
        candidates.append(candidate)
    # Compatibility fallback: provider glob by artifact id (the
    # historical lookup; the durable reference is authoritative).
    artifact_id = (job.input_metadata or {}).get("artifact_id")
    if isinstance(artifact_id, str) and artifact_id:
        globbed = storage.get_path(artifact_id)
        if globbed is not None:
            candidates.append(globbed.resolve())
    for candidate in candidates:
        if base != candidate and base not in candidate.parents:
            continue
        if candidate.is_file():
            return candidate
    return None


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
    request: Request,
    background_tasks: BackgroundTasks,
) -> ArtifactResponseV2:
    """Initiates a DURABLE artifact generation job (Spec 026).

    Each accepted submission creates a NEW artifact and a NEW durable
    job (no client idempotency key; a canonical input fingerprint is
    persisted for diagnostics).  The collection context is resolved
    server-side and never taken from request input."""
    if payload.format not in SUPPORTED_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {payload.format}")

    tenant_id = durable_jobs.resolve_request_tenant(request)
    artifact_id = uuid.uuid4().hex
    # Server-resolved collection context (the configured active
    # collection; NEVER an unauthenticated client choice).
    from retriva.indexing.qdrant_store import get_collection_name
    collection_context = get_collection_name()

    submission = durable_jobs.submit_artifact_job(
        tenant_id=tenant_id,
        artifact_id=artifact_id,
        artifact_type=payload.artifact_type,
        format=payload.format,
        parameters=payload.parameters or {},
        user_metadata=payload.user_metadata,
        collection_context=collection_context,
        background_tasks=background_tasks,
    )

    return ArtifactResponseV2(
        status="accepted",
        message="Artifact generation job accepted",
        job_id=submission.job.id,
        artifact_id=artifact_id,
    )


@router.get(
    "/{artifact_id}",
    response_model=JobResponseV2,
    responses={
        404: {"description": "Artifact not found"},
    }
)
async def get_artifact_v2(artifact_id: str, request: Request):
    """Returns metadata and status for the durable artifact job."""
    tenant_id = durable_jobs.resolve_request_tenant(request)
    job = _artifact_job_or_none(artifact_id, tenant_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Artifact job not found")
    return _project_job(job)


@router.get(
    "/{artifact_id}/content",
    responses={
        200: {"description": "Artifact download"},
        202: {"description": "Job still in progress"},
        404: {"description": "Artifact not found"},
        410: {"description": "Artifact generation failed"},
    }
)
async def download_artifact_content_v2(artifact_id: str, request: Request):
    """Downloads the rendered content if ready (durable lifecycle)."""
    tenant_id = durable_jobs.resolve_request_tenant(request)
    job = _artifact_job_or_none(artifact_id, tenant_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Artifact job not found")

    status_value = job.status.value
    if status_value == "succeeded":
        file_path = _resolve_content_path(job)
        if file_path is None:
            _emit_missing_file_anomaly_once(
                tenant_id, job.id, artifact_id)
            raise HTTPException(status_code=404,
                                detail="Artifact file not found")
        return FileResponse(
            path=str(file_path),
            filename=file_path.name,
            media_type=media_type_for(file_path.name),
        )

    if status_value in ("failed", "cancelled"):
        raise HTTPException(
            status_code=410,
            detail=(f"Artifact generation did not complete "
                    f"({job.last_error_code or 'not_completed'})"))

    # Non-terminal (pending family, running, cancelling): compatible
    # in-progress response.
    raise HTTPException(status_code=202,
                        detail="Artifact generation still in progress")


@router.delete("/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_artifact_v2(artifact_id: str, request: Request):
    """Idempotently deletes an artifact and cancels its durable job.

    Cancellation is durable intent: the Spec 025 state machine owns
    the transition (pending → cancelled without execution; running →
    cancelling with cooperative acknowledgement; finalization race →
    per Spec 026 §14 — a proven-complete artifact may remain
    succeeded and is removed from the artifact store by THIS
    deletion).  Celery revoke is a non-guaranteed aid only.  A
    running render's partial output is NOT deleted from under the
    renderer: the handler quarantines it on the durable cancel."""
    tenant_id = durable_jobs.resolve_request_tenant(request)
    from retriva.ingestion_api.durable_jobs import jobs_service
    service = jobs_service()
    job = _artifact_job_or_none(artifact_id, tenant_id)

    if job is None:
        # Unknown to the durable store: legacy-orphan file cleanup;
        # idempotent 204 (compatibility).
        from retriva.infrastructure.storage import LocalStorageProvider
        LocalStorageProvider().delete(artifact_id)
        return

    try:
        service.cancel(tenant_id=tenant_id, job_id=job.id,
                       revoke_requested=True)
    except Exception as exc:  # noqa: BLE001 - the cancel intent is
        # durable; races are classified by the state machine
        logger.warning(
            "artifact cancel attempt failed (state machine retains "
            "the intent): job=%s exception=%s",
            job.id, exc.__class__.__name__)

    status_value = job.status.value
    if status_value in ("running", "cancelling"):
        # The handler owns partial-output quarantine on the durable
        # cancel; do not race the renderer.
        return

    file_path = _resolve_content_path(job)
    if file_path is not None:
        if status_value == "succeeded":
            # Deletion of a finalized artifact is a legitimate
            # lifecycle operation (not an execution anomaly): record
            # it once, bounded, so the succeeded job's missing file
            # is explainable.
            try:
                service.repo.record_anomaly(
                    tenant_id=tenant_id, job_id=job.id,
                    detail={"reason": "artifact_file_deleted_by_request",
                            "artifact_id": artifact_id})
            except Exception:  # noqa: BLE001 - best-effort evidence
                pass
        try:
            file_path.unlink()
        except OSError:
            pass
    return
