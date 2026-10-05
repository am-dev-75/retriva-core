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

"""
v2 job status endpoints — Spec 025 durable store (M4 compat surface).

The durable PostgreSQL store is authoritative for the integrated
ingestion workflow; the v1-style legacy in-memory/Redis state remains
available ONLY as a fallback projection for ids unknown to the durable
store (documented durable-first precedence; Spec 025 §3.11).  There is
NO manual-retry route on this unauthenticated public surface (operator
CLI only).

Tenant model (Spec 025 §3.12): the tenant is resolved server-side
(fixed configured tenant for the current unauthenticated development
deployment; a header is honored only behind a trusted gateway or a
clearly named loopback-constrained development override).  Ordinary
request input never selects an arbitrary tenant; the durable store is
tenant-scoped with pagination; the legacy fallback keeps its previous
(single-tenant development) behavior.
"""

from fastapi import APIRouter, HTTPException, Query, Request, status

from retriva.ingestion_api.durable_jobs import resolve_request_tenant
from retriva.ingestion_api.job_manager import CancellationError
from retriva.ingestion_api.schemas_v2 import JobResponseV2
from retriva.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v2/jobs", tags=["v2-jobs"])

_LEGACY_JOB_STATUSES = {
    "pending", "running", "completed", "failed",
    "cancelling", "cancelled",
}
_DURABLE_JOB_STATUSES = {
    "pending", "dispatching", "queued", "dispatch_unknown",
    "retry_wait", "running", "cancelling", "manual_review",
    "succeeded", "failed", "cancelled",
}


def _compat_projection(job) -> JobResponseV2:
    """Project a durable job row into the legacy ``JobResponseV2``
    shape with ADDITIVE-safe semantics: the shape is unchanged;
    ``stages_completed`` is reconstructed from the ordered stage list
    truncated at the current stage (deterministic; Spec 025 §3.7);
    ``error`` is the sanitized summary (never a raw exception)."""
    from retriva.ingestion_api.schemas_v2 import JobStage

    stage_names = [s.value for s in JobStage]
    stages_completed: list = []
    if job.progress_stage:
        if job.progress_stage in stage_names:
            idx = stage_names.index(job.progress_stage)
            stages_completed = stage_names[:idx]
        else:
            stages_completed = stage_names
    status_value = job.status.value
    if status_value == "succeeded":
        status_value = "completed"
    error = None
    if job.status == "failed":
        error = job.last_error_summary or job.last_error_code
    elif job.status == "cancelled":
        error = None
    created_at = (job.created_at.isoformat()
                  if job.created_at else "")
    updated_at = (job.updated_at.isoformat()
                  if job.updated_at else "")
    return JobResponseV2(
        job_id=job.id,
        status=status_value,
        source=job.subject_id
        or (job.input_metadata or {}).get("source_uri", "")
        or (job.input_metadata or {}).get("staged_dir", "")
        or "",
        job_type=job.job_type,
        current_stage=job.progress_stage,
        stages_completed=stages_completed,
        stage_detail=job.progress_message,
        progress=job.progress,
        created_at=created_at,
        updated_at=updated_at,
        error=error,
    )


def _legacy_fallback(job_id: str) -> JobResponseV2 | None:
    """Legacy projection for ids unknown to the durable store
    (durable-first precedence; Spec 025 §3.11)."""
    from retriva.ingestion_api.job_manager import JobManager

    job = JobManager().get_job(job_id)
    if job is not None:
        return JobResponseV2(**job.to_dict())
    from retriva.ingestion_api.celery_app import celery_enabled
    if celery_enabled():
        from retriva.ingestion_api.tasks import get_task_status
        task_state = get_task_status(job_id)
        if task_state is not None:
            return JobResponseV2(
                job_id=job_id,
                status=task_state.get("status", "pending"),
                source=task_state.get("source", ""),
                job_type=task_state.get("job_type", "v2_document"),
                created_at=task_state.get("created_at", ""),
                updated_at=task_state.get("updated_at", ""),
                error=task_state.get("error"),
                current_stage=task_state.get("current_stage"),
                stages_completed=task_state.get("stages_completed", []),
                stage_detail=task_state.get("stage_detail"),
                progress=task_state.get("progress"),
            )
    return None


@router.get("", response_model=list[JobResponseV2])
async def list_jobs_v2(
    request: Request,
    job_status: str | None = Query(
        None, alias="status",
        description="Filter by job status"),
    job_type: str | None = Query(
        None, alias="job_type", description="Filter by job type"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List jobs for the resolved tenant from the durable store
    (paginated, bounded)."""
    tenant_id = resolve_request_tenant(request)
    try:
        from retriva.ingestion_api.durable_jobs import jobs_service

        jobs = jobs_service().list_jobs(
            tenant_id=tenant_id,
            status=job_status if job_status in _DURABLE_JOB_STATUSES
            else None,
            job_type=job_type,
            limit=limit, offset=offset)
        return [_compat_projection(j) for j in jobs]
    except Exception as exc:  # noqa: BLE001 - degraded read
        logger.warning(
            "durable job list unavailable (exception=%s); returning "
            "empty list", exc.__class__.__name__)
        return []


@router.get("/{job_id}", response_model=JobResponseV2)
async def get_job_v2(job_id: str, request: Request):
    """Get the status of a specific job.

    Durable-first: ids known to the durable store resolve there
    (tenant-scoped); ids unknown to the durable store fall back to the
    legacy in-memory/Redis projection (deterministic precedence; a v2
    integrated-flow id can only ever resolve durably)."""
    tenant_id = resolve_request_tenant(request)
    from retriva.ingestion_api.durable_jobs import jobs_service
    from retriva.jobs.errors import JobNotFoundError

    try:
        job = jobs_service().get_job(tenant_id=tenant_id, job_id=job_id)
        return _compat_projection(job)
    except JobNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 - degraded read
        logger.warning(
            "durable job read unavailable (exception=%s); falling "
            "back to legacy projection", exc.__class__.__name__)

    fallback = _legacy_fallback(job_id)
    if fallback is not None:
        return fallback
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Job not found",
    )


@router.post("/{job_id}/cancel", response_model=JobResponseV2)
async def cancel_job_v2(job_id: str, request: Request):
    """Request durable cancellation of a job (Spec 025 §3.3).

    Cancellation is durable intent: the worker's cooperative
    checkpoints honor it; Celery revoke is a best-effort transport aid
    only (it NEVER guarantees interruption of running work).
    Duplicates are idempotent.  NO retry route exists on this public
    surface."""
    tenant_id = resolve_request_tenant(request)
    from retriva.ingestion_api.durable_jobs import jobs_service
    from retriva.jobs.errors import JobNotFoundError

    try:
        decision = jobs_service().cancel(
            tenant_id=tenant_id, job_id=job_id)
    except JobNotFoundError:
        fallback = _legacy_fallback(job_id)
        if fallback is not None:
            return fallback
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )
    if decision.outcome.value == "already_terminal":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job already terminal "
                   f"({decision.job.status.value})",
        )
    if decision.outcome.value == "not_cancellable":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job is in manual_review; only an operator can "
                   "resolve it",
        )
    return _compat_projection(decision.job)
