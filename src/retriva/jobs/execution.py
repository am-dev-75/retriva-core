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

"""The shared worker execution protocol (Spec 025 §3.5–§3.6, §3.10).

ONE protocol for BOTH execution transports (Celery tasks and the
local BackgroundTasks fallback): the durable, atomic claim first;
throttled durable cancellation checks; progress through the durable
service; a SINGLE writer for terminal transitions; durable retry
scheduling (Celery ``self.retry`` is never the retry path); OOM
redelivery classified by the claim rules.  No execution may happen
outside the durable attempt model.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from retriva.jobs.domain import (
    ERROR_CODE_UNCLASSIFIED,
    AttemptRecord,
    AttemptStatus,
    JobRecord,
    SanitizedError,
    error_code_for_exception,
    sanitize_error_summary,
)
from retriva.jobs.repository import (
    ClaimOutcome,
    PostgresJobsRepository,
)
from retriva.logger import get_logger

_log = get_logger(__name__)


class CancelCheckResult:
    """The pipeline-facing cancellation check returns a plain bool;
    the handler adapter decides what to raise."""


@dataclass
class HandlerOutcome:
    """What the handler adapter observed (the protocol owns the one
    terminal transition)."""

    kind: str  # success | failure | cancelled
    error: Optional[SanitizedError] = None
    retryable: bool = False
    result_metadata: Optional[Dict[str, Any]] = None
    detail: Optional[Dict[str, Any]] = None


Handler = Callable[..., HandlerOutcome]
# handler(job=..., attempt=..., cancel_check=...) -> HandlerOutcome

#: rescheduler(job_id, tenant_id, delay_seconds) — the durable retry
#: scheduler (worker post-commit timer; reconciliation R6 is the
#: fallback).
Rescheduler = Callable[[str, str, float], None]


def make_cancel_check(repo: PostgresJobsRepository, tenant_id: str,
                      job_id: str, *,
                      throttle_seconds: float = 2.0) -> Callable[[], bool]:
    """Durable-flag cancellation check, throttled (bounded
    repository traffic); returns True when cancellation was durably
    requested.  The caller's pipeline raises its own cancellation
    error on True (existing cooperative checkpoints unchanged)."""
    state = {"last": -1e9, "value": False}

    def cancel_check() -> bool:
        now = time.monotonic()
        if now - state["last"] >= throttle_seconds:
            state["last"] = now
            state["value"] = repo.is_cancel_requested(
                tenant_id=tenant_id, job_id=job_id)
        return state["value"]

    return cancel_check


def worker_identity(prefix: str = "celery") -> str:
    """Bounded worker identity for claim stamps (no secrets)."""
    import os
    import socket

    return f"{prefix}:{socket.gethostname()}:{os.getpid()}"


def execute_durable_job(
        *, repo: PostgresJobsRepository,
        handler: Handler,
        job_id: str, attempt_id: str, tenant_id: str,
        dispatch_token: str, celery_task_id: str,
        worker_id: Optional[str] = None,
        redelivered: bool = False,
        stale_threshold_seconds: int = 1800,
        cancel_throttle_seconds: float = 2.0,
        cancelled_exceptions: Tuple[type, ...] = (),
        rescheduler: Optional[Callable[[str, str], None]] = None,
        log: Any = _log,
) -> str:
    """Claim and execute one durable attempt.

    Returns the claim outcome; execution happens only for
    ``granted``/``granted_takeover``.  All non-granted outcomes are
    idempotent no-ops (deterministically classified)."""
    worker_id = worker_id or worker_identity()
    decision = repo.claim_for_delivery(
        tenant_id=tenant_id, job_id=job_id, attempt_id=attempt_id,
        dispatch_token=dispatch_token, celery_task_id=celery_task_id,
        worker_id=worker_id, redelivered=redelivered,
        stale_threshold_seconds=stale_threshold_seconds)
    if decision.outcome not in (ClaimOutcome.GRANTED,
                                ClaimOutcome.GRANTED_TAKEOVER):
        log.info(
            "delivery not executed: outcome=%s job=%s detail=%s",
            decision.outcome.value, job_id,
            {k: v for k, v in (decision.detail or {}).items()
             if k != "reason" or True})
        return decision.outcome.value

    cancel_check = make_cancel_check(
        repo, tenant_id, job_id,
        throttle_seconds=cancel_throttle_seconds)
    try:
        outcome = handler(
            job=decision.job, attempt=decision.attempt,
            cancel_check=cancel_check, worker_id=worker_id)
    except cancelled_exceptions as exc:
        # Cooperative cancellation surfaced as an exception: durable
        # acknowledge (T14).
        repo.acknowledge_cooperative_cancel(
            tenant_id=tenant_id, job_id=job_id, attempt_id=attempt_id,
            detail={"via": "exception",
                    "exception_class": exc.__class__.__name__})
        log.info("job cancelled cooperatively: job=%s", job_id)
        return "cancelled"
    except Exception as exc:  # noqa: BLE001 - classified below
        error = SanitizedError(
            code=error_code_for_exception(exc),
            summary=sanitize_error_summary(exc.__class__.__name__))
        failure = repo.complete_failure(
            tenant_id=tenant_id, job_id=job_id, attempt_id=attempt_id,
            error=error, retryable=True,
            detail={"exception_class": exc.__class__.__name__})
        if failure is not None and failure.retry_scheduled:
            log.info(
                "durable retry scheduled: job=%s attempt_no=%s "
                "scheduled_at=%s", job_id, failure.attempt.attempt_no,
                failure.scheduled_at.isoformat() if failure.scheduled_at
                else None)
            if rescheduler is not None:
                rescheduler(job_id, tenant_id, _delay_for(failure))
        elif failure is None:
            log.info(
                "failure callback was a duplicate (attempt not "
                "running): job=%s attempt=%s", job_id, attempt_id)
        return "failed"
    if outcome.kind == "cancelled":
        repo.acknowledge_cooperative_cancel(
            tenant_id=tenant_id, job_id=job_id, attempt_id=attempt_id,
            detail=(outcome.detail or {}))
        return "cancelled"
    if outcome.kind == "failure":
        failure = repo.complete_failure(
            tenant_id=tenant_id, job_id=job_id, attempt_id=attempt_id,
            error=outcome.error
            or SanitizedError(code=ERROR_CODE_UNCLASSIFIED,
                              summary="execution_failed"),
            retryable=outcome.retryable,
            detail=outcome.detail)
        if failure is not None and failure.retry_scheduled:
            log.info(
                "durable retry scheduled: job=%s attempt_no=%s",
                job_id, failure.attempt.attempt_no)
            if rescheduler is not None:
                rescheduler(job_id, tenant_id, _delay_for(failure))
        return "failed"
    # success (explicit or the handler forgot to mark; the claim made
    # the attempt running and the pipeline completed normally).
    repo.complete_success(
        tenant_id=tenant_id, job_id=job_id, attempt_id=attempt_id,
        result_metadata=outcome.result_metadata,
        detail=outcome.detail)
    return "succeeded"


def _delay_for(failure) -> float:
    """Seconds until the scheduled retry (bounded, never negative)."""
    scheduled = failure.scheduled_at
    if scheduled is None:
        return 0.0
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=timezone.utc)
    return max((scheduled - datetime.now(timezone.utc)).total_seconds(),
               0.0)


class RetryRescheduler:
    """Durable retry scheduling without a scheduler process: a daemon
    timer per scheduled retry (worker post-commit); reconciliation
    sweep R6 is the fallback for timers lost to crashes/restarts."""

    def __init__(self, reschedule_fn: Callable[[str, str], None],
                 max_delay_seconds: float = 600.0) -> None:
        self._reschedule_fn = reschedule_fn
        self._max_delay = max_delay_seconds

    def schedule(self, job_id: str, tenant_id: str,
                 delay_seconds: float) -> None:
        delay = min(max(delay_seconds, 0.0), self._max_delay)
        timer = threading.Timer(
            delay, self._reschedule_fn, args=(job_id, tenant_id))
        timer.daemon = True
        timer.name = f"jobs-retry-{job_id[:8]}"
        timer.start()
