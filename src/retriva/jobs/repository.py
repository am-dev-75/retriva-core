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

"""PostgreSQL repository for the durable job lifecycle (Spec 025
§3.1–§3.2, §3.5, §3.8; ADR-030).

Every transition is a predicate-guarded atomic UPDATE (rowcount
decides) inside one transaction that also writes the transition
event; duplicates are idempotent no-ops.  Tenant context is set with
``SET LOCAL app.current_tenant`` per transaction and fails closed
(Constitution §32).  Terminal states are immutable; late and
duplicate callbacks never move state backward.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor

from retriva.infrastructure.postgres.config import (
    PostgresPlatformSettings,
)
from retriva.infrastructure.postgres.tenant import (
    set_tenant_context,
    validate_tenant_id,
)
from retriva.jobs.domain import (
    TERMINAL_ATTEMPT_STATUSES,
    TERMINAL_JOB_STATUSES,
    AttemptRecord,
    AttemptStatus,
    EventActor,
    EventRecord,
    EventType,
    ExecutionTransport,
    JobRecord,
    JobStatus,
    PublicationState,
    RetryClass,
    SanitizedError,
)
from retriva.jobs.errors import (
    IdempotencyConflictError,
    JobNotFoundError,
    JobsError,
    OperatorRetryRefusedError,
    TenantContextMissingError,
)

#: Reserved bounded key carrying the submission's input identity for
#: idempotency-conflict detection (Spec 025 §3.1).
INPUT_FINGERPRINT_KEY = "input_fingerprint"

#: Metadata JSONB hard bound (matches the SQL CHECK constraints).
METADATA_MAX_BYTES = 16384

#: Bounded event `detail` bound (matches the SQL CHECK constraint).
EVENT_DETAIL_MAX_BYTES = 16384

#: Backoff cap for durable retry scheduling (seconds).
RETRY_BACKOFF_CAP_SECONDS = 600

_PRIVILEGED_CLEANUP_GUC = "app.jobs_privileged_cleanup"


class ClaimOutcome(str, Enum):
    """Deterministic classification of a delivery against the durable
    attempt state (Spec 025 §3.5)."""

    GRANTED = "granted"                        # execute the handler
    GRANTED_TAKEOVER = "granted_takeover"      # proven-dead prior execution
    REFUSED_CANCELLED = "refused_cancelled"    # cancel intent won the race
    DUPLICATE = "duplicate"                    # same worker, live execution
    LEAVE_FOR_RECONCILIATION = (
        "leave_for_reconciliation")            # prior worker possibly alive
    STALE_DELIVERY = "stale_delivery"          # token/task/generation mismatch
    TERMINAL_NOOP = "terminal_noop"            # attempt or job already terminal


class CancellationOutcome(str, Enum):
    """Result of a cancellation request (Spec 025 §3.3)."""

    CANCELLED = "cancelled"                # pending → cancelled (nothing published)
    REQUESTED = "requested"                # durable cancel intent recorded
    ALREADY_TERMINAL = "already_terminal"  # terminal job: typed no-op
    NOT_CANCELLABLE = "not_cancellable"    # manual_review: operator-only


@dataclass(frozen=True)
class ClaimDecision:
    outcome: ClaimOutcome
    attempt: Optional[AttemptRecord] = None
    job: Optional[JobRecord] = None
    detail: Dict[str, Any] | None = None


@dataclass(frozen=True)
class FailureDecision:
    """Outcome of a recorded execution failure."""

    job_status: JobStatus
    retry_scheduled: bool
    scheduled_at: Optional[datetime]
    attempt: AttemptRecord


@dataclass(frozen=True)
class CancelDecision:
    outcome: CancellationOutcome
    job: Optional[JobRecord]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _job_from_row(row) -> JobRecord:
    return JobRecord(
        id=row["id"],
        tenant_id=row["tenant_id"],
        job_type=row["job_type"],
        payload_version=row["payload_version"],
        status=JobStatus(row["status"]),
        execution_transport=ExecutionTransport(row["execution_transport"]),
        subject_type=row["subject_type"],
        subject_id=row["subject_id"],
        input_metadata=row["input_metadata"] or {},
        result_metadata=row["result_metadata"],
        progress=row["progress"],
        progress_stage=row["progress_stage"],
        progress_message=row["progress_message"],
        idempotency_key=row["idempotency_key"],
        requested_by=row["requested_by"],
        queue=row["queue"],
        scheduled_at=row["scheduled_at"],
        submitted_at=row["submitted_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        cancelled_at=row["cancelled_at"],
        cancel_requested_at=row["cancel_requested_at"],
        attempt_count=int(row["attempt_count"]),
        max_attempts=int(row["max_attempts"]),
        last_error_code=row["last_error_code"],
        last_error_summary=row["last_error_summary"],
        celery_task_id=row["celery_task_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        purge_after=row["purge_after"],
    )


def _attempt_from_row(row) -> AttemptRecord:
    return AttemptRecord(
        id=row["id"],
        job_id=row["job_id"],
        tenant_id=row["tenant_id"],
        attempt_no=int(row["attempt_no"]),
        dispatch_generation=int(row["dispatch_generation"]),
        dispatch_token=row["dispatch_token"],
        celery_task_id=row["celery_task_id"],
        publication_state=PublicationState(row["publication_state"]),
        status=AttemptStatus(row["status"]),
        published_at=row["published_at"],
        publication_tries=int(row["publication_tries"]),
        execution_generation=int(row["execution_generation"]),
        worker_id=row["worker_id"],
        dispatched_at=row["dispatched_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        retry_class=(RetryClass(row["retry_class"])
                     if row["retry_class"] else None),
        error_code=row["error_code"],
        error_summary=row["error_summary"],
        detail=row["detail"] or {},
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _bounded_metadata(metadata: Optional[Dict[str, Any]],
                      bound: int = METADATA_MAX_BYTES) -> Dict[str, Any]:
    if not metadata:
        return {}
    rendered = json.dumps(metadata, default=str)
    if len(rendered.encode("utf-8")) > bound:
        raise JobsError(
            f"job metadata exceeds the persisted bound of {bound} bytes")
    return metadata


def _bounded_event_detail(detail: Dict[str, Any]) -> Dict[str, Any]:
    if not detail:
        return {}
    rendered = json.dumps(detail, default=str)
    if len(rendered.encode("utf-8")) > EVENT_DETAIL_MAX_BYTES:
        raise JobsError(
            "event detail exceeds the persisted bound of "
            f"{EVENT_DETAIL_MAX_BYTES} bytes")
    return detail


def _retention_interval_sql(status_value: str) -> str:
    days = {"succeeded": 30, "failed": 90, "cancelled": 90}.get(
        status_value)
    if days is None:
        raise JobsError(
            f"no retention policy for status {status_value!r}")
    return f"make_interval(days => {int(days)})"


class PostgresJobsRepository:
    """psycopg2 adapter for the durable job lifecycle.

    Each operation opens one connection (the Core runtime role), sets
    the tenant context with ``SET LOCAL``, applies guarded statements,
    and commits; on error everything rolls back.  Privileged operations
    (retention purge, cross-tenant reconciliation selectors) run on the
    migrator connection with the controlled cleanup GUC set.
    """

    def __init__(self, settings: PostgresPlatformSettings,
                 *, privileged_roles: bool = False) -> None:
        self._settings = settings
        self._privileged_roles = privileged_roles

    # -- plumbing ---------------------------------------------------------

    @contextmanager
    def _transaction(
            self, tenant_id: Optional[str] = None,
            *, privileged: bool = False) -> Iterator[Any]:
        role = "migrator" if privileged else "core"
        conn = psycopg2.connect(
            **self._settings.connection_kwargs(role))
        try:
            with conn:
                with conn.cursor(
                        cursor_factory=RealDictCursor) as cur:
                    if tenant_id is not None:
                        try:
                            set_tenant_context(cur, tenant_id)
                        except Exception as exc:
                            raise TenantContextMissingError(
                                str(exc)) from exc
                    if privileged:
                        cur.execute(
                            "SELECT set_config(%s, 'granted', true)",
                            (_PRIVILEGED_CLEANUP_GUC,))
                    yield cur
        except psycopg2.Error:
            raise
        finally:
            conn.close()

    def _record_event(self, cur, *, job_id: str, tenant_id: str,
                      event_type: EventType, actor: EventActor,
                      from_status: Optional[JobStatus],
                      to_status: Optional[JobStatus],
                      attempt_id: Optional[str] = None,
                      detail: Optional[Dict[str, Any]] = None) -> None:
        import uuid

        cur.execute(
            "INSERT INTO jobs.job_events "
            "(id, job_id, tenant_id, event_type, actor, from_status, "
            " to_status, attempt_id, detail) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (uuid.uuid4().hex, job_id, tenant_id, event_type.value,
             actor.value,
             from_status.value if from_status else None,
             to_status.value if to_status else None,
             attempt_id, json.dumps(_bounded_event_detail(detail or {}),
                                    default=str)))

    # -- submission (T1) ----------------------------------------------------

    def submit_job(
            self, *, tenant_id: str, job_id: str, job_type: str,
            payload_version: str, execution_transport: str,
            input_metadata: Optional[Dict[str, Any]] = None,
            idempotency_key: Optional[str] = None,
            requested_by: Optional[str] = None,
            subject_type: Optional[str] = None,
            subject_id: Optional[str] = None,
            max_attempts: Optional[int] = None,
            queue: Optional[str] = None,
            actor: EventActor = EventActor.API,
    ) -> Tuple[JobRecord, bool]:
        """Insert a ``pending`` job (T1).  Idempotency: an existing
        (tenant, job_type, idempotency_key) row is returned as-is
        (created=False) when the input identity matches; a key reuse
        with a different input identity raises
        :class:`IdempotencyConflictError` (409 at the API boundary)."""
        tenant_id = validate_tenant_id(tenant_id)
        metadata = _bounded_metadata(input_metadata)
        with self._transaction(tenant_id) as cur:
            if idempotency_key:
                existing = self._find_by_idempotency(
                    cur, tenant_id, job_type, idempotency_key)
                if existing is not None:
                    fingerprint = (metadata or {}).get(
                        INPUT_FINGERPRINT_KEY)
                    prior = (existing["input_metadata"] or {}).get(
                        INPUT_FINGERPRINT_KEY)
                    if fingerprint and prior and fingerprint != prior:
                        raise IdempotencyConflictError(
                            "idempotency key is already used by a "
                            "different input identity")
                    return _job_from_row(existing), False
            max_attempts_value = (
                int(max_attempts) if max_attempts is not None else None)
            try:
                if max_attempts_value is None:
                    cur.execute(
                        "INSERT INTO jobs.jobs "
                        "(id, tenant_id, job_type, payload_version, "
                        " status, subject_type, subject_id, "
                        " input_metadata, idempotency_key, requested_by, "
                        " queue, execution_transport) "
                        "VALUES (%s, %s, %s, %s, 'pending', %s, %s, %s, "
                        "        %s, %s, %s, %s) "
                        "RETURNING *",
                        (job_id, tenant_id, job_type, payload_version,
                         subject_type, subject_id,
                         json.dumps(metadata, default=str),
                         idempotency_key, requested_by, queue,
                         execution_transport))
                else:
                    cur.execute(
                        "INSERT INTO jobs.jobs "
                        "(id, tenant_id, job_type, payload_version, "
                        " status, subject_type, subject_id, "
                        " input_metadata, idempotency_key, requested_by, "
                        " queue, execution_transport, max_attempts) "
                        "VALUES (%s, %s, %s, %s, 'pending', %s, %s, %s, "
                        "        %s, %s, %s, %s, %s) "
                        "RETURNING *",
                        (job_id, tenant_id, job_type, payload_version,
                         subject_type, subject_id,
                         json.dumps(metadata, default=str),
                         idempotency_key, requested_by, queue,
                         execution_transport, max_attempts_value))
                row = cur.fetchone()
            except psycopg2.errors.UniqueViolation:
                # Lost a race with a concurrent identical submission:
                # idempotent convergence on the winner's row.
                existing = self._find_by_idempotency(
                    cur, tenant_id, job_type, idempotency_key)
                if existing is None:
                    raise
                return _job_from_row(existing), False
            self._record_event(
                cur, job_id=job_id, tenant_id=tenant_id,
                event_type=EventType.JOB_CREATED, actor=actor,
                from_status=None, to_status=JobStatus.PENDING,
                detail={"payload_version": payload_version,
                        "execution_transport": execution_transport})
            return _job_from_row(row), True

    @staticmethod
    def _find_by_idempotency(cur, tenant_id, job_type,
                             idempotency_key):
        cur.execute(
            "SELECT * FROM jobs.jobs WHERE tenant_id = %s "
            "AND job_type = %s AND idempotency_key = %s",
            (tenant_id, job_type, idempotency_key))
        return cur.fetchone()

    # -- dispatch preparation (T3 / operator retry T21) ----------------------

    def prepare_dispatch(
            self, *, tenant_id: str, job_id: str,
            dispatch_token: str, celery_task_id: str,
            attempt_id: str, actor: EventActor = EventActor.SYSTEM,
            event_type: EventType = EventType.DISPATCH_PREPARED,
    ) -> Optional[AttemptRecord]:
        """T3: ``pending -> dispatching`` + create the durable attempt
        (preallocated Celery task id, dispatch token, publication
        state ``prepared``).  Returns None when the guard does not
        match (concurrent actor, cancel already requested)."""
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "UPDATE jobs.jobs SET status = 'dispatching', "
                "attempt_count = attempt_count + 1, updated_at = now() "
                "WHERE id = %s AND tenant_id = %s AND status = 'pending' "
                "AND cancel_requested_at IS NULL "
                "RETURNING attempt_count, max_attempts",
                (job_id, tenant_id))
            guard = cur.fetchone()
            if guard is None:
                return None
            attempt_no = int(guard["attempt_count"])
            cur.execute(
                "INSERT INTO jobs.job_attempts "
                "(id, job_id, tenant_id, attempt_no, "
                " dispatch_generation, dispatch_token, celery_task_id, "
                " publication_state, status, dispatched_at) "
                "VALUES (%s, %s, %s, %s, 1, %s, %s, 'prepared', "
                "        'queued', now()) RETURNING *",
                (attempt_id, job_id, tenant_id, attempt_no,
                 dispatch_token, celery_task_id))
            row = cur.fetchone()
            self._record_event(
                cur, job_id=job_id, tenant_id=tenant_id,
                event_type=event_type, actor=actor,
                from_status=JobStatus.PENDING,
                to_status=JobStatus.DISPATCHING,
                attempt_id=attempt_id,
                detail={"attempt_no": attempt_no,
                        "dispatch_generation": 1,
                        "preallocated_task_id": True})
            return _attempt_from_row(row)

    def prepare_retry_dispatch(
            self, *, tenant_id: str, job_id: str,
            dispatch_token: str, celery_task_id: str,
            attempt_id: str,
    ) -> Optional[AttemptRecord]:
        """T20: ``retry_wait -> dispatching`` for a due retry (new
        attempt number; the executor publishes after commit)."""
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "UPDATE jobs.jobs SET status = 'dispatching', "
                "attempt_count = attempt_count + 1, scheduled_at = NULL, "
                "updated_at = now() "
                "WHERE id = %s AND tenant_id = %s AND status = "
                "'retry_wait' AND scheduled_at <= now() "
                "AND cancel_requested_at IS NULL "
                "RETURNING attempt_count",
                (job_id, tenant_id))
            guard = cur.fetchone()
            if guard is None:
                return None
            attempt_no = int(guard["attempt_count"])
            cur.execute(
                "INSERT INTO jobs.job_attempts "
                "(id, job_id, tenant_id, attempt_no, "
                " dispatch_generation, dispatch_token, celery_task_id, "
                " publication_state, status, dispatched_at) "
                "VALUES (%s, %s, %s, %s, 1, %s, %s, 'prepared', "
                "        'queued', now()) RETURNING *",
                (attempt_id, job_id, tenant_id, attempt_no,
                 dispatch_token, celery_task_id))
            row = cur.fetchone()
            self._record_event(
                cur, job_id=job_id, tenant_id=tenant_id,
                event_type=EventType.RETRY_DISPATCHED,
                actor=EventActor.SYSTEM,
                from_status=JobStatus.RETRY_WAIT,
                to_status=JobStatus.DISPATCHING,
                attempt_id=attempt_id,
                detail={"attempt_no": attempt_no})
            return _attempt_from_row(row)

    def prepare_operator_retry(
            self, *, tenant_id: str, job_id: str,
            dispatch_token: str, celery_task_id: str,
            attempt_id: str, reason: str,
            override_max_attempts: bool = False,
    ) -> Optional[AttemptRecord]:
        """T21 (operator CLI only): ``failed -> queued`` with a new
        durable attempt; a durably recorded override raises
        ``max_attempts`` for this job.  Returns None when the job is
        not in an operator-retryable state."""
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "UPDATE jobs.jobs SET status = 'queued', "
                "attempt_count = attempt_count + 1, "
                "last_error_code = NULL, last_error_summary = NULL, "
                "max_attempts = CASE WHEN %s THEN attempt_count + %s "
                "ELSE max_attempts END, "
                "updated_at = now() "
                "WHERE id = %s AND tenant_id = %s AND status = 'failed' "
                "AND (attempt_count < max_attempts OR %s) "
                "RETURNING attempt_count",
                (bool(override_max_attempts), 1, job_id, tenant_id,
                 bool(override_max_attempts)))
            guard = cur.fetchone()
            if guard is None:
                return None
            attempt_no = int(guard["attempt_count"])
            cur.execute(
                "INSERT INTO jobs.job_attempts "
                "(id, job_id, tenant_id, attempt_no, "
                " dispatch_generation, dispatch_token, celery_task_id, "
                " publication_state, status, dispatched_at, "
                " retry_class) "
                "VALUES (%s, %s, %s, %s, 1, %s, %s, 'prepared', "
                "        'queued', now(), 'operator_override') "
                "RETURNING *",
                (attempt_id, job_id, tenant_id, attempt_no,
                 dispatch_token, celery_task_id))
            row = cur.fetchone()
            self._record_event(
                cur, job_id=job_id, tenant_id=tenant_id,
                event_type=EventType.OPERATOR_RETRY,
                actor=EventActor.OPERATOR,
                from_status=JobStatus.FAILED,
                to_status=JobStatus.QUEUED,
                attempt_id=attempt_id,
                detail={"attempt_no": attempt_no,
                        "reason": reason or "",
                        "override_max_attempts":
                            bool(override_max_attempts)})
            return _attempt_from_row(row)

    def resolve_manual_review(
            self, *, tenant_id: str, job_id: str, resolution: str,
            reason: str, actor_label: str,
            dispatch_token: Optional[str] = None,
            celery_task_id: Optional[str] = None,
            attempt_id: Optional[str] = None,
    ) -> Optional[JobRecord]:
        """T22: operator resolution of a ``manual_review`` job.

        ``resolution`` is one of ``succeeded`` / ``failed`` /
        ``cancelled`` / ``queued`` (operator-approved re-dispatch:
        creates a new attempt like an operator retry).  Returns None
        when the job is not in ``manual_review``."""
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            if resolution == "queued":
                cur.execute(
                    "UPDATE jobs.jobs SET status = 'queued', "
                    "attempt_count = attempt_count + 1, "
                    "updated_at = now() "
                    "WHERE id = %s AND tenant_id = %s "
                    "AND status = 'manual_review' "
                    "RETURNING attempt_count",
                    (job_id, tenant_id))
                guard = cur.fetchone()
                if guard is None:
                    return None
                attempt_no = int(guard["attempt_count"])
                if not (dispatch_token and celery_task_id
                        and attempt_id):
                    raise JobsError(
                        "re-dispatch resolution requires the "
                        "preallocated attempt identity")
                cur.execute(
                    "INSERT INTO jobs.job_attempts "
                    "(id, job_id, tenant_id, attempt_no, "
                    " dispatch_generation, dispatch_token, "
                    " celery_task_id, publication_state, status, "
                    " dispatched_at, retry_class) "
                    "VALUES (%s, %s, %s, %s, 1, %s, %s, 'prepared', "
                    "        'queued', now(), 'operator_override') "
                    "RETURNING *",
                    (attempt_id, job_id, tenant_id, attempt_no,
                     dispatch_token, celery_task_id))
                cur.fetchone()
                to_status = JobStatus.QUEUED
            elif resolution in ("succeeded", "failed", "cancelled"):
                purge = _retention_interval_sql(resolution)
                cur.execute(
                    "UPDATE jobs.jobs SET status = %s, "
                    "finished_at = COALESCE(finished_at, now()), "
                    "cancelled_at = CASE WHEN %s = 'cancelled' THEN "
                    "  COALESCE(cancelled_at, now()) ELSE cancelled_at "
                    "END, purge_after = now() + " + purge + ", "
                    "updated_at = now() "
                    "WHERE id = %s AND tenant_id = %s "
                    "AND status = 'manual_review' RETURNING *",
                    (resolution, resolution, job_id, tenant_id))
                row = cur.fetchone()
                if row is None:
                    return None
                to_status = JobStatus(resolution)
            else:
                raise JobsError(
                    f"unknown manual-review resolution {resolution!r}")
            self._record_event(
                cur, job_id=job_id, tenant_id=tenant_id,
                event_type=EventType.OPERATOR_RESOLUTION,
                actor=EventActor.OPERATOR,
                from_status=JobStatus.MANUAL_REVIEW,
                to_status=to_status,
                detail={"resolution": resolution,
                        "reason": reason or "",
                        "operator": actor_label or ""})
            cur.execute(
                "SELECT * FROM jobs.jobs WHERE id = %s AND tenant_id = %s",
                (job_id, tenant_id))
            return _job_from_row(cur.fetchone())

    # -- publication outcomes (T4/T5/T6) --------------------------------------

    def record_publication_inflight(
            self, *, tenant_id: str, job_id: str, attempt_id: str,
            republish: bool = False,
    ) -> bool:
        """Set the attempt's publication state to ``publishing`` (the
        publish call is about to run); ``publication_tries`` counts
        publication tries of the same dispatch generation."""
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            guard = "'prepared'" if not republish else \
                "'prepared', 'published', 'unknown'"
            cur.execute(
                f"UPDATE jobs.job_attempts SET publication_state = "
                f"'publishing', publication_tries = publication_tries + 1, "
                f"updated_at = now() WHERE id = %s AND tenant_id = %s "
                f"AND publication_state IN ({guard})",
                (attempt_id, tenant_id))
            return cur.rowcount == 1

    def record_publication_confirmed(
            self, *, tenant_id: str, job_id: str, attempt_id: str,
            actor: EventActor = EventActor.SYSTEM,
            evidence: str = "publish_returned",
    ) -> bool:
        """T4 (and T7 delivery evidence): publication confirmed → the
        attempt is ``published`` and the job is ``queued``."""
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "UPDATE jobs.job_attempts SET publication_state = "
                "'published', published_at = now(), updated_at = now() "
                "WHERE id = %s AND tenant_id = %s "
                "AND publication_state = 'publishing'",
                (attempt_id, tenant_id))
            if cur.rowcount != 1:
                return False
            cur.execute(
                "UPDATE jobs.jobs SET status = 'queued', "
                "celery_task_id = (SELECT celery_task_id FROM "
                "jobs.job_attempts WHERE id = %s), updated_at = now() "
                "WHERE id = %s AND tenant_id = %s AND status IN "
                "('dispatching', 'dispatch_unknown')",
                (attempt_id, job_id, tenant_id))
            moved = cur.rowcount == 1
            if not moved:
                cur.execute(
                    "SELECT status FROM jobs.jobs WHERE id = %s "
                    "AND tenant_id = %s", (job_id, tenant_id))
                row = cur.fetchone()
                if row is None or row["status"] not in (
                        "queued", "dispatch_unknown"):
                    raise JobsError(
                        "publication confirmed for a job that is no "
                        "longer dispatchable")
            self._record_event(
                cur, job_id=job_id, tenant_id=tenant_id,
                event_type=EventType.DISPATCH_CONFIRMED,
                actor=actor,
                from_status=None,
                to_status=JobStatus.QUEUED,
                attempt_id=attempt_id,
                detail={"evidence": evidence})
            return True

    def record_publication_rejected(
            self, *, tenant_id: str, job_id: str, attempt_id: str,
            error: SanitizedError,
    ) -> bool:
        """T5: DEFINITE rejection before broker acceptance → the
        attempt is abandoned (``dispatch_failed``) and the job returns
        to ``pending`` (explicitly retryable; the next dispatch
        creates a new attempt).  A raced cancellation keeps the job in
        ``cancelling`` (reconciliation resolves it)."""
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "UPDATE jobs.job_attempts SET publication_state = "
                "'rejected', status = 'dispatch_failed', "
                "finished_at = now(), error_code = %s, "
                "error_summary = %s, updated_at = now() "
                "WHERE id = %s AND tenant_id = %s "
                "AND publication_state = 'publishing'",
                (error.code, error.summary, attempt_id, tenant_id))
            if cur.rowcount != 1:
                return False
            cur.execute(
                "UPDATE jobs.jobs SET status = 'pending', "
                "last_error_code = %s, last_error_summary = %s, "
                "updated_at = now() "
                "WHERE id = %s AND tenant_id = %s AND status = "
                "'dispatching' AND cancel_requested_at IS NULL",
                (error.code, error.summary, job_id, tenant_id))
            self._record_event(
                cur, job_id=job_id, tenant_id=tenant_id,
                event_type=EventType.DISPATCH_REJECTED,
                actor=EventActor.SYSTEM,
                from_status=JobStatus.DISPATCHING,
                to_status=JobStatus.PENDING,
                attempt_id=attempt_id,
                detail={"error_code": error.code})
            return True

    def record_publication_ambiguous(
            self, *, tenant_id: str, job_id: str, attempt_id: str,
            error: SanitizedError,
    ) -> bool:
        """T6: AMBIGUOUS publication outcome → ``dispatch_unknown``.
        NEVER blindly reverted to ``pending``; reconciliation resolves
        by evidence (R2)."""
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "UPDATE jobs.job_attempts SET publication_state = "
                "'unknown', error_code = %s, error_summary = %s, "
                "updated_at = now() "
                "WHERE id = %s AND tenant_id = %s "
                "AND publication_state = 'publishing'",
                (error.code, error.summary, attempt_id, tenant_id))
            if cur.rowcount != 1:
                return False
            cur.execute(
                "UPDATE jobs.jobs SET status = 'dispatch_unknown', "
                "updated_at = now() "
                "WHERE id = %s AND tenant_id = %s AND status IN "
                "('dispatching', 'queued')",
                (job_id, tenant_id))
            self._record_event(
                cur, job_id=job_id, tenant_id=tenant_id,
                event_type=EventType.DISPATCH_AMBIGUOUS,
                actor=EventActor.SYSTEM,
                from_status=JobStatus.DISPATCHING,
                to_status=JobStatus.DISPATCH_UNKNOWN,
                attempt_id=attempt_id,
                detail={"error_code": error.code})
            return True

    # -- worker claim (T10 + §3.5 protocol) -----------------------------------

    def claim_for_delivery(
            self, *, tenant_id: str, job_id: str, attempt_id: str,
            dispatch_token: str, celery_task_id: str, worker_id: str,
            redelivered: bool = False,
            stale_threshold_seconds: int = 1800,
    ) -> ClaimDecision:
        """The durable, atomic worker-side claim (Spec 025 §3.5).

        Only one worker may claim an unclaimed attempt; duplicate
        deliveries are classified deterministically; the job/tenant
        consistency check happens BEFORE execution.  Lock order is
        job row first, then attempt row (all paths follow it)."""
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "SELECT * FROM jobs.jobs WHERE id = %s FOR UPDATE",
                (job_id,))
            job_row = cur.fetchone()
            if job_row is None or job_row["tenant_id"] != tenant_id:
                return ClaimDecision(
                    outcome=ClaimOutcome.STALE_DELIVERY,
                    detail={"reason": "job_not_found_or_tenant_mismatch"})
            job = _job_from_row(job_row)
            cur.execute(
                "SELECT * FROM jobs.job_attempts WHERE id = %s "
                "AND job_id = %s FOR UPDATE", (attempt_id, job_id))
            attempt_row = cur.fetchone()
            if attempt_row is None:
                return ClaimDecision(
                    outcome=ClaimOutcome.STALE_DELIVERY,
                    job=job,
                    detail={"reason": "attempt_not_found"})
            attempt = _attempt_from_row(attempt_row)

            # Consistency: tenant, dispatch token, preallocated task id.
            if attempt.tenant_id != job.tenant_id:
                return self._anomaly_and_stale(
                    cur, job, attempt, "tenant_mismatch")
            if attempt.dispatch_token != dispatch_token:
                return self._anomaly_and_stale(
                    cur, job, attempt, "dispatch_token_mismatch")
            if attempt.celery_task_id != celery_task_id:
                return self._anomaly_and_stale(
                    cur, job, attempt, "celery_task_id_mismatch")

            # Terminal job: nothing executes; contradictions are
            # recorded once as a bounded anomaly.
            if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED,
                              JobStatus.CANCELLED):
                if attempt.status not in TERMINAL_ATTEMPT_STATUSES_SET:
                    self._record_event(
                        cur, job_id=job_id, tenant_id=tenant_id,
                        event_type=EventType.ANOMALY,
                        actor=EventActor.WORKER,
                        from_status=job.status, to_status=None,
                        attempt_id=attempt_id,
                        detail={"reason": "delivery_for_terminal_job",
                                "attempt_status": attempt.status.value})
                return ClaimDecision(
                    outcome=ClaimOutcome.TERMINAL_NOOP, job=job,
                    attempt=attempt)

            attempt_terminal = (
                attempt.status in TERMINAL_ATTEMPT_STATUSES_SET)
            if attempt_terminal:
                # Idempotent no-op; a delivered message for a terminal
                # attempt (e.g. publication was classified rejected but
                # the broker accepted it) is a bounded anomaly.
                if attempt.status == AttemptStatus.DISPATCH_FAILED:
                    self._record_event(
                        cur, job_id=job_id, tenant_id=tenant_id,
                        event_type=EventType.ANOMALY,
                        actor=EventActor.WORKER,
                        from_status=None, to_status=None,
                        attempt_id=attempt_id,
                        detail={"reason":
                                "delivery_for_rejected_attempt"})
                return ClaimDecision(
                    outcome=ClaimOutcome.TERMINAL_NOOP, job=job,
                    attempt=attempt)

            cancel_intent = (
                job.cancel_requested_at is not None
                or job.status == JobStatus.CANCELLING)

            if attempt.status == AttemptStatus.QUEUED:
                if cancel_intent:
                    # Cancellation won before the claim: never-executed
                    # evidence (T14 via claim refusal).
                    cur.execute(
                        "UPDATE jobs.job_attempts SET status = "
                        "'cancelled', finished_at = now(), "
                        "updated_at = now() WHERE id = %s AND status = "
                        "'queued'", (attempt_id,))
                    cur.execute(
                        "UPDATE jobs.jobs SET status = 'cancelled', "
                        "cancelled_at = COALESCE(cancelled_at, now()), "
                        "finished_at = COALESCE(finished_at, now()), "
                        "purge_after = now() + "
                        + _retention_interval_sql("cancelled")
                        + ", updated_at = now() WHERE id = %s AND "
                        "status IN ('queued', 'dispatching', "
                        "'dispatch_unknown', 'cancelling')",
                        (job_id,))
                    self._record_event(
                        cur, job_id=job_id, tenant_id=tenant_id,
                        event_type=EventType.CANCELLED,
                        actor=EventActor.WORKER,
                        from_status=job.status,
                        to_status=JobStatus.CANCELLED,
                        attempt_id=attempt_id,
                        detail={"evidence": "claim_refused"})
                    cur.execute(
                        "SELECT * FROM jobs.jobs WHERE id = %s",
                        (job_id,))
                    job_row = cur.fetchone()
                    cur.execute(
                        "SELECT * FROM jobs.job_attempts WHERE id = %s",
                        (attempt_id,))
                    attempt_row = cur.fetchone()
                    return ClaimDecision(
                        outcome=ClaimOutcome.REFUSED_CANCELLED,
                        job=_job_from_row(job_row),
                        attempt=_attempt_from_row(attempt_row),
                        detail={"evidence": "claim_refused"})
                if job.status not in (JobStatus.QUEUED,
                                      JobStatus.DISPATCH_UNKNOWN,
                                      JobStatus.DISPATCHING):
                    # Divergence (e.g. attempt queued while the job
                    # moved on without it): never execute; surface.
                    self._record_event(
                        cur, job_id=job_id, tenant_id=tenant_id,
                        event_type=EventType.ANOMALY,
                        actor=EventActor.WORKER,
                        from_status=None, to_status=None,
                        attempt_id=attempt_id,
                        detail={"reason": "job_status_not_claimable",
                                "job_status": job.status.value})
                    return ClaimDecision(
                        outcome=ClaimOutcome.LEAVE_FOR_RECONCILIATION,
                        job=job, attempt=attempt,
                        detail={"reason": "job_status_not_claimable"})
                # Claim granted (T10): the delivery is publication
                # evidence when the outcome write never landed (T7).
                cur.execute(
                    "UPDATE jobs.job_attempts SET status = 'running', "
                    "worker_id = %s, started_at = now(), "
                    "publication_state = CASE WHEN publication_state "
                    "IN ('prepared', 'publishing', 'unknown') THEN "
                    "'published' ELSE publication_state END, "
                    "published_at = COALESCE(published_at, now()), "
                    "updated_at = now() "
                    "WHERE id = %s AND status = 'queued'",
                    (worker_id, attempt_id))
                cur.execute(
                    "UPDATE jobs.jobs SET status = 'running', "
                    "started_at = COALESCE(started_at, now()), "
                    "celery_task_id = %s, updated_at = now() "
                    "WHERE id = %s AND status IN ('queued', "
                    "'dispatch_unknown', 'dispatching')",
                    (celery_task_id, job_id))
                self._record_event(
                    cur, job_id=job_id, tenant_id=tenant_id,
                    event_type=EventType.ATTEMPT_CLAIMED,
                    actor=EventActor.WORKER,
                    from_status=job.status, to_status=JobStatus.RUNNING,
                    attempt_id=attempt_id,
                    detail={"worker_id": worker_id,
                            "redelivered": bool(redelivered),
                            "attempt_no": attempt.attempt_no})
                cur.execute(
                    "SELECT * FROM jobs.jobs WHERE id = %s", (job_id,))
                job_row = cur.fetchone()
                cur.execute(
                    "SELECT * FROM jobs.job_attempts WHERE id = %s",
                    (attempt_id,))
                attempt_row = cur.fetchone()
                return ClaimDecision(
                    outcome=ClaimOutcome.GRANTED,
                    job=_job_from_row(job_row),
                    attempt=_attempt_from_row(attempt_row))

            if attempt.status == AttemptStatus.RUNNING:
                # Delivery for an in-flight execution.
                if attempt.worker_id == worker_id:
                    # Same process: its own execution is live; duplicate
                    # delivery is an idempotent no-op.
                    return ClaimDecision(
                        outcome=ClaimOutcome.DUPLICATE, job=job,
                        attempt=attempt,
                        detail={"reason": "same_worker"})
                claim_age = None
                if attempt.started_at is not None:
                    started = attempt.started_at
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    claim_age = (_now() - started).total_seconds()
                if (claim_age is not None
                        and claim_age < stale_threshold_seconds):
                    # Prior worker possibly still alive: no execution.
                    return ClaimDecision(
                        outcome=ClaimOutcome.LEAVE_FOR_RECONCILIATION,
                        job=job, attempt=attempt,
                        detail={"claim_age_seconds": claim_age})
                # Proven-dead takeover of the SAME attempt: prior
                # execution evidence is lost, execution_generation+1
                # re-claims the attempt; NOT a new attempt number.
                cur.execute(
                    "UPDATE jobs.job_attempts SET "
                    "execution_generation = execution_generation + 1, "
                    "worker_id = %s, started_at = now(), "
                    "updated_at = now() WHERE id = %s AND status = "
                    "'running'", (worker_id, attempt_id))
                self._record_event(
                    cur, job_id=job_id, tenant_id=tenant_id,
                    event_type=EventType.ATTEMPT_LOST,
                    actor=EventActor.WORKER,
                    from_status=None, to_status=None,
                    attempt_id=attempt_id,
                    detail={"evidence": "takeover",
                            "lost_execution_generation":
                                attempt.execution_generation,
                            "redelivered": bool(redelivered)})
                cur.execute(
                    "SELECT * FROM jobs.job_attempts WHERE id = %s",
                    (attempt_id,))
                attempt_row = cur.fetchone()
                return ClaimDecision(
                    outcome=ClaimOutcome.GRANTED_TAKEOVER,
                    job=job,
                    attempt=_attempt_from_row(attempt_row),
                    detail={"evidence": "takeover"})

            # attempt status is LOST/CANCELLED/etc. (non-terminal set
            # exhausted above): conservative classification.
            return ClaimDecision(
                outcome=ClaimOutcome.LEAVE_FOR_RECONCILIATION,
                job=job, attempt=attempt,
                detail={"reason": "unclaimable_attempt_status",
                        "attempt_status": attempt.status.value})

    def _anomaly_and_stale(self, cur, job: JobRecord,
                           attempt: AttemptRecord,
                           reason: str) -> ClaimDecision:
        self._record_event(
            cur, job_id=job.id, tenant_id=job.tenant_id,
            event_type=EventType.ANOMALY, actor=EventActor.WORKER,
            from_status=None, to_status=None, attempt_id=attempt.id,
            detail={"reason": reason})
        return ClaimDecision(
            outcome=ClaimOutcome.STALE_DELIVERY, job=job,
            attempt=attempt, detail={"reason": reason})

    # -- execution progress / terminal transitions -----------------------------

    def record_progress(
            self, *, tenant_id: str, job_id: str,
            progress: Optional[int], stage: Optional[str],
            message: Optional[str],
    ) -> bool:
        """Durable progress write (throttled by the service layer);
        monotonic within a stage is enforced by the service."""
        tenant_id = validate_tenant_id(tenant_id)
        message = (message or "")[:512] or None
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "UPDATE jobs.jobs SET progress = %s, "
                "progress_stage = %s, progress_message = %s, "
                "updated_at = now() WHERE id = %s AND tenant_id = %s "
                "AND status IN ('running', 'cancelling')",
                (progress, stage, message, job_id, tenant_id))
            return cur.rowcount == 1

    def complete_success(
            self, *, tenant_id: str, job_id: str, attempt_id: str,
            result_metadata: Optional[Dict[str, Any]] = None,
            detail: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """T17/T15: durable success.  The job reaches ``succeeded``
        from ``running`` or ``cancelling`` (the latter records
        ``cancel_lost_race`` — the system must NOT report cancelled
        when side effects are known complete).  Idempotent."""
        tenant_id = validate_tenant_id(tenant_id)
        was_cancelling = False
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "UPDATE jobs.job_attempts SET status = 'succeeded', "
                "finished_at = now(), updated_at = now() "
                "WHERE id = %s AND tenant_id = %s AND status = 'running'",
                (attempt_id, tenant_id))
            if cur.rowcount != 1:
                return False
            cur.execute(
                "SELECT status FROM jobs.jobs WHERE id = %s "
                "FOR UPDATE", (job_id,))
            row = cur.fetchone()
            was_cancelling = bool(row and row["status"] == "cancelling")
            cur.execute(
                "UPDATE jobs.jobs SET status = 'succeeded', "
                "finished_at = now(), result_metadata = %s, "
                "purge_after = now() + "
                + _retention_interval_sql("succeeded")
                + ", updated_at = now() WHERE id = %s AND status IN "
                "('running', 'cancelling')",
                (json.dumps(_bounded_metadata(result_metadata, 16384),
                            default=str)
                 if result_metadata else None, job_id))
            applied = cur.rowcount == 1
            event_detail = {"cancel_lost_race": was_cancelling}
            if detail:
                event_detail.update(detail)
            if applied:
                self._record_event(
                    cur, job_id=job_id, tenant_id=tenant_id,
                    event_type=EventType.ATTEMPT_SUCCEEDED,
                    actor=EventActor.WORKER,
                    from_status=JobStatus.CANCELLING
                    if was_cancelling else JobStatus.RUNNING,
                    to_status=JobStatus.SUCCEEDED,
                    attempt_id=attempt_id, detail=event_detail)
            return applied

    def complete_failure(
            self, *, tenant_id: str, job_id: str, attempt_id: str,
            error: SanitizedError, retryable: bool,
            backoff_seconds_cap: int = RETRY_BACKOFF_CAP_SECONDS,
            detail: Optional[Dict[str, Any]] = None,
    ) -> Optional[FailureDecision]:
        """T18/T19: durable failure.  Retryable failures schedule a
        durable ``retry_wait`` (exponential backoff, capped) while
        ``attempt_count < max_attempts``; otherwise the job fails
        terminally.  Returns None when the attempt was not running
        (duplicate callback)."""
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "SELECT * FROM jobs.job_attempts WHERE id = %s FOR UPDATE",
                (attempt_id,))
            attempt_row = cur.fetchone()
            if attempt_row is None:
                raise JobNotFoundError("attempt not found")
            attempt_no = int(attempt_row["attempt_no"])
            cur.execute(
                "UPDATE jobs.job_attempts SET status = 'failed', "
                "finished_at = now(), retry_class = %s, "
                "error_code = %s, error_summary = %s, updated_at = now() "
                "WHERE id = %s AND status = 'running'",
                (RetryClass.RETRYABLE.value if retryable
                 else RetryClass.NON_RETRYABLE.value,
                 error.code, error.summary, attempt_id))
            if cur.rowcount != 1:
                return None
            cur.execute(
                "SELECT * FROM jobs.jobs WHERE id = %s FOR UPDATE",
                (job_id,))
            job = _job_from_row(cur.fetchone())
            will_retry = (
                retryable and attempt_no < job.max_attempts
                and job.status == JobStatus.RUNNING)
            if will_retry:
                backoff = min(2 ** max(attempt_no - 1, 0),
                              backoff_seconds_cap)
                scheduled = _now() + timedelta(seconds=backoff)
                cur.execute(
                    "UPDATE jobs.jobs SET status = 'retry_wait', "
                    "scheduled_at = %s, last_error_code = %s, "
                    "last_error_summary = %s, updated_at = now() "
                    "WHERE id = %s AND status = 'running'",
                    (scheduled, error.code, error.summary, job_id))
                self._record_event(
                    cur, job_id=job_id, tenant_id=tenant_id,
                    event_type=EventType.RETRY_SCHEDULED,
                    actor=EventActor.WORKER,
                    from_status=JobStatus.RUNNING,
                    to_status=JobStatus.RETRY_WAIT,
                    attempt_id=attempt_id,
                    detail={"attempt_no": attempt_no,
                            "backoff_seconds": backoff,
                            "error_code": error.code})
            else:
                cur.execute(
                    "UPDATE jobs.jobs SET status = 'failed', "
                    "finished_at = now(), last_error_code = %s, "
                    "last_error_summary = %s, purge_after = now() + "
                    + _retention_interval_sql("failed")
                    + ", updated_at = now() WHERE id = %s AND status = "
                    "'running'",
                    (error.code, error.summary, job_id))
                self._record_event(
                    cur, job_id=job_id, tenant_id=tenant_id,
                    event_type=EventType.ATTEMPT_FAILED,
                    actor=EventActor.WORKER,
                    from_status=JobStatus.RUNNING,
                    to_status=JobStatus.FAILED,
                    attempt_id=attempt_id,
                    detail={"attempt_no": attempt_no,
                            "error_code": error.code})
            cur.execute(
                "SELECT * FROM jobs.jobs WHERE id = %s", (job_id,))
            job_row = cur.fetchone()
            cur.execute(
                "SELECT * FROM jobs.job_attempts WHERE id = %s",
                (attempt_id,))
            attempt = _attempt_from_row(cur.fetchone())
            return FailureDecision(
                job_status=JobStatus(job_row["status"]),
                retry_scheduled=will_retry,
                scheduled_at=scheduled if will_retry else None,
                attempt=attempt)

    def acknowledge_cooperative_cancel(
            self, *, tenant_id: str, job_id: str, attempt_id: str,
            detail: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """T14 (worker ack): the worker observed the durable cancel
        intent at a checkpoint and stopped; attempt cancelled + job
        cancelled with never-executed-further evidence."""
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "UPDATE jobs.job_attempts SET status = 'cancelled', "
                "finished_at = now(), updated_at = now() "
                "WHERE id = %s AND tenant_id = %s AND status = 'running'",
                (attempt_id, tenant_id))
            if cur.rowcount != 1:
                return False
            cur.execute(
                "UPDATE jobs.jobs SET status = 'cancelled', "
                "cancelled_at = COALESCE(cancelled_at, now()), "
                "finished_at = COALESCE(finished_at, now()), "
                "purge_after = now() + "
                + _retention_interval_sql("cancelled")
                + ", updated_at = now() WHERE id = %s AND status = "
                "'cancelling'", (job_id,))
            applied = cur.rowcount == 1
            if applied:
                self._record_event(
                    cur, job_id=job_id, tenant_id=tenant_id,
                    event_type=EventType.CANCELLED,
                    actor=EventActor.WORKER,
                    from_status=JobStatus.CANCELLING,
                    to_status=JobStatus.CANCELLED,
                    attempt_id=attempt_id,
                    detail={"evidence": "cooperative_ack",
                            **(detail or {})})
            return applied

    def mark_execution_lost(
            self, *, tenant_id: str, job_id: str, attempt_id: str,
            reason: str, to_manual_review: bool = False,
            redispatch: bool = False,
    ) -> Optional[JobRecord]:
        """R7/R8: classification of an execution with no terminal
        evidence.  ``redispatch`` (restart-safe types only, decided by
        the service) returns the job to ``pending`` for re-dispatch;
        ``to_manual_review`` parks it for the operator; otherwise the
        attempt is ``lost`` and the job stays for evidence."""
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "UPDATE jobs.job_attempts SET status = 'lost', "
                "finished_at = now(), updated_at = now() "
                "WHERE id = %s AND tenant_id = %s AND status = 'running'",
                (attempt_id, tenant_id))
            if cur.rowcount != 1:
                return None
            self._record_event(
                cur, job_id=job_id, tenant_id=tenant_id,
                event_type=EventType.ATTEMPT_LOST,
                actor=EventActor.SYSTEM,
                from_status=None, to_status=None,
                attempt_id=attempt_id,
                detail={"reason": reason,
                        "redispatch": bool(redispatch),
                        "manual_review": bool(to_manual_review)})
            if redispatch:
                cur.execute(
                    "UPDATE jobs.jobs SET status = 'pending', "
                    "updated_at = now() WHERE id = %s AND status = "
                    "'running'", (job_id,))
            elif to_manual_review:
                cur.execute(
                    "UPDATE jobs.jobs SET status = 'manual_review', "
                    "finished_at = NULL, updated_at = now() "
                    "WHERE id = %s AND status IN ('running', "
                    "'cancelling')", (job_id,))
            cur.execute(
                "SELECT * FROM jobs.jobs WHERE id = %s", (job_id,))
            return _job_from_row(cur.fetchone())

    # -- cancellation (T2/T11/T12/T13) ----------------------------------------

    def request_cancel(self, *, tenant_id: str, job_id: str,
                       actor: EventActor = EventActor.API,
                       revoke_requested: bool = False,
    ) -> CancelDecision:
        """Durable cancellation intent (Spec 025 §3.3).  Celery revoke
        is a best-effort transport aid recorded as bounded detail; it
        NEVER guarantees interruption of running work."""
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "SELECT * FROM jobs.jobs WHERE id = %s FOR UPDATE",
                (job_id,))
            row = cur.fetchone()
            if row is None:
                raise JobNotFoundError(
                    "job not found for this tenant context")
            job = _job_from_row(row)
            if job.status in TERMINAL_JOB_STATUSES:
                return CancelDecision(
                    outcome=CancellationOutcome.ALREADY_TERMINAL,
                    job=job)
            if job.status == JobStatus.MANUAL_REVIEW:
                return CancelDecision(
                    outcome=CancellationOutcome.NOT_CANCELLABLE,
                    job=job)
            if job.status == JobStatus.PENDING:
                cur.execute(
                    "UPDATE jobs.jobs SET status = 'cancelled', "
                    "cancelled_at = now(), finished_at = now(), "
                    "cancel_requested_at = now(), purge_after = now() + "
                    + _retention_interval_sql("cancelled")
                    + ", updated_at = now() WHERE id = %s",
                    (job_id,))
                self._record_event(
                    cur, job_id=job_id, tenant_id=tenant_id,
                    event_type=EventType.CANCEL_REQUESTED,
                    actor=actor, from_status=job.status,
                    to_status=JobStatus.CANCELLED,
                    detail={"revoke_requested": bool(revoke_requested)})
                self._record_event(
                    cur, job_id=job_id, tenant_id=tenant_id,
                    event_type=EventType.CANCELLED,
                    actor=actor, from_status=JobStatus.PENDING,
                    to_status=JobStatus.CANCELLED,
                    detail={"evidence": "nothing_published"})
                cur.execute(
                    "SELECT * FROM jobs.jobs WHERE id = %s", (job_id,))
                return CancelDecision(
                    outcome=CancellationOutcome.CANCELLED,
                    job=_job_from_row(cur.fetchone()))
            # dispatching / queued / dispatch_unknown / retry_wait /
            # running → cancelling (T11/T12/T13).
            cur.execute(
                "UPDATE jobs.jobs SET status = 'cancelling', "
                "cancel_requested_at = now(), updated_at = now() "
                "WHERE id = %s AND status IN ('dispatching', 'queued', "
                "'dispatch_unknown', 'retry_wait', 'running')",
                (job_id,))
            if cur.rowcount != 1:
                # Lost a race with a terminal transition.
                cur.execute(
                    "SELECT * FROM jobs.jobs WHERE id = %s", (job_id,))
                job = _job_from_row(cur.fetchone())
                return CancelDecision(
                    outcome=CancellationOutcome.ALREADY_TERMINAL
                    if job.status in TERMINAL_JOB_STATUSES
                    else CancellationOutcome.REQUESTED,
                    job=job)
            self._record_event(
                cur, job_id=job_id, tenant_id=tenant_id,
                event_type=EventType.CANCEL_REQUESTED,
                actor=actor, from_status=job.status,
                to_status=JobStatus.CANCELLING,
                detail={"revoke_requested": bool(revoke_requested)})
            cur.execute(
                "SELECT * FROM jobs.jobs WHERE id = %s", (job_id,))
            return CancelDecision(
                outcome=CancellationOutcome.REQUESTED,
                job=_job_from_row(cur.fetchone()))

    def cancel_unclaimed_attempt(
            self, *, tenant_id: str, job_id: str, attempt_id: str,
            evidence: str = "never_executed",
    ) -> bool:
        """R3/R5: a cancelling job whose attempt never executed →
        attempt ``cancelled`` + job ``cancelled`` (never-executed
        evidence)."""
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "UPDATE jobs.job_attempts SET status = 'cancelled', "
                "finished_at = now(), updated_at = now() "
                "WHERE id = %s AND tenant_id = %s AND status = 'queued'",
                (attempt_id, tenant_id))
            if cur.rowcount != 1:
                return False
            cur.execute(
                "UPDATE jobs.jobs SET status = 'cancelled', "
                "cancelled_at = COALESCE(cancelled_at, now()), "
                "finished_at = COALESCE(finished_at, now()), "
                "purge_after = now() + "
                + _retention_interval_sql("cancelled")
                + ", updated_at = now() WHERE id = %s AND status = "
                "'cancelling'", (job_id,))
            applied = cur.rowcount == 1
            if applied:
                self._record_event(
                    cur, job_id=job_id, tenant_id=tenant_id,
                    event_type=EventType.CANCELLED,
                    actor=EventActor.SYSTEM,
                    from_status=JobStatus.CANCELLING,
                    to_status=JobStatus.CANCELLED,
                    attempt_id=attempt_id,
                    detail={"evidence": evidence})
            return applied

    # -- reads ------------------------------------------------------------------

    def get_job(self, *, tenant_id: str, job_id: str) -> JobRecord:
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "SELECT * FROM jobs.jobs WHERE id = %s AND tenant_id = %s",
                (job_id, tenant_id))
            row = cur.fetchone()
            if row is None:
                raise JobNotFoundError(
                    "job not found for this tenant context")
            return _job_from_row(row)

    def get_job_unscoped(self, *, job_id: str,
                         privileged: bool = False) -> Optional[JobRecord]:
        """Operator-context read (privileged: no tenant filter)."""
        with self._transaction(privileged=privileged) as cur:
            cur.execute("SELECT * FROM jobs.jobs WHERE id = %s",
                        (job_id,))
            row = cur.fetchone()
            return _job_from_row(row) if row else None

    def get_job_by_subject(self, *, tenant_id: str, job_type: str,
                           subject_id: str) -> Optional[JobRecord]:
        """Tenant-scoped lookup by durable subject (Spec 026: the
        artifact API resolves ``artifact:<id>`` through the
        server-generated subject id).  Parameterized; most recent
        matching job wins; None when unknown to this tenant."""
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "SELECT * FROM jobs.jobs WHERE tenant_id = %s "
                "AND job_type = %s AND subject_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (tenant_id, job_type, subject_id))
            row = cur.fetchone()
            return _job_from_row(row) if row else None

    def is_cancel_requested(self, *, tenant_id: str, job_id: str) -> bool:
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "SELECT cancel_requested_at FROM jobs.jobs WHERE id = %s "
                "AND tenant_id = %s", (job_id, tenant_id))
            row = cur.fetchone()
            return bool(row and row["cancel_requested_at"] is not None)

    def list_jobs(
            self, *, tenant_id: str, status: Optional[str] = None,
            job_type: Optional[str] = None, limit: int = 50,
            offset: int = 0,
    ) -> List[JobRecord]:
        tenant_id = validate_tenant_id(tenant_id)
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        clauses = ["tenant_id = %s"]
        params: List[Any] = [tenant_id]
        if status:
            clauses.append("status = %s")
            params.append(status)
        if job_type:
            clauses.append("job_type = %s")
            params.append(job_type)
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "SELECT * FROM jobs.jobs WHERE " + " AND ".join(clauses)
                + " ORDER BY submitted_at DESC LIMIT %s OFFSET %s",
                (*params, limit, offset))
            return [_job_from_row(r) for r in cur.fetchall()]

    def get_attempt(self, *, tenant_id: str, attempt_id: str
                    ) -> Optional[AttemptRecord]:
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "SELECT * FROM jobs.job_attempts WHERE id = %s "
                "AND tenant_id = %s", (attempt_id, tenant_id))
            row = cur.fetchone()
            return _attempt_from_row(row) if row else None

    def get_latest_attempt(self, *, tenant_id: str, job_id: str
                           ) -> Optional[AttemptRecord]:
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "SELECT * FROM jobs.job_attempts WHERE job_id = %s "
                "AND tenant_id = %s ORDER BY attempt_no DESC LIMIT 1",
                (job_id, tenant_id))
            row = cur.fetchone()
            return _attempt_from_row(row) if row else None

    def events_for(self, *, tenant_id: str, job_id: str,
                   limit: int = 100) -> List[EventRecord]:
        tenant_id = validate_tenant_id(tenant_id)
        limit = max(1, min(int(limit), 500))
        with self._transaction(tenant_id) as cur:
            cur.execute(
                "SELECT * FROM jobs.job_events WHERE job_id = %s "
                "AND tenant_id = %s ORDER BY seq LIMIT %s",
                (job_id, tenant_id, limit))
            rows = cur.fetchall()
        events = []
        for row in rows:
            events.append(EventRecord(
                id=row["id"], job_id=row["job_id"],
                tenant_id=row["tenant_id"],
                event_type=EventType(row["event_type"]),
                actor=EventActor(row["actor"]),
                from_status=(JobStatus(row["from_status"])
                             if row["from_status"] else None),
                to_status=(JobStatus(row["to_status"])
                           if row["to_status"] else None),
                attempt_id=row["attempt_id"],
                detail=row["detail"] or {},
                created_at=row["created_at"], seq=int(row["seq"])))
        return events

    def record_anomaly(self, *, tenant_id: str, job_id: str,
                       detail: Dict[str, Any]) -> None:
        """Bounded anomaly evidence (at most once per classification
        decision; the service de-duplicates per attempt)."""
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            self._record_event(
                cur, job_id=job_id, tenant_id=tenant_id,
                event_type=EventType.ANOMALY, actor=EventActor.SYSTEM,
                from_status=None, to_status=None,
                detail=detail)

    def record_reconciliation(
            self, *, tenant_id: str, job_id: str, classification: str,
            action: str, detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        tenant_id = validate_tenant_id(tenant_id)
        with self._transaction(tenant_id) as cur:
            self._record_event(
                cur, job_id=job_id, tenant_id=tenant_id,
                event_type=EventType.RECONCILED,
                actor=EventActor.SYSTEM, from_status=None,
                to_status=None,
                detail={"classification": classification,
                        "action": action, **(detail or {})})

    # -- reconciliation selectors (batched, R1–R9) ------------------------------

    def _select_batch(self, sql: str, params: tuple,
                      tenant_id: Optional[str],
                      privileged: bool) -> List[JobRecord]:
        with self._transaction(
                tenant_id=tenant_id, privileged=privileged) as cur:
            cur.execute(sql, params)
            return [_job_from_row(r) for r in cur.fetchall()]

    def stale_pending(self, *, tenant_id: Optional[str],
                      threshold: datetime, batch: int,
                      privileged: bool = False) -> List[JobRecord]:
        sql = (
            "SELECT * FROM jobs.jobs WHERE status = 'pending' "
            "AND updated_at < %s AND cancel_requested_at IS NULL "
            "ORDER BY updated_at LIMIT %s")
        params: Tuple = (threshold, int(batch))
        if tenant_id is not None:
            sql = sql.replace("AND updated_at",
                              "AND tenant_id = %s AND updated_at")
            params = (tenant_id, threshold, int(batch))
        return self._select_batch(sql, params, tenant_id, privileged)

    def stale_dispatching(self, *, tenant_id: Optional[str],
                          threshold: datetime, batch: int,
                          privileged: bool = False) -> List[JobRecord]:
        sql = (
            "SELECT j.* FROM jobs.jobs j WHERE j.status = 'dispatching' "
            "AND j.updated_at < %s "
            "AND EXISTS (SELECT 1 FROM jobs.job_attempts a WHERE "
            "a.job_id = j.id AND a.publication_state IN ('prepared', "
            "'publishing')) "
            "ORDER BY j.updated_at LIMIT %s")
        params: Tuple = (threshold, int(batch))
        if tenant_id is not None:
            sql = sql.replace("AND j.updated_at",
                              "AND j.tenant_id = %s AND j.updated_at")
            params = (tenant_id, threshold, int(batch))
        return self._select_batch(sql, params, tenant_id, privileged)

    def dispatch_unknown_jobs(self, *, tenant_id: Optional[str],
                              batch: int,
                              privileged: bool = False) -> List[JobRecord]:
        sql = (
            "SELECT * FROM jobs.jobs WHERE status = 'dispatch_unknown' "
            + ("AND tenant_id = %s " if tenant_id is not None else "")
            + "ORDER BY updated_at LIMIT %s")
        params: Tuple = ((tenant_id, int(batch)) if tenant_id is not None
                         else (int(batch),))
        return self._select_batch(sql, params, tenant_id, privileged)

    def stale_queued(self, *, tenant_id: Optional[str],
                     threshold: datetime, batch: int,
                     privileged: bool = False) -> List[JobRecord]:
        sql = (
            "SELECT j.* FROM jobs.jobs j WHERE j.status = 'queued' "
            "AND j.updated_at < %s AND j.cancel_requested_at IS NULL "
            "ORDER BY j.updated_at LIMIT %s")
        params: Tuple = (threshold, int(batch))
        if tenant_id is not None:
            sql = sql.replace("AND j.updated_at",
                              "AND j.tenant_id = %s AND j.updated_at")
            params = (tenant_id, threshold, int(batch))
        return self._select_batch(sql, params, tenant_id, privileged)

    def due_retry_wait(self, *, tenant_id: Optional[str], batch: int,
                       privileged: bool = False) -> List[JobRecord]:
        sql = (
            "SELECT * FROM jobs.jobs WHERE status = 'retry_wait' "
            "AND scheduled_at <= now() "
            "AND cancel_requested_at IS NULL ORDER BY scheduled_at "
            "LIMIT %s")
        params: Tuple = (int(batch),)
        if tenant_id is not None:
            sql = sql.replace("AND scheduled_at",
                              "AND tenant_id = %s AND scheduled_at")
            params = (tenant_id, int(batch))
        return self._select_batch(sql, params, tenant_id, privileged)

    def stale_running(self, *, tenant_id: Optional[str],
                      threshold: datetime, batch: int,
                      privileged: bool = False) -> List[JobRecord]:
        sql = (
            "SELECT j.* FROM jobs.jobs j WHERE j.status = 'running' "
            "AND j.updated_at < %s "
            "ORDER BY j.updated_at LIMIT %s")
        params: Tuple = (threshold, int(batch))
        if tenant_id is not None:
            sql = sql.replace("AND j.updated_at",
                              "AND j.tenant_id = %s AND j.updated_at")
            params = (tenant_id, threshold, int(batch))
        return self._select_batch(sql, params, tenant_id, privileged)

    def cancelling_jobs(self, *, tenant_id: Optional[str], batch: int,
                        privileged: bool = False) -> List[JobRecord]:
        sql = (
            "SELECT * FROM jobs.jobs WHERE status = 'cancelling' "
            + ("AND tenant_id = %s " if tenant_id is not None else "")
            + "ORDER BY updated_at LIMIT %s")
        params: Tuple = ((tenant_id, int(batch)) if tenant_id is not None
                         else (int(batch),))
        return self._select_batch(sql, params, tenant_id, privileged)

    def divergent_jobs(self, *, tenant_id: Optional[str], batch: int,
                       privileged: bool = False) -> List[JobRecord]:
        """R9: attempts terminal while the job is not (or vice versa)."""
        sql = (
            "SELECT j.* FROM jobs.jobs j WHERE j.status NOT IN "
            "('succeeded', 'failed', 'cancelled') AND EXISTS ("
            "SELECT 1 FROM jobs.job_attempts a WHERE a.job_id = j.id "
            "AND a.status IN ('succeeded', 'failed', 'lost', "
            "'cancelled', 'dispatch_failed')) "
            "ORDER BY j.updated_at LIMIT %s")
        params: Tuple = (int(batch),)
        if tenant_id is not None:
            sql = sql.replace("AND EXISTS",
                              "AND j.tenant_id = %s AND EXISTS")
            params = (tenant_id, int(batch))
        return self._select_batch(sql, params, tenant_id, privileged)

    def attempts_for_job(self, *, tenant_id: Optional[str], job_id: str,
                         privileged: bool = False) -> List[AttemptRecord]:
        with self._transaction(
                tenant_id=tenant_id, privileged=privileged) as cur:
            cur.execute(
                "SELECT * FROM jobs.job_attempts WHERE job_id = %s "
                "ORDER BY attempt_no", (job_id,))
            return [_attempt_from_row(r) for r in cur.fetchall()]

    def count_jobs_by_status(self, *, tenant_id: Optional[str],
                             privileged: bool = False) -> Dict[str, int]:
        sql = ("SELECT status, count(*) FROM jobs.jobs "
               + ("WHERE tenant_id = %s " if tenant_id is not None else "")
               + "GROUP BY status")
        params: Tuple = ((tenant_id,) if tenant_id is not None else ())
        with self._transaction(
                tenant_id=tenant_id, privileged=privileged) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return {
            (row["status"] if isinstance(row, dict) else row[0]):
            int(row["count"] if isinstance(row, dict) else row[1])
            for row in rows}

    def adopt_divergence_outcome(
            self, *, tenant_id: str, job_id: str, resolution: str,
            reason: str,
    ) -> Optional[JobRecord]:
        """R9: adopt the durable attempt outcome as the job outcome
        (evidence-based; guarded on the job still being
        non-terminal)."""
        tenant_id = validate_tenant_id(tenant_id)
        if resolution not in ("succeeded", "failed", "cancelled",
                              "pending", "manual_review"):
            raise JobsError(
                f"unknown divergence resolution {resolution!r}")
        with self._transaction(tenant_id) as cur:
            if resolution == "pending":
                cur.execute(
                    "UPDATE jobs.jobs SET status = 'pending', "
                    "updated_at = now() WHERE id = %s AND status NOT IN "
                    "('succeeded', 'failed', 'cancelled')", (job_id,))
                applied = cur.rowcount == 1
            elif resolution == "manual_review":
                # R9 conservative parking: non-terminal, operator-owned;
                # NOT a finished_at terminal state, never purged by age.
                cur.execute(
                    "UPDATE jobs.jobs SET status = 'manual_review', "
                    "finished_at = NULL, updated_at = now() WHERE id = %s "
                    "AND status NOT IN ('succeeded', 'failed', "
                    "'cancelled')", (job_id,))
                applied = cur.rowcount == 1
            else:
                cur.execute(
                    "UPDATE jobs.jobs SET status = %s, "
                    "finished_at = COALESCE(finished_at, now()), "
                    "cancelled_at = CASE WHEN %s = 'cancelled' THEN "
                    "COALESCE(cancelled_at, now()) ELSE cancelled_at "
                    "END, purge_after = now() + "
                    + _retention_interval_sql(resolution)
                    + ", updated_at = now() WHERE id = %s AND status "
                    "NOT IN ('succeeded', 'failed', 'cancelled')",
                    (resolution, resolution, job_id))
                applied = cur.rowcount == 1
            if not applied:
                return None
            self._record_event(
                cur, job_id=job_id, tenant_id=tenant_id,
                event_type=EventType.RECONCILED,
                actor=EventActor.SYSTEM, from_status=None,
                to_status=(JobStatus(resolution)
                           if resolution != "pending"
                           else JobStatus.PENDING),
                detail={"reason": reason, "classification": "R9"})
            cur.execute("SELECT * FROM jobs.jobs WHERE id = %s",
                        (job_id,))
            return _job_from_row(cur.fetchone())

    # -- privileged retention purge (cleanup; migrator connection) --------------

    def purge_expired_jobs(self, *, batch: int,
                           now: Optional[datetime] = None) -> int:
        """Delete jobs whose ``purge_after`` has passed (attempts and
        events cascade with their owning job).  Runs on the MIGRATOR
        connection with the controlled cleanup GUC; bounded by batch;
        active and non-terminal jobs never have ``purge_after`` set.
        Returns the number of jobs deleted."""
        batch = max(1, int(batch))
        with self._transaction(privileged=True) as cur:
            cur.execute(
                "WITH victims AS ("
                "  SELECT id FROM jobs.jobs "
                "  WHERE purge_after IS NOT NULL AND purge_after <= %s "
                "  ORDER BY purge_after LIMIT %s"
                ") DELETE FROM jobs.jobs j USING victims v "
                "WHERE j.id = v.id RETURNING j.id",
                ((now or _now()), batch))
            deleted = len(cur.fetchall())
        return deleted

    def count_purgeable(self, *, now: Optional[datetime] = None) -> int:
        with self._transaction(privileged=True) as cur:
            cur.execute(
                "SELECT count(*) AS n FROM jobs.jobs WHERE purge_after "
                "IS NOT NULL AND purge_after <= %s", ((now or _now()),))
            return int(cur.fetchone()["n"])


# Terminal attempt statuses as raw values (for SQL-side membership).
TERMINAL_ATTEMPT_STATUSES_SET = TERMINAL_ATTEMPT_STATUSES = frozenset({
    AttemptStatus.SUCCEEDED, AttemptStatus.FAILED,
    AttemptStatus.LOST, AttemptStatus.CANCELLED,
    AttemptStatus.DISPATCH_FAILED,
})
