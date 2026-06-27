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
Thread-safe in-memory job manager for tracking asynchronous ingestion jobs.

Jobs follow a cooperative cancellation model: the ``cancel_check`` callback
is polled at batch boundaries in the embedding and upsert loops. There is no
thread interruption or force-kill.
"""

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from retriva.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Job status enum
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


TERMINAL_STATES = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}


# ---------------------------------------------------------------------------
# Cancellation exception — raised at checkpoints, caught by workers
# ---------------------------------------------------------------------------

class CancellationError(Exception):
    """Raised when a cancellation checkpoint detects a pending cancel request."""


# ---------------------------------------------------------------------------
# Job dataclass
# ---------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    status: JobStatus
    source: str
    job_type: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error: Optional[str] = None
    cancel_requested: bool = False
    current_stage: Optional[str] = None
    stages_completed: List[str] = field(default_factory=list)
    stage_detail: Optional[str] = None
    progress: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "job_id": self.id,
            "status": self.status.value,
            "source": self.source,
            "job_type": self.job_type,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "error": self.error,
            "current_stage": self.current_stage,
            "stages_completed": list(self.stages_completed),
            "stage_detail": self.stage_detail,
            "progress": self.progress,
        }


# ---------------------------------------------------------------------------
# JobManager singleton
# ---------------------------------------------------------------------------

class JobManager:
    """Thread-safe singleton for managing ingestion job lifecycle."""

    _instance: Optional["JobManager"] = None
    _init_lock = threading.Lock()

    def __new__(cls) -> "JobManager":
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._jobs: Dict[str, Job] = {}
                    instance._lock = threading.Lock()
                    cls._instance = instance
        return cls._instance

    # -- Factory -----------------------------------------------------------

    def create_job(self, source: str, job_type: str) -> Job:
        job = Job(
            id=uuid.uuid4().hex,
            status=JobStatus.PENDING,
            source=source,
            job_type=job_type,
        )
        with self._lock:
            self._jobs[job.id] = job
        logger.debug(f"Job {job.id} created ({job_type}: {source})")
        return job

    # -- State transitions -------------------------------------------------

    def start_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.status == JobStatus.PENDING:
                job.status = JobStatus.RUNNING
                job.updated_at = datetime.now(timezone.utc)
                logger.debug(f"Job {job_id} → running")

    def complete_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.status == JobStatus.RUNNING:
                job.status = JobStatus.COMPLETED
                job.updated_at = datetime.now(timezone.utc)
                logger.info(f"Job {job_id} → completed")

    def fail_job(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.status in (JobStatus.RUNNING, JobStatus.CANCELLING):
                job.status = JobStatus.FAILED
                job.error = error
                job.updated_at = datetime.now(timezone.utc)
                logger.error(f"Job {job_id} → failed: {error}")

    def request_cancel(self, job_id: str) -> bool:
        """
        Request cancellation.

        Returns True if the request was accepted (job moved to CANCELLING).
        Returns False if the job is in a terminal state (cannot cancel).
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status in TERMINAL_STATES:
                return False
            if job.status in (JobStatus.CANCELLING,):
                return True  # already cancelling, idempotent
            job.status = JobStatus.CANCELLING
            job.cancel_requested = True
            job.updated_at = datetime.now(timezone.utc)
            logger.info(f"Job {job_id} → cancelling")
            return True

    def mark_cancelled(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.status == JobStatus.CANCELLING:
                job.status = JobStatus.CANCELLED
                job.updated_at = datetime.now(timezone.utc)
                logger.info(f"Job {job_id} → cancelled")

    # -- Stage tracking (v2) -----------------------------------------------

    def advance_stage(self, job_id: str, stage: str) -> None:
        """Record a pipeline stage transition (v2 jobs only).

        Moves the previous ``current_stage`` into ``stages_completed``
        and sets the new stage. Resets ``stage_detail`` and ``progress``
        since they belong to the previous stage.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.status == JobStatus.RUNNING:
                if job.current_stage and job.current_stage not in job.stages_completed:
                    job.stages_completed.append(job.current_stage)
                job.current_stage = stage
                job.stage_detail = None
                job.progress = None
                job.updated_at = datetime.now(timezone.utc)
                logger.debug(f"Job {job_id} stage → {stage}")

    def set_stage_detail(self, job_id: str, detail: str, progress: Optional[int] = None) -> None:
        """Update fine-grained progress within the current stage.

        Args:
            job_id: The job identifier.
            detail: Human-readable description of the sub-activity
                    (e.g. ``"parsing chunk 2/3 (pages 501-1000)"``).
            progress: Optional 0-100 percentage for the current stage.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.status == JobStatus.RUNNING:
                job.stage_detail = detail
                if progress is not None:
                    job.progress = max(0, min(100, progress))
                job.updated_at = datetime.now(timezone.utc)

    # -- Queries -----------------------------------------------------------

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.cancel_requested if job else False

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> List[Job]:
        with self._lock:
            return list(self._jobs.values())

    # -- Testing support ---------------------------------------------------

    @classmethod
    def _reset(cls) -> None:
        """Reset the singleton — for testing only."""
        with cls._init_lock:
            if cls._instance is not None:
                cls._instance._jobs.clear()
                cls._instance = None