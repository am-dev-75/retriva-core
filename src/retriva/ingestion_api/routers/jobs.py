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

from fastapi import APIRouter, HTTPException, status
from retriva.ingestion_api.schemas import JobResponse
from retriva.ingestion_api.job_manager import JobManager, TERMINAL_STATES, JobStatus
from retriva.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


@router.get("", response_model=list[JobResponse])
async def list_jobs():
    """List all tracked ingestion jobs."""
    manager = JobManager()
    jobs = manager.list_jobs()
    return [JobResponse(**j.to_dict()) for j in jobs]


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: str):
    """Get the status of a specific job.

    When Celery is enabled, checks Redis first (the worker process
    updates job state there) then falls back to the in-memory manager.

    The worker writes *partial* state dicts to Redis during stage changes
    (e.g. ``{"job_id", "status", "error"}``), so we merge the Redis
    real-time status with the full metadata from the in-memory
    ``JobManager`` before validating against ``JobResponse``.
    """
    manager = JobManager()
    in_memory_job = manager.get_job(job_id)

    # When Celery is enabled, the worker updates state in Redis.
    from retriva.ingestion_api.celery_app import celery_enabled
    if celery_enabled():
        from retriva.ingestion_api.tasks import get_task_status
        task_state = get_task_status(job_id)
        if task_state is not None:
            # Redis state may be partial (only job_id/status/error).
            # Enrich it with full metadata from the in-memory manager so
            # that JobResponse validation doesn't fail on missing fields.
            if in_memory_job is not None:
                full = in_memory_job.to_dict()
                full.update(task_state)  # Redis status takes precedence
                return JobResponse(**full)
            # No in-memory copy: fill in defaults for required fields so
            # the caller gets a useful response instead of a 500.
            task_state.setdefault("source", "unknown")
            task_state.setdefault("job_type", "unknown")
            task_state.setdefault("created_at", task_state.get("updated_at", ""))
            task_state.setdefault("updated_at", task_state.get("created_at", ""))
            return JobResponse(**task_state)

    if in_memory_job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return JobResponse(**in_memory_job.to_dict())


@router.post("/{job_id}/cancel", response_model=JobResponse)
async def cancel_job(job_id: str):
    """
    Request cooperative cancellation of a job.

    - Running/pending → 202 Accepted (cancelling)
    - Already cancelling/cancelled → 200 OK (idempotent)
    - Completed/failed → 409 Conflict
    - Unknown → 404
    """
    manager = JobManager()
    job = manager.get_job(job_id)

    if job is None:
        # When Celery is enabled, the job may not be in the in-memory manager
        # (e.g. after an API restart). Try Redis-backed cancellation.
        from retriva.ingestion_api.celery_app import celery_enabled
        if celery_enabled():
            from retriva.ingestion_api.tasks import request_task_cancellation
            request_task_cancellation(job_id)
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={"job_id": job_id, "status": "cancelling"},
    )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    if job.status in (JobStatus.CANCELLING, JobStatus.CANCELLED):
        # Idempotent — already cancelling or cancelled
        return JobResponse(**job.to_dict())

    if job.status in TERMINAL_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot cancel job in '{job.status.value}' state",
        )

    manager.request_cancel(job_id)

    # Also revoke the Celery task if Celery is enabled
    from retriva.ingestion_api.celery_app import celery_enabled
    if celery_enabled():
        from retriva.ingestion_api.tasks import request_task_cancellation
        request_task_cancellation(job_id)

    # Re-fetch after state change
    job = manager.get_job(job_id)
    logger.info(f"Cancellation requested for job {job_id}")

    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=JobResponse(**job.to_dict()).model_dump(),
    )
