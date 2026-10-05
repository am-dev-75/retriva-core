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

"""Local (BackgroundTasks) executor for the durable job lifecycle
(Spec 025 §3.10; ADR-030 Decision 11).

The existing development fallback is preserved ONLY as the same
PostgreSQL-authoritative lifecycle: it never bypasses durable
persistence, never creates a second state machine, and never restores
the in-memory ``JobManager`` as an authoritative store.  The only
difference from Celery execution is ``execution_transport='local'``
and the in-process worker identity; a process death leaves the
attempt to reconciliation with local-transport evidence; duplicate
local execution is prevented by the durable attempt claim.
"""

from __future__ import annotations

from typing import Any, Callable, Tuple

from retriva.jobs.config import JobsSettings
from retriva.jobs.dispatch import DeliveryEnvelope
from retriva.jobs.execution import (
    execute_durable_job,
    worker_identity,
)
from retriva.jobs.repository import PostgresJobsRepository
from retriva.logger import get_logger

_log = get_logger(__name__)


class LocalExecutor:
    """Runs the durable protocol in-process after the response
    (FastAPI BackgroundTasks).  Durable state survives the API
    process because it lives in PostgreSQL; the in-process execution
    itself does NOT survive a crash — reconciliation classifies the
    attempt (nothing pretends process-local execution survives)."""

    def __init__(self, repo: PostgresJobsRepository,
                 settings: Optional[JobsSettings] = None,
                 rescheduler: Optional[Callable[[str, str], None]] = None,
                 ) -> None:
        self._repo = repo
        self._settings = settings or JobsSettings()
        self._rescheduler = rescheduler

    def runner_for(self, envelope: DeliveryEnvelope,
                   handler: Callable[..., Any],
                   cancelled_exceptions: Tuple[type, ...] = (),
                   ) -> Callable[[], Any]:
        """Build the BackgroundTasks callable (the route registers it
        with ``background_tasks.add_task``)."""
        def _run_locally() -> None:
            try:
                execute_durable_job(
                    repo=self._repo, handler=handler,
                    job_id=envelope.job_id,
                    attempt_id=envelope.attempt_id,
                    tenant_id=envelope.tenant_id,
                    dispatch_token=envelope.dispatch_token,
                    celery_task_id=envelope.celery_task_id,
                    worker_id=worker_identity("local"),
                    redelivered=False,
                    stale_threshold_seconds=(
                        self._settings
                        .reconcile_stale_threshold_seconds),
                    cancelled_exceptions=cancelled_exceptions,
                    rescheduler=self._rescheduler)
            except Exception as exc:  # noqa: BLE001 - never crash the response path
                _log.error(
                    "local durable execution crashed: job=%s "
                    "exception=%s (reconciliation will classify)",
                    envelope.job_id, exc.__class__.__name__)

        _run_locally.__name__ = "run_durable_job_local"
        return _run_locally
