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

"""Application service for the durable job lifecycle (Spec 025 §3.4,
§3.8, §3.12; ADR-030).

The service is the ONLY path that mutates the durable lifecycle:
submit (idempotent), publication-state dispatch (prepare → publish →
classify), durable retry scheduling, cancellation, operator retry
(CLI-authorized), and reconciliation entry points.  Tenant context
flows in explicitly from callers (server-side resolution); the
repository fails closed without it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from retriva.jobs.config import JobsSettings
from retriva.jobs.dispatch import (
    CeleryPublisher,
    DeliveryEnvelope,
    LocalPublishResult,
    LocalPublisher,
    PublishResult,
    PublicationOutcome,
    new_dispatch_identity,
)
from retriva.jobs.domain import (
    AttemptRecord,
    EventActor,
    JobRecord,
    JobStatus,
    SanitizedError,
)
from retriva.jobs.errors import (
    JobNotFoundError,
    JobsError,
    OperatorRetryRefusedError,
    TenantContextMissingError,
)
from retriva.jobs.registry import JobTypeRegistry, JobTypeSpec
from retriva.jobs.repository import (
    CancelDecision,
    ClaimDecision,
    ClaimOutcome,
    CancellationOutcome,
    FailureDecision,
    PostgresJobsRepository,
)
from retriva.logger import get_logger

_log = get_logger(__name__)

#: Canonical payload contract version for v2 ingestion jobs.
PAYLOAD_VERSION_V2 = "v2-1"

#: Input-metadata keys that are durable bookkeeping, never task
#: message arguments.
_INTERNAL_METADATA_KEYS = frozenset({"input_fingerprint"})


def task_payload_from_metadata(job: JobRecord) -> Dict[str, Any]:
    """Task message payload derived from the durable
    ``input_metadata`` (used by republish paths: R2 and the stale
    attempt republishes — the registered task's signature bounds the
    accepted keys; Core-builtin submission helpers write exactly the
    task's accepted arguments)."""
    return {
        key: value
        for key, value in (job.input_metadata or {}).items()
        if key not in _INTERNAL_METADATA_KEYS
    }


class LocalDispatch:
    """A dispatch whose publication returned the local runner: the
    route registers the runner with ``background_tasks.add_task``."""

    def __init__(self, job: JobRecord, runner: Callable[[], Any]) -> None:
        self.job = job
        self.runner = runner


class JobsService:
    """Durable job lifecycle service (PostgreSQL authoritative)."""

    def __init__(
            self, repo: Optional[PostgresJobsRepository] = None,
            settings: Optional[JobsSettings] = None,
            registry: Optional[JobTypeRegistry] = None,
            publisher: Optional[Any] = None,
            retry_scheduler: Optional[Any] = None,
    ) -> None:
        self.settings = settings or JobsSettings()
        self.repo = repo
        self.registry = registry
        self.publisher = publisher
        self.rescheduler = retry_scheduler
        self._reschedule_lock = threading.Lock()

    # -- submission -----------------------------------------------------------

    def submit(
            self, *, tenant_id: str, job_type: str,
            execution_transport: str,
            input_metadata: Optional[Dict[str, Any]] = None,
            idempotency_key: Optional[str] = None,
            requested_by: Optional[str] = None,
            subject_type: Optional[str] = None,
            subject_id: Optional[str] = None,
            payload_version: str = PAYLOAD_VERSION_V2,
            job_id: Optional[str] = None,
            max_attempts: Optional[int] = None,
            queue: Optional[str] = None,
    ) -> JobRecord:
        """T1: durable submission (idempotent; NO broker call inside
        the transaction)."""
        spec = self.require_spec(job_type)
        import uuid

        resolved_id = job_id or uuid.uuid4().hex
        attempts = max_attempts if max_attempts is not None else \
            spec.max_attempts_default
        job, _created = self.repo.submit_job(
            tenant_id=tenant_id, job_id=resolved_id, job_type=job_type,
            payload_version=payload_version,
            execution_transport=execution_transport,
            input_metadata=input_metadata,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
            subject_type=subject_type, subject_id=subject_id,
            max_attempts=max(attempts, 1),
            queue=queue or spec.queue,
            actor=EventActor.API)
        return job

    def require_spec(self, job_type: str) -> JobTypeSpec:
        if self.registry is None:
            raise JobsError("job-type registry is not configured")
        return self.registry.require(job_type)

    # -- publication-state dispatch -------------------------------------------

    def dispatch_job(self, job: JobRecord, *,
                     payload: Optional[Dict[str, Any]] = None,
                     tenant_id: Optional[str] = None,
    ) -> Optional[LocalDispatch]:
        """Two-phase dispatch (Spec 025 §3.4): prepare the attempt
        (preallocated Celery task id, dispatch token, timestamps,
        publication state persisted BEFORE publishing) → publish →
        classify.  Returns a :class:`LocalDispatch` for the local
        transport (the route registers the runner); None when the
        prepare guard did not match (e.g. a raced cancellation)."""
        tenant = tenant_id or job.tenant_id
        spec = self.require_spec(job.job_type)
        attempt_id, dispatch_token, celery_task_id = \
            new_dispatch_identity()
        attempt = self.repo.prepare_dispatch(
            tenant_id=tenant, job_id=job.id,
            dispatch_token=dispatch_token,
            celery_task_id=celery_task_id, attempt_id=attempt_id)
        if attempt is None:
            _log.info(
                "dispatch prepare skipped (guard did not match): "
                "job=%s", job.id)
            return None
        envelope = DeliveryEnvelope(
            job_id=job.id, attempt_id=attempt.id, tenant_id=tenant,
            dispatch_token=dispatch_token,
            celery_task_id=celery_task_id,
            task_name=spec.task_name, job_type=job.job_type,
            payload=dict(payload or {}))
        return self._publish_prepared(
            job, attempt, envelope, tenant, queue=job.queue or spec.queue)

    def _publish_prepared(
            self, job: JobRecord, attempt: AttemptRecord,
            envelope: DeliveryEnvelope, tenant_id: str,
            queue: Optional[str], *,
            republish: bool = False,
    ) -> Optional[LocalDispatch]:
        if self.publisher is None:
            raise JobsError("job publisher is not configured")
        if isinstance(self.publisher, LocalPublisher):
            result = self.publisher.publish(envelope, queue)
            # Local transport: publication recorded as confirmed
            # (in-process schedule accepted); the runner claims
            # through the same durable protocol.
            return LocalDispatch(job, result.runner)
        self.repo.record_publication_inflight(
            tenant_id=tenant_id, job_id=job.id, attempt_id=attempt.id,
            republish=republish)
        result = self.publisher.publish(envelope, queue)
        self._apply_publication_outcome(
            job, attempt, result, tenant_id)
        return None

    def _apply_publication_outcome(
            self, job: JobRecord, attempt: AttemptRecord,
            result: PublishResult, tenant_id: str) -> None:
        if result.outcome == PublicationOutcome.CONFIRMED:
            self.repo.record_publication_confirmed(
                tenant_id=tenant_id, job_id=job.id,
                attempt_id=attempt.id)
            return
        if result.outcome == PublicationOutcome.DEFINITELY_REJECTED:
            self.repo.record_publication_rejected(
                tenant_id=tenant_id, job_id=job.id,
                attempt_id=attempt.id,
                error=result.error
                or SanitizedError(code="transport_rejected",
                                  summary="publish rejected"))
            _log.warning(
                "publication definitely rejected before broker "
                "acceptance: job=%s attempt=%s", job.id, attempt.id)
            return
        # AMBIGUOUS (fail-safe default): never reverted blindly.
        self.repo.record_publication_ambiguous(
            tenant_id=tenant_id, job_id=job.id, attempt_id=attempt.id,
            error=result.error
            or SanitizedError(code="publication_ambiguous",
                              summary="publish outcome unknown"))
        _log.warning(
            "publication outcome ambiguous: job=%s attempt=%s "
            "(reconciliation will resolve by evidence)",
            job.id, attempt.id)

    def republish_dispatch(self, job: JobRecord,
                           attempt: AttemptRecord) -> bool:
        """T8 (R2): re-publish the SAME dispatch generation — the same
        attempt identity, preallocated Celery task id, and dispatch
        token; NOT a new execution attempt.  Publication-try bound is
        enforced by the caller (reconcile).  The message payload is
        derived from the durable ``input_metadata`` (the durable store
        is the source of truth for task inputs; a republished message
        never relies on caller-held state)."""
        tenant = job.tenant_id
        spec = self.require_spec(job.job_type)
        envelope = DeliveryEnvelope(
            job_id=job.id, attempt_id=attempt.id, tenant_id=tenant,
            dispatch_token=attempt.dispatch_token,
            celery_task_id=attempt.celery_task_id
            or attempt.id,
            task_name=spec.task_name, job_type=job.job_type,
            payload=task_payload_from_metadata(job))
        self._publish_prepared(
            job, attempt, envelope, tenant,
            queue=job.queue or spec.queue, republish=True)
        return True

    def reschedule_due(self, job_id: str, tenant_id: str) -> bool:
        """T20: durable retry rescheduling (executor for
        ``retry_wait``); publication follows like a fresh dispatch.
        For the local transport the returned runner executes inline
        (the timer thread / CLI process runs the same protocol)."""
        with self._reschedule_lock:
            attempt_id, dispatch_token, celery_task_id = \
                new_dispatch_identity()
            attempt = self.repo.prepare_retry_dispatch(
                tenant_id=tenant_id, job_id=job_id,
                dispatch_token=dispatch_token,
                celery_task_id=celery_task_id, attempt_id=attempt_id)
            if attempt is None:
                return False
            job = self.repo.get_job(tenant_id=tenant_id, job_id=job_id)
            dispatch = self._publish_prepared(
                job, attempt, DeliveryEnvelope(
                    job_id=job_id, attempt_id=attempt.id,
                    tenant_id=tenant_id,
                    dispatch_token=dispatch_token,
                    celery_task_id=celery_task_id,
                    task_name=self.require_spec(
                        job.job_type).task_name,
                    job_type=job.job_type, payload={}), tenant_id,
                queue=job.queue
                or self.require_spec(job.job_type).queue)
            if dispatch is not None and dispatch.runner is not None:
                dispatch.runner()
            return True

    # -- queries ----------------------------------------------------------------

    def get_job(self, *, tenant_id: str, job_id: str) -> JobRecord:
        return self.repo.get_job(tenant_id=tenant_id, job_id=job_id)

    def find_job_any_tenant(self, job_id: str) -> Optional[JobRecord]:
        """Legacy-compatibility id resolution probe (durable-first
        precedence; Spec 025 §3.11): used ONLY to classify whether an
        id belongs to the durable store; never exposes cross-tenant
        data (the caller then re-reads with its own tenant context)."""
        try:
            return self.repo.get_job_unscoped(
                job_id=job_id, privileged=True)
        except Exception:  # noqa: BLE001 - probe is best-effort
            return None

    def list_jobs(self, *, tenant_id: str, status: Optional[str] = None,
                  job_type: Optional[str] = None, limit: int = 50,
                  offset: int = 0) -> List[JobRecord]:
        return self.repo.list_jobs(
            tenant_id=tenant_id, status=status, job_type=job_type,
            limit=limit, offset=offset)

    def cancel(self, *, tenant_id: str, job_id: str,
               revoke_requested: bool = False) -> CancelDecision:
        decision = self.repo.request_cancel(
            tenant_id=tenant_id, job_id=job_id,
            actor=EventActor.API,
            revoke_requested=revoke_requested)
        # Best-effort transport aid ONLY (never a guarantee of
        # interruption; Spec 025 §3.3): revoke the attempt's
        # preallocated Celery task id for not-yet-started deliveries.
        if (decision.outcome in (CancellationOutcome.REQUESTED,
                                 CancellationOutcome.CANCELLED)
                and job_transport_is_celery(decision.job)):
            self._best_effort_revoke(decision.job, tenant_id)
        return decision

    def _best_effort_revoke(self, job: JobRecord,
                            tenant_id: str) -> None:
        try:
            attempt = self.repo.get_latest_attempt(
                tenant_id=tenant_id, job_id=job.id)
            task_id = attempt.celery_task_id if attempt else None
            if not task_id:
                return
            from retriva.ingestion_api.celery_app import get_celery_app
            app = get_celery_app()
            if app is not None:
                app.control.revoke(task_id, terminate=False)
        except Exception as exc:  # noqa: BLE001 - best-effort aid
            _log.info(
                "celery revoke not performed (best-effort): job=%s "
                "exception=%s", job.id, exc.__class__.__name__)

    def operator_retry(self, *, tenant_id: str, job_id: str,
                       reason: str = "",
                       override_max_attempts: bool = False,
                       payload: Optional[Dict[str, Any]] = None,
    ) -> JobRecord:
        """T21 (operator CLI/administrative surface ONLY — never the
        unauthenticated public API)."""
        existing = self.repo.get_job_unscoped(
            job_id=job_id, privileged=True)
        if existing is None:
            raise JobNotFoundError(
                "job not found (operator context)")
        spec = self.require_spec(existing.job_type)
        attempt_id, dispatch_token, celery_task_id = \
            new_dispatch_identity()
        attempt = self.repo.prepare_operator_retry(
            tenant_id=tenant_id, job_id=job_id,
            dispatch_token=dispatch_token,
            celery_task_id=celery_task_id, attempt_id=attempt_id,
            reason=reason, override_max_attempts=override_max_attempts)
        if attempt is None:
            raise OperatorRetryRefusedError(
                "operator retry refused: the job is not in a "
                "retryable terminal state, or attempts are exhausted "
                "without a durably recorded override")
        job = self.repo.get_job(tenant_id=tenant_id, job_id=job_id)
        dispatch = self._publish_prepared(
            job, attempt, DeliveryEnvelope(
                job_id=job_id, attempt_id=attempt.id,
                tenant_id=tenant_id,
                dispatch_token=dispatch_token,
                celery_task_id=celery_task_id,
                task_name=spec.task_name, job_type=job.job_type,
                payload=dict(payload or {})),
            tenant_id, queue=job.queue or spec.queue)
        if dispatch is not None and dispatch.runner is not None:
            # Local transport: the CLI process runs the same durable
            # protocol inline (documented operator behavior).
            dispatch.runner()
        return self.repo.get_job(tenant_id=tenant_id, job_id=job_id)

    # -- progress / worker callbacks ------------------------------------------------

    def record_progress(self, *, tenant_id: str, job_id: str,
                        progress: Optional[int], stage: Optional[str],
                        message: Optional[str]) -> bool:
        return self.repo.record_progress(
            tenant_id=tenant_id, job_id=job_id, progress=progress,
            stage=stage, message=message)

    def is_cancel_requested(self, *, tenant_id: str,
                            job_id: str) -> bool:
        return self.repo.is_cancel_requested(
            tenant_id=tenant_id, job_id=job_id)


def retry_backoff_for(attempt_no: int) -> float:
    """Existing exponential parity, capped (durable scheduling)."""
    return float(min(2 ** max(attempt_no - 1, 0), 600))


def job_transport_is_celery(job: JobRecord) -> bool:
    from retriva.jobs.domain import ExecutionTransport
    return job.execution_transport == ExecutionTransport.CELERY
