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
v2 job status endpoints.

Returns extended job status with pipeline stage information
(``current_stage``, ``stages_completed``).
"""

from fastapi import APIRouter, HTTPException, status

from retriva.ingestion_api.job_manager import JobManager
from retriva.ingestion_api.schemas_v2 import JobResponseV2
from retriva.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v2/jobs", tags=["v2-jobs"])


@router.get("", response_model=list[JobResponseV2])
async def list_jobs_v2():
    """List all v2 ingestion jobs with stage information.

    When Celery is enabled, enriches each job with its Redis-backed state
    (which is updated by the worker process) so the list reflects real-time
    progress rather than the stale in-memory snapshot from job creation.
    """
    manager = JobManager()
    jobs = manager.list_jobs()
    # Filter to v2 jobs only
    v2_jobs = [j for j in jobs if j.job_type.startswith("v2_")]

    # When Celery is enabled, the worker process updates job state in Redis.
    # The API process's in-memory JobManager is stale (it created the job
    # but never receives updates from the worker).  Merge Redis state.
    from retriva.ingestion_api.celery_app import celery_enabled
    if celery_enabled():
        from retriva.ingestion_api.tasks import get_task_status
        result = []
        for j in v2_jobs:
            task_state = get_task_status(j.id)
            if task_state is not None:
                result.append(JobResponseV2(**task_state))
            else:
                result.append(JobResponseV2(**j.to_dict()))
        return result

    return [JobResponseV2(**j.to_dict()) for j in v2_jobs]


@router.get("/{job_id}", response_model=JobResponseV2)
async def get_job_v2(job_id: str):
    """Get the status of a specific job with pipeline stage information.

    When Celery/Redis is enabled, falls back to the Redis-backed task status
    if the job is not found in the in-memory JobManager (e.g. after an API
    restart).
    """
    manager = JobManager()

    # When Celery is enabled, the worker process updates job state in Redis.
    # The API process's in-memory JobManager is stale (it created the job
    # but never receives updates from the worker).  Check Redis first.
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

    # Fall back to in-memory (BackgroundTasks mode or pre-Celery)
    job = manager.get_job(job_id)
    if job is not None:
        return JobResponseV2(**job.to_dict())

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Job not found",
    )
