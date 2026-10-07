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
import os
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
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
            from retriva.ingestion_api.execution import (
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


#: Versioned canonical upload input-identity schema (Spec 030 / ADR-035).
UPLOAD_INPUT_SCHEMA = "v2upload-input/1"

#: Payload fields that are request/host-local and MUST NOT affect the
#: durable upload input identity (Spec 030 §2.1).
_UPLOAD_VOLATILE_FIELDS = ("temp_path", "created_at")


def _canonical_scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool) or isinstance(value, int) \
            or isinstance(value, float):
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    return None


def _canonical_value(value: Any) -> Any:
    """Recursively canonicalize an accepted JSON-compatible value.

    Strings are NFC-normalized; lists/tuples become deterministically
    ordered, de-duplicated lists of canonical values; dicts get sorted,
    NFC-normalized keys.  Values that are not JSON-compatible scalars,
    lists, or dicts are dropped (they cannot be persisted detectably)."""
    if value is None or isinstance(value, bool) or isinstance(value, int) \
            or isinstance(value, float):
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, (list, tuple)):
        canonical_items = [_canonical_value(item) for item in value]
        seen = []
        for item in canonical_items:
            if item is None:
                continue
            marker = json.dumps(item, sort_keys=True, default=str)
            if marker not in seen:
                seen.append(marker)
        ordered = sorted(seen)
        return [json.loads(marker) for marker in ordered]
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key in sorted(value, key=lambda k: str(k)):
            canon = _canonical_value(value[key])
            if canon is None and value[key] is not None:
                continue
            out[unicodedata.normalize("NFC", str(key))] = canon
        return out
    return None


def _canonical_metadata(value: Any) -> Optional[Dict[str, Any]]:
    """Canonicalize accepted stable user metadata: JSON-compatible
    values are recursively canonicalized (never silently dropped when
    semantically meaningful); list-valued fields such as ``kb_ids``
    become deterministically sorted, de-duplicated sets."""
    if not isinstance(value, dict):
        return None
    canon = _canonical_value(value)
    return canon or None


