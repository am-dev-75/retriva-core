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

"""Integration glue: the v2 ingestion workflow on the durable job
lifecycle (Spec 025 §4; ADR-030).

PostgreSQL is the ONLY authoritative logical job store for the
integrated flow: no legacy JobManager dual writes, no Redis
job-state/cancel keys.  One execution protocol serves both transports
(Celery and the local BackgroundTasks fallback).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional, Tuple

from retriva.jobs.config import JobsSettings
from retriva.jobs.dispatch import (
    CeleryPublisher,
    DeliveryEnvelope,
    LocalPublishResult,
    LocalPublisher,
)
from retriva.jobs.domain import (
    ERROR_CODE_UNCLASSIFIED,
    EventActor,
    JobRecord,
    SanitizedError,
    error_code_for_exception,
    sanitize_error_summary,
)
from retriva.jobs.errors import IdempotencyConflictError
from retriva.jobs.execution import HandlerOutcome
from retriva.jobs.registry import job_type_registry
from retriva.jobs.repository import PostgresJobsRepository
from retriva.jobs.service import PAYLOAD_VERSION_V2, JobsService
from retriva.jobs.tenant import TenantResolver, tenant_resolver
from retriva.logger import get_logger

_log = get_logger(__name__)

_JOB_TYPES = ("v2_document", "v2_mediawiki", "v2_upload")

_service_lock = threading.Lock()
_service: Optional[JobsService] = None


def _default_payload_version() -> str:
    return PAYLOAD_VERSION_V2


def build_service(settings: Optional[JobsSettings] = None,
                  repo: Optional[PostgresJobsRepository] = None,
                  publisher: Optional[Any] = None,
                  registry: Optional[Any] = None,
                  ) -> JobsService:
    """Build a fully-wired service (repo + transport-appropriate
    publisher + registry + durable retry scheduler)."""
    from retriva.infrastructure.postgres.config import (
        get_platform_settings,
    )
    from retriva.jobs.execution import RetryRescheduler
    from retriva.jobs.local import LocalExecutor

    jobs_settings = settings or JobsSettings()
    platform_settings = get_platform_settings()
    repository = repo or PostgresJobsRepository(platform_settings)
    job_registry = registry or job_type_registry()

    service = JobsService(
        repo=repository, settings=jobs_settings,
        registry=job_registry, publisher=publisher)
    scheduler = RetryRescheduler(service.reschedule_due)
    service.rescheduler = scheduler
    if publisher is None:
        from retriva.ingestion_api.celery_app import celery_enabled

        if celery_enabled():
            def _app_getter():
                from retriva.ingestion_api.celery_app import (
                    get_celery_app,
                )
                return get_celery_app()
            service.publisher = CeleryPublisher(_app_getter, job_registry)
        else:
            from retriva.ingestion_api.job_manager import (
                CancellationError,
            )

            executor = LocalExecutor(
                repository, jobs_settings,
                rescheduler=scheduler.schedule)

            def _local_runner_factory(envelope: DeliveryEnvelope):
                return executor.runner_for(
                    envelope,
                    handler_for(service, envelope.job_type,
                                envelope.tenant_id, envelope.job_id,
                                dict(envelope.payload)),
                    cancelled_exceptions=(CancellationError,))

            service.publisher = LocalPublisher(_local_runner_factory)
    return service


def jobs_service() -> JobsService:
    """Process-wide service (API process and worker process share the
    same durable lifecycle through PostgreSQL)."""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = build_service()
    return _service


def reset_jobs_service() -> None:
    """Test hook."""
    global _service
    with _service_lock:
        _service = None


# ---------------------------------------------------------------------------
# Idempotency keys (default: derived from content identity)
# ---------------------------------------------------------------------------

def _hash_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def fingerprint_for(identity: Dict[str, Any]) -> str:
    """Bounded input-identity fingerprint for idempotency-conflict
    detection (reserved ``input_fingerprint`` metadata key)."""
    from retriva.jobs.repository import INPUT_FINGERPRINT_KEY
    return hashlib.sha256(json.dumps(
        identity, sort_keys=True, default=str).encode(
        "utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Progress recorder: the durable projection of the pipeline's
# JobManager-surface progress calls (JobManager is NOT authoritative
# for the integrated flow; the recorder writes PostgreSQL only)
# ---------------------------------------------------------------------------

class DurableProgressRecorder:
    """Implements the progress surface ``process_document_v2`` and
    ``process_mediawiki_export`` use, writing DURABLE progress; it
    never records terminal states (the shared execution protocol is
    the single writer — it observes the flags here after the pipeline
    returns)."""

    def __init__(self, service: JobsService, tenant_id: str,
                 job_id: str, attempt_id: str) -> None:
        self._service = service
        self._tenant_id = tenant_id
        self._job_id = job_id
        self._attempt_id = attempt_id
        self._throttle = service.settings.progress_throttle_seconds
        self._last_write = 0.0
        self._last_stage: Optional[str] = None
        self._progress = 0
        self._stage = None
        self._message = None
        self._terminal: Optional[str] = None
        self._error: Optional[SanitizedError] = None

    # -- pipeline surface -------------------------------------------------

    def start_job(self, job_id: str) -> None:
        # The durable claim already recorded running state.
        return None

    def advance_stage(self, job_id: str, stage: str) -> None:
        self._stage = stage
        self._progress = 0
        self._write_progress(throttled=False)

    def set_stage_detail(self, job_id: str, detail: str,
                         progress: int) -> None:
        self._message = detail
        self._progress = int(progress) if progress is not None else None
        self._write_progress(
            throttled=(self._stage == self._last_stage_written()))

    def _last_stage_written(self) -> Optional[str]:
        return self._last_stage

    def _write_progress(self, *, throttled: bool) -> None:
        now = time.monotonic()
        stage_changed = self._stage != self._last_stage
        if throttled and not stage_changed and (
                now - self._last_write) < self._throttle:
            return
        self._last_write = now
        self._last_stage = self._stage
        try:
            self._service.record_progress(
                tenant_id=self._tenant_id, job_id=self._job_id,
                progress=self._progress, stage=self._stage,
                message=self._message)
        except Exception as exc:  # noqa: BLE001 - progress is best-effort
            _log.warning(
                "durable progress write failed (best-effort): job=%s "
                "exception=%s", self._job_id, exc.__class__.__name__)

    def complete_job(self, job_id: str) -> None:
        self._terminal = "success"

    def mark_cancelled(self, job_id: str) -> None:
        self._terminal = "cancelled"

    def fail_job(self, job_id: str, error_summary: object) -> None:
        self._terminal = "failure"
        self._error = SanitizedError(
            code=ERROR_CODE_UNCLASSIFIED,
            summary=sanitize_error_summary(error_summary))

    def is_cancel_requested(self, job_id: str) -> bool:
        try:
            return self._service.is_cancel_requested(
                tenant_id=self._tenant_id, job_id=self._job_id)
        except Exception:  # noqa: BLE001 - check is best-effort
            return False

    def get_job(self, job_id: str):
        """Legacy-surface probe for the pipeline cleanup branches
        (``process_document_v2``'s finally block): a lightweight
        snapshot of this recorder's observed state.  The durable store
        stays authoritative; nothing is written here — same semantics
        as the in-memory ``JobManager`` projection it replaces."""
        from types import SimpleNamespace
        from retriva.ingestion_api.job_manager import JobStatus
        if self._terminal == "success":
            status = JobStatus.COMPLETED
        elif self._terminal == "cancelled":
            status = JobStatus.CANCELLED
        elif self._terminal == "failure":
            status = JobStatus.FAILED
        else:
            status = JobStatus.PENDING
        return SimpleNamespace(status=status)

    # -- outcome for the execution protocol ---------------------------------

    def outcome(self) -> HandlerOutcome:
        if self._terminal == "cancelled":
            return HandlerOutcome(kind="cancelled")
        if self._terminal == "failure":
            return HandlerOutcome(
                kind="failure",
                error=self._error
                or SanitizedError(code=ERROR_CODE_UNCLASSIFIED,
                                  summary="execution_failed"),
                retryable=True)
        return HandlerOutcome(kind="success")


# ---------------------------------------------------------------------------
# Handlers (job-type specific adapters over the existing pipelines)
# ---------------------------------------------------------------------------

def document_handler(service: JobsService, tenant_id: str, job_id: str,
                     payload: Dict[str, Any]):
    """Adapter running the v2 document pipeline on the durable
    lifecycle (pipeline untouched; progress projected durably)."""
    from retriva.ingestion_api.job_manager import CancellationError

    def _run(job: JobRecord, attempt, cancel_check: Callable[[], bool],
             worker_id: str) -> HandlerOutcome:
        from retriva.ingestion_api.routers.v2_documents import (
            process_document_v2,
        )
        recorder = DurableProgressRecorder(
            service, tenant_id, job_id, attempt.id)
        # Same normalization as the Celery task body: the collection
        # context is transport plumbing, not pipeline input.
        from retriva.indexing.qdrant_store import (
            DEFAULT_COLLECTION_NAME,
            _collection_name_ctx,
            set_collection_name,
        )
        local_payload = dict(payload)
        collection = (
            local_payload.pop("collection_name", None)
            or DEFAULT_COLLECTION_NAME)
        token = set_collection_name(collection)
        try:
            process_document_v2(
                job_id=job_id,
                recorder=recorder,
                _cancel_check=cancel_check,
                **local_payload)
        except CancellationError:
            recorder.mark_cancelled(job_id)
            return recorder.outcome()
        except Exception as exc:  # noqa: BLE001 - classified below
            return HandlerOutcome(
                kind="failure",
                error=SanitizedError(
                    code=error_code_for_exception(exc),
                    summary=sanitize_error_summary(
                        exc.__class__.__name__)),
                retryable=True,
                detail={"exception_class": exc.__class__.__name__})
        finally:
            _collection_name_ctx.reset(token)
        return recorder.outcome()

    return _run


def mediawiki_handler(service: JobsService, tenant_id: str, job_id: str,
                      payload: Dict[str, Any]):
    """Adapter running the MediaWiki export pipeline on the durable
    lifecycle."""
    from retriva.ingestion_api.job_manager import CancellationError

    def _run(job: JobRecord, attempt, cancel_check: Callable[[], bool],
             worker_id: str) -> HandlerOutcome:
        from retriva.ingestion.mediawiki_v2_parser import (
            process_mediawiki_export,
        )
        recorder = DurableProgressRecorder(
            service, tenant_id, job_id, attempt.id)
        try:
            process_mediawiki_export(
                payload["staged_dir"],
                payload.get("user_metadata"),
                payload.get("kb_id", "default"),
                cancel_check,
                job_id,
                recorder=recorder,
            )
        except CancellationError:
            recorder.mark_cancelled(job_id)
            return recorder.outcome()
        except Exception as exc:  # noqa: BLE001 - classified below
            return HandlerOutcome(
                kind="failure",
                error=SanitizedError(
                    code=error_code_for_exception(exc),
                    summary=sanitize_error_summary(
                        exc.__class__.__name__)),
                retryable=True,
                detail={"exception_class": exc.__class__.__name__})
        return recorder.outcome()

    return _run


_HANDLERS: Dict[str, Callable] = {
    "v2_document": document_handler,
    "v2_upload": document_handler,
    "v2_mediawiki": mediawiki_handler,
}


def handler_for(service: JobsService, job_type: str, tenant_id: str,
                job_id: str, payload: Dict[str, Any]):
    factory = _HANDLERS.get(job_type)
    if factory is None:
        raise RuntimeError(
            f"no durable handler adapter for job type {job_type!r}")
    return factory(service, tenant_id, job_id, payload)


# ---------------------------------------------------------------------------
# Worker-side execution entry points (Celery task bodies)
# ---------------------------------------------------------------------------

def run_document_job(task, *, job_id: str, attempt_id: str,
                     tenant_id: str, dispatch_token: str,
                     celery_task_id: str, payload: Dict[str, Any]) -> str:
    """Celery body for the v2 document ingestion task (durable
    protocol; collection context + extension loading preserved)."""
    from retriva.indexing.qdrant_store import (
        DEFAULT_COLLECTION_NAME,
        _collection_name_ctx,
        set_collection_name,
    )
    from retriva.registry import CapabilityRegistry

    CapabilityRegistry().load_extensions()
    service = jobs_service()
    col = payload.pop("collection_name", None) or DEFAULT_COLLECTION_NAME
    token = set_collection_name(col)
    try:
        return _execute_via_protocol(
            task, service, "v2_document", payload, job_id, attempt_id,
            tenant_id, dispatch_token, celery_task_id)
    finally:
        _collection_name_ctx.reset(token)


def run_mediawiki_job(task, *, job_id: str, attempt_id: str,
                      tenant_id: str, dispatch_token: str,
                      celery_task_id: str, payload: Dict[str, Any]) -> str:
    from retriva.indexing.qdrant_store import (
        DEFAULT_COLLECTION_NAME,
        _collection_name_ctx,
        set_collection_name,
    )
    from retriva.registry import CapabilityRegistry

    CapabilityRegistry().load_extensions()
    service = jobs_service()
    col = payload.pop("collection_name", None) or DEFAULT_COLLECTION_NAME
    token = set_collection_name(col)
    try:
        return _execute_via_protocol(
            task, service, "v2_mediawiki", payload, job_id, attempt_id,
            tenant_id, dispatch_token, celery_task_id)
    finally:
        _collection_name_ctx.reset(token)


def _execute_via_protocol(task, service: JobsService, job_type: str,
                          payload: Dict[str, Any], job_id: str,
                          attempt_id: str, tenant_id: str,
                          dispatch_token: str,
                          celery_task_id: str) -> str:
    from retriva.ingestion_api.job_manager import CancellationError
    from retriva.jobs.celery_integration import run_celery_durable_task

    payload = dict(payload)
    payload.pop("job_id", None)
    payload.pop("attempt_id", None)
    payload.pop("tenant_id", None)
    payload.pop("dispatch_token", None)
    payload.pop("celery_task_id", None)
    return run_celery_durable_task(
        task, repo=service.repo,
        handler=handler_for(service, job_type, tenant_id, job_id,
                            payload),
        job_id=job_id, attempt_id=attempt_id, tenant_id=tenant_id,
        dispatch_token=dispatch_token,
        celery_task_id=celery_task_id,
        settings=service.settings,
        rescheduler=(service.rescheduler.schedule
                     if service.rescheduler else None),
        cancelled_exceptions=(CancellationError,))


# ---------------------------------------------------------------------------
# Submission helpers for the routes (tenant resolution + idempotency +
# dispatch; the route registers the local runner on BackgroundTasks)
# ---------------------------------------------------------------------------

@dataclass
class SubmissionResult:
    job: JobRecord
    created: bool
    local_runner: Optional[Callable[[], Any]]


def resolve_request_tenant(request) -> str:
    """Server-side tenant resolution for a request (trust model in
    retriva.jobs.tenant; never logs the tenant value)."""
    resolver = tenant_resolver()
    header_value = request.headers.get(
        resolver._settings.tenant_header_name)
    client_host = request.client.host if request.client else None
    resolution = resolver.resolve(client_host, header_value)
    return resolution.tenant_id


def submit_document_job(*, tenant_id: str, source_uri: str,
                        content_type: Optional[str],
                        user_metadata: Optional[Dict[str, Any]],
                        parser_hint: Optional[str],
                        kb_id: str = "default",
                        collection_name: Optional[str] = None,
                        background_tasks=None,
) -> SubmissionResult:
    """Durable v2 document submission + dispatch (Spec 025 §3.4)."""
    service = jobs_service()
    payload = dict(
        source_uri=source_uri, content_type=content_type,
        user_metadata=user_metadata, parser_hint=parser_hint,
        kb_id=kb_id, collection_name=collection_name)
    idempotency_key = f"v2doc:{_hash_hex(f'{kb_id}|{source_uri}')}"
    input_metadata = dict(payload)
    input_metadata["input_fingerprint"] = fingerprint_for(payload)
    job = service.submit(
        tenant_id=tenant_id, job_type="v2_document",
        execution_transport=_transport(),
        input_metadata=input_metadata,
        idempotency_key=idempotency_key,
        payload_version=_default_payload_version())
    return _dispatch_submitted(service, job, payload, background_tasks)


def submit_mediawiki_job(*, tenant_id: str, staged_dir: str,
                         user_metadata: Optional[Dict[str, Any]],
                         kb_id: str = "default",
                         collection_name: Optional[str] = None,
                         background_tasks=None,
) -> SubmissionResult:
    service = jobs_service()
    payload = dict(
        staged_dir=staged_dir, user_metadata=user_metadata,
        kb_id=kb_id, collection_name=collection_name)
    idempotency_key = f"v2mw:{_hash_hex(f'{kb_id}|{staged_dir}')}"
    input_metadata = dict(payload)
    input_metadata["input_fingerprint"] = fingerprint_for(payload)
    job = service.submit(
        tenant_id=tenant_id, job_type="v2_mediawiki",
        execution_transport=_transport(),
        input_metadata=input_metadata,
        idempotency_key=idempotency_key,
        payload_version=_default_payload_version())
    return _dispatch_submitted(service, job, payload, background_tasks)


def submit_upload_job(*, tenant_id: str, source_path: str,
                      content_type: Optional[str],
                      user_metadata: Optional[Dict[str, Any]],
                      parser_hint: Optional[str],
                      temp_path: Optional[str], doc_id: Optional[str],
                      content_hash: Optional[str], kb_id: str = "default",
                      source_paths: Optional[list] = None,
                      content_size: Optional[int] = None,
                      ingestion_status: str = "completed",
                      created_at: Optional[str] = None,
                      collection_name: Optional[str] = None,
                      background_tasks=None,
) -> SubmissionResult:
    service = jobs_service()
    hex_digest = (content_hash or "").split(":", 1)[-1]
    payload = dict(
        source_uri=source_path, content_type=content_type,
        user_metadata=user_metadata, parser_hint=parser_hint,
        temp_path=temp_path, doc_id=doc_id, content_hash=content_hash,
        kb_id=kb_id, source_paths=source_paths,
        content_size=content_size,
        ingestion_status=ingestion_status, created_at=created_at,
        collection_name=collection_name)
    idempotency_key = f"v2up:{_hash_hex(f'{kb_id}|{hex_digest}')}"
    input_metadata = dict(payload)
    input_metadata["input_fingerprint"] = fingerprint_for(payload)
    job = service.submit(
        tenant_id=tenant_id, job_type="v2_upload",
        execution_transport=_transport(),
        input_metadata=input_metadata,
        idempotency_key=idempotency_key,
        subject_type="document", subject_id=doc_id,
        payload_version=_default_payload_version())
    return _dispatch_submitted(service, job, payload, background_tasks)


def _transport() -> str:
    from retriva.ingestion_api.celery_app import celery_enabled
    return "celery" if celery_enabled() else "local"


def _dispatch_submitted(service: JobsService, job: JobRecord,
                        payload: Dict[str, Any],
                        background_tasks) -> SubmissionResult:
    dispatch = service.dispatch_job(job, payload=payload,
                                    tenant_id=job.tenant_id)
    runner = dispatch.runner if dispatch is not None else None
    if runner is not None and background_tasks is not None:
        background_tasks.add_task(runner)
    return SubmissionResult(job=job, created=True, local_runner=runner)
