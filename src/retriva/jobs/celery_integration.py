# Copyright (C) 2026 Retriva.
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

"""Celery integration for the durable job lifecycle (Spec 025 §3.5–
§3.6; ADR-030 Decision 5/7).

The Celery task base wraps handler execution with the durable claim /
progress / terminal protocol; the existing ``ingestion_api`` tasks
delegate to it.  Celery retry counters are diagnostic only — the
retry path is durable (``retry_wait`` + reschedule executor).
"""

from __future__ import annotations

from typing import Any, Callable, Tuple

from retriva.jobs.config import JobsSettings
from retriva.jobs.execution import (
    execute_durable_job,
    worker_identity,
)
from retriva.jobs.repository import PostgresJobsRepository
from retriva.logger import get_logger

_log = get_logger(__name__)


def run_celery_durable_task(
        task: Any, *,
        repo: PostgresJobsRepository,
        handler: Callable[..., Any],
        job_id: str, attempt_id: str, tenant_id: str,
        dispatch_token: str, celery_task_id: str,
        settings: Optional[JobsSettings] = None,
        rescheduler: Optional[Callable[[str, str], None]] = None,
        cancelled_exceptions: Tuple[type, ...] = (),
) -> str:
    """Run one durable attempt inside a bound Celery task.

    ``redelivered`` comes from the transport (``acks_late=True`` +
    ``task_reject_on_worker_lost=True`` redelivery evidence) and is
    one input to the claim classification; the durable attempt row
    remains authoritative."""
    settings = settings or JobsSettings()
    return execute_durable_job(
        repo=repo, handler=handler,
        job_id=job_id, attempt_id=attempt_id, tenant_id=tenant_id,
        dispatch_token=dispatch_token,
        celery_task_id=celery_task_id,
        worker_id=worker_identity("celery"),
        redelivered=bool(getattr(task.request, "redelivered", False)),
        stale_threshold_seconds=(
            settings.reconcile_stale_threshold_seconds),
        cancelled_exceptions=cancelled_exceptions,
        rescheduler=rescheduler)