def _canonical_path(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return unicodedata.normalize("NFC", value.replace("\\", "/"))


def _canonical_mime(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    parts = [p.strip() for p in value.split(";")]
    return unicodedata.normalize("NFC", parts[0].lower()) or None


def canonical_upload_identity(payload: Dict[str, Any], *,
                              tenant_id: str) -> Dict[str, Any]:
    """Canonical, versioned semantic input identity for an upload
    submission (Spec 030 / ADR-035).  Includes only stable fields that
    materially affect the durable operation/result; excludes all
    request-volatile/transport-local fields (``temp_path``,
    ``created_at``, derived ids such as ``doc_id``/``content_size``, and
    pre-submission dedup preconditions such as ``force``)."""
    source_paths = payload.get("source_paths")
    canon_paths = None
    if isinstance(source_paths, (list, tuple)):
        normed = sorted(p for p in
                        (_canonical_path(x) for x in source_paths)
                        if p)
        canon_paths = normed or None
    return {
        "schema": UPLOAD_INPUT_SCHEMA,
        "tenant_id": str(tenant_id),
        "operation": "v2_upload",
        "kb_id": _canonical_scalar(payload.get("kb_id")),
        "source_identity": {
            "source_path": _canonical_path(payload.get("source_uri")),
            "source_paths": canon_paths,
        },
        "content_identity": {
            "content_hash": _canonical_scalar(payload.get("content_hash")),
        },
        "content_type": _canonical_mime(payload.get("content_type")),
        "parser_hint": _canonical_scalar(payload.get("parser_hint")),
        "user_metadata": _canonical_metadata(payload.get("user_metadata")),
        "processing": {
            "payload_version": _canonical_scalar(
                payload.get("payload_version")
                or _default_payload_version()),
            "collection_name": _canonical_scalar(
                payload.get("collection_name")),
            "ingestion_status": _canonical_scalar(
                payload.get("ingestion_status")),
        },
    }


def upload_input_fingerprint(payload: Dict[str, Any], *,
                             tenant_id: str) -> str:
    """Deterministic SHA-256 hex of the canonical upload identity."""
    identity = canonical_upload_identity(payload, tenant_id=tenant_id)
    return hashlib.sha256(json.dumps(
        identity, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True).encode("utf-8")).hexdigest()


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
        from retriva.ingestion_api.execution import JobStatus
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
    from retriva.ingestion_api.execution import CancellationError

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
        knowledge = None
        try:
            from retriva.knowledge.pipeline import knowledge_pipeline
            _job_type = getattr(job, "job_type", "v2_document")
            knowledge = knowledge_pipeline().context_for_job(
                tenant_id, job_id, _job_type)
        except Exception:
            knowledge = None
        try:
            process_document_v2(
                job_id=job_id,
                recorder=recorder,
                _cancel_check=cancel_check,
                knowledge_context=knowledge,
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
    from retriva.ingestion_api.execution import CancellationError

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
                tenant_id=tenant_id,
                collection_name=payload.get("collection_name"),
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


def artifact_handler(service: JobsService, tenant_id: str, job_id: str,
                     payload: Dict[str, Any]):
    """Adapter running the v2 artifact generation on the durable
    lifecycle (Spec 026 / ADR-031).  ONE handler for both the Celery
    worker and the local BackgroundTasks fallback.

    Collection context: validated against the DURABLE submission
    record (never re-inferred from mutable process-global state);
    missing, malformed, or no-longer-permitted context fails safe
    (non-retryable).  Handler/render failures are non-retryable:
    the input is deterministic, so a new attempt would fail again or
    repeat provider cost (basic_report LLM call)."""
    from retriva.ingestion_api.execution import CancellationError

    # Renderer registration is an import side effect; the WORKER
    # process never imports the artifact router, so the execution
    # path registers the renderers explicitly (Spec 026 §9; idempotent
    # module imports).
    import retriva.rendering.markdown_renderer       # noqa: F401
    import retriva.rendering.pdf_renderer            # noqa: F401
    import retriva.rendering.docx_renderer           # noqa: F401
    import retriva.rendering.xlsx_renderer           # noqa: F401
    import retriva.rendering.opendocument_renderer   # noqa: F401

    def _run(job: JobRecord, attempt, cancel_check: Callable[[], bool],
             worker_id: str) -> HandlerOutcome:
        from retriva.indexing.qdrant_store import (
            DEFAULT_COLLECTION_NAME,
            _collection_name_ctx,
            set_collection_name,
        )

        from retriva.rendering import services as rendering_services
        recorder = DurableProgressRecorder(
            service, tenant_id, job_id, attempt.id)
        # Payload-contract gate (Spec 026 §6): unknown persisted
        # versions never reach execution (fail safe; no arbitrary
        # behavior from persisted strings).
        if job.payload_version != PAYLOAD_VERSION_V2:
            return HandlerOutcome(
                kind="failure",
                error=SanitizedError(
                    code="payload_version_unsupported",
                    summary="artifact payload contract version is "
                            "not supported"),
                retryable=False,
                detail={"payload_version": str(job.payload_version)})
        local_payload = dict(payload)
        collection_context = local_payload.pop("collection_context", None)
        token = None
        progress_token = None
        try:
            # Durable collection context validation (binding owner
            # decision, Spec 026 §8): the durable record must carry a
            # well-formed context that is still permitted (the
            # server-configured collection); execution uses ONLY the
            # durable value.
            if (not isinstance(collection_context, str)
                    or not collection_context
                    or len(collection_context) > 128
                    or collection_context != DEFAULT_COLLECTION_NAME):
                return HandlerOutcome(
                    kind="failure",
                    error=SanitizedError(
                        code="collection_context_invalid",
                        summary="collection context is missing, "
                                "malformed, or no longer permitted"),
                    retryable=False,
                    detail={"reason": "collection_context_invalid"})
            token = set_collection_name(collection_context)

            def _progress(phase: str) -> None:
                recorder.advance_stage(job_id, phase)

            progress_token = rendering_services._artifact_progress_cb.set(
                _progress)

            result = run_artifact_generation(
                recorder=recorder,
                cancel_check=cancel_check,
                tenant_id=tenant_id,
                job_id=job_id,
                **local_payload)
        except CancellationError:
            recorder.mark_cancelled(job_id)
            return recorder.outcome()
        except Exception as exc:  # noqa: BLE001 - classified below
            # Non-retryable: rendering is deterministic given the
            # durable input; a retry would fail again or repeat
            # provider cost (Spec 026 §13).
            return HandlerOutcome(
                kind="failure",
                error=SanitizedError(
                    code=error_code_for_exception(exc),
                    summary=sanitize_error_summary(
                        exc.__class__.__name__)),
                retryable=False,
                detail={"exception_class": exc.__class__.__name__})
        finally:
            if progress_token is not None:
                rendering_services._artifact_progress_cb.reset(
                    progress_token)
            if token is not None:
                _collection_name_ctx.reset(token)
        recorder.complete_job(job_id)
        outcome = recorder.outcome()
        outcome.result_metadata = result
        return outcome

    return _run


_HANDLERS: Dict[str, Callable] = {
    "v2_document": document_handler,
    "v2_upload": document_handler,
    "v2_mediawiki": mediawiki_handler,
    "v2_artifact": artifact_handler,
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


def run_artifact_job(task, *, job_id: str, attempt_id: str,
                     tenant_id: str, dispatch_token: str,
                     celery_task_id: str, payload: Dict[str, Any]) -> str:
    """Celery body for the v2 artifact task (Spec 026).  The
    collection context is validated INSIDE the handler from the
    durable submission record (never re-inferred from process-global
    state), so the task body passes the payload through untouched."""
    from retriva.registry import CapabilityRegistry

    CapabilityRegistry().load_extensions()
    service = jobs_service()
    return _execute_via_protocol(
        task, service, "v2_artifact", payload, job_id, attempt_id,
        tenant_id, dispatch_token, celery_task_id)


def _execute_via_protocol(task, service: JobsService, job_type: str,
                          payload: Dict[str, Any], job_id: str,
                          attempt_id: str, tenant_id: str,
                          dispatch_token: str,
                          celery_task_id: str) -> str:
    from retriva.ingestion_api.execution import CancellationError
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
    knowledge: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Spec 028 knowledge integration helpers
# ---------------------------------------------------------------------------

def _knowledge_pipeline_or_none():
    """Return the knowledge pipeline when native ingestion is permitted;
    None when the knowledge schema is absent (legacy path); raise the
    fail-closed error when the schema exists but authority is not
    authoritative."""
    from retriva.knowledge.pipeline import knowledge_pipeline

    pipeline = knowledge_pipeline()
    if not pipeline.schema_present():
        return None
    pipeline.enabled()  # raises KnowledgeIngestionUnavailable if gated
    return pipeline


def _fingerprint_source(path: Optional[str]) -> Optional[str]:
    """Best-effort sha256 fingerprint of a server-side source file.
    Returns None when the bytes are not readable (documented reindex
    limitation: version identity then cannot dedup)."""
    if not path or not os.path.isfile(path):
        return None
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(block)
        return f"sha256:{h.hexdigest()}"
    except OSError:
        return None


def _knowledge_collection(collection_name: Optional[str]) -> str:
    from retriva.indexing.qdrant_store import (
        DEFAULT_COLLECTION_NAME,
    )
    return collection_name or DEFAULT_COLLECTION_NAME


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
    knowledge = _register_knowledge(
        service, tenant_id=tenant_id, job_id=job.id,
        job_type="v2_document", kb_id=kb_id,
        collection_name=_knowledge_collection(collection_name),
        begin=lambda pipeline: pipeline.begin_document(
            tenant_id=tenant_id, source_uri=source_uri, kb_id=kb_id,
            collection_name=_knowledge_collection(collection_name),
            content_fingerprint=_fingerprint_source(source_uri),
            job_id=job.id))
    result = _dispatch_submitted(service, job, payload, background_tasks)
    result.knowledge = knowledge
    return result


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
    # MediaWiki registers one knowledge document PER PAGE during
    # execution (page identity is only known after parsing); the
    # submission hook only performs the fail-closed authority gate so
    # that pre-cutover submissions are rejected before any work.
    _knowledge_pipeline_or_none()
    return _dispatch_submitted(service, job, payload, background_tasks)


def _upload_job_payload(*, source_path, content_type, user_metadata,
                        parser_hint, temp_path, doc_id, content_hash,
                        kb_id, source_paths, content_size,
                        ingestion_status, created_at,
                        collection_name) -> Dict[str, Any]:
    """Stable upload job payload forwarded to the worker/local handler.

    Contract: keys MUST match the accepted worker handler signature
    (``process_document_task`` / ``process_document_v2``).  The
    processing-contract version is carried by the durable submission
    argument, NOT this execution payload."""
    return dict(
        source_uri=source_path, content_type=content_type,
        user_metadata=user_metadata, parser_hint=parser_hint,
        temp_path=temp_path, doc_id=doc_id, content_hash=content_hash,
        kb_id=kb_id, source_paths=source_paths,
        content_size=content_size,
        ingestion_status=ingestion_status, created_at=created_at,
        collection_name=collection_name)


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
    payload = _upload_job_payload(
        source_path=source_path, content_type=content_type,
        user_metadata=user_metadata, parser_hint=parser_hint,
        temp_path=temp_path, doc_id=doc_id, content_hash=content_hash,
        kb_id=kb_id, source_paths=source_paths,
        content_size=content_size, ingestion_status=ingestion_status,
        created_at=created_at, collection_name=collection_name)
    idempotency_key = f"v2up:{_hash_hex(f'{kb_id}|{hex_digest}')}"
    input_metadata = dict(payload)
    # Spec 030 / ADR-035: canonical, versioned semantic input identity
    # (excludes request-volatile temp_path/created_at and derived ids).
    input_metadata["input_fingerprint"] = upload_input_fingerprint(
        payload, tenant_id=tenant_id)
    job, _created = service.submit(
        tenant_id=tenant_id, job_type="v2_upload",
        execution_transport=_transport(),
        input_metadata=input_metadata,
        idempotency_key=idempotency_key,
        subject_type="document", subject_id=doc_id,
        payload_version=_default_payload_version(),
        _with_status=True)
    fingerprint = content_hash or _fingerprint_source(temp_path) \
        or _fingerprint_source(source_path)
    knowledge = _register_knowledge(
        service, tenant_id=tenant_id, job_id=job.id,
        job_type="v2_upload", kb_id=kb_id,
        collection_name=_knowledge_collection(collection_name),
        begin=lambda pipeline: pipeline.begin_upload(
            tenant_id=tenant_id, kb_id=kb_id, source_path=source_path,
            filename=os.path.basename(source_path or ""),
            collection_name=_knowledge_collection(collection_name),
            job_id=job.id, content_fingerprint=fingerprint))
    result = _dispatch_submitted(service, job, payload, background_tasks,
                                 created=_created)
    result.knowledge = knowledge
    return result


def _register_knowledge(service, *, tenant_id, job_id, job_type, kb_id,
                        collection_name, begin) -> Optional[Dict[str, Any]]:
    """Idempotently register knowledge evidence for a durable job and
    return its serialized context (None when knowledge is not enabled)."""
    pipeline = _knowledge_pipeline_or_none()
    if pipeline is None:
        return None
    existing = pipeline.context_for_job(tenant_id, job_id, job_type)
    if existing is not None:
        return existing.to_payload()
    context = begin(pipeline)
    return context.to_payload() if context is not None else None



def _transport() -> str:
    from retriva.ingestion_api.celery_app import celery_enabled
    return "celery" if celery_enabled() else "local"


def _dispatch_submitted(service: JobsService, job: JobRecord,
                        payload: Dict[str, Any],
                        background_tasks, *, created: bool = True
                        ) -> SubmissionResult:
    dispatch = service.dispatch_job(job, payload=payload,
                                    tenant_id=job.tenant_id)
    runner = dispatch.runner if dispatch is not None else None
    if runner is not None and background_tasks is not None:
        background_tasks.add_task(runner)
    return SubmissionResult(job=job, created=created, local_runner=runner)


def submit_artifact_job(*, tenant_id: str, artifact_id: str,
                        artifact_type: str, format: str,
                        parameters: Optional[Dict[str, Any]],
                        user_metadata: Optional[Dict[str, Any]],
                        collection_context: str,
                        background_tasks=None) -> SubmissionResult:
    """Durable v2 artifact submission + dispatch (Spec 026 / ADR-031).

    Binding contract: NO idempotency key (every accepted submission
    creates a NEW artifact + durable job); a canonical input
    fingerprint is persisted for diagnostics/reconciliation; the
    collection context is the server-resolved, bounded normalized
    reference (never client input); the artifact id is server-
    generated and persisted as the durable subject."""
    service = jobs_service()
    payload = dict(
        artifact_id=artifact_id, artifact_type=artifact_type,
        format=format, parameters=parameters or {},
        user_metadata=user_metadata,
        collection_context=collection_context)
    input_metadata = dict(payload)
    input_metadata["input_fingerprint"] = fingerprint_for(payload)
    job = service.submit(
        tenant_id=tenant_id, job_type="v2_artifact",
        execution_transport=_transport(),
        input_metadata=input_metadata,
        subject_type="artifact", subject_id=artifact_id,
        payload_version=_default_payload_version())
    return _dispatch_submitted(service, job, payload, background_tasks)


def run_artifact_generation(*, recorder, cancel_check, tenant_id: str,
                            job_id: str, artifact_id: str,
                            artifact_type: str, format: str,
                            parameters: Optional[Dict[str, Any]],
                            user_metadata: Optional[Dict[str, Any]],
                            ) -> Dict[str, Any]:
    """Render + ATOMIC finalization for one durable artifact attempt
    (Spec 026 §10).  Raises ``CancellationError`` when cancellation
    wins; every failure raises (the handler classifies).  Never
    overwrites an existing artifact unless its provenance proves it
    belongs to the same tenant, artifact, and durable job."""
    from retriva.ingestion_api.execution import CancellationError
    from retriva.indexing.qdrant_store import get_collection_name
    from retriva.infrastructure.storage import LocalStorageProvider

    from retriva.ingestion_api.artifact_store import (
        artifact_paths,
        hash_file,
        media_type_for,
        provenance_matches,
        quarantine_partial,
        read_provenance,
        validate_artifact_id,
        validate_format_extension,
        write_provenance,
    )

    recorder.advance_stage(job_id, "rendering")
    ext = validate_format_extension(format)
    validate_artifact_id(artifact_id)
    storage = LocalStorageProvider()
    final, partial, prov_path = artifact_paths(
        Path(storage.base_path), artifact_id, ext)

    # Idempotent finalization: a complete artifact with provenance
    # matching THIS job/tenant is adopted as-is (no overwrite).
    if final.exists():
        prov = read_provenance(prov_path)
        if final == final and prov is not None:
            try:
                size, sha256 = hash_file(final)
            except OSError:
                size, sha256 = -1, ""
            if provenance_matches(
                    prov, tenant_id=tenant_id, artifact_id=artifact_id,
                    job_id=job_id, sha256=sha256, size=size):
                recorder.advance_stage(job_id, "finalizing")
                recorder.complete_job(job_id)
                return {
                    "artifact_id": artifact_id,
                    "storage_ref": str(final.relative_to(
                        Path(storage.base_path))),
                    "media_type": media_type_for(final.name),
                    "size": size,
                    "sha256": sha256,
                }
        raise ValueError(
            "artifact output already exists without matching "
            "provenance; overwrite refused")

    from retriva.rendering import get_renderer
    renderer = get_renderer(format)
    if renderer is None:
        raise LookupError(f"no renderer registered for {format!r}")

    if not renderer.render(
            artifact_type=artifact_type,
            parameters=parameters or {},
            output_path=partial,
            cancel_check=cancel_check):
        quarantine_partial(partial, prov_path)
        if cancel_check is not None and cancel_check():
            raise CancellationError("artifact rendering cancelled")
        raise RuntimeError("artifact rendering failed")

    if cancel_check is not None and cancel_check():
        quarantine_partial(partial, prov_path)
        raise CancellationError("artifact cancelled before finalization")

    # Finalization: the partial file was flushed and closed by the
    # renderer (render() returned); finalize atomically within the
    # same directory.
    recorder.advance_stage(job_id, "finalizing")
    size, sha256 = hash_file(partial)
    os.replace(partial, final)
    storage_ref = str(final.relative_to(Path(storage.base_path)))
    write_provenance(
        prov_path, tenant_id=tenant_id, artifact_id=artifact_id,
        job_id=job_id, format=format, sha256=sha256, size=size,
        storage_ref=storage_ref)
    recorder.complete_job(job_id)
    return {
        "artifact_id": artifact_id,
        "storage_ref": storage_ref,
        "media_type": media_type_for(final.name),
        "size": size,
        "sha256": sha256,
    }
