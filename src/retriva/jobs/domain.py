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

"""Durable job domain model: states, transitions, records, sanitized
errors (Spec 025 §3.1–§3.2; ADR-030 Decision 4–7).

The transition table here is the single source of truth for what is
legal; the repository enforces every transition with a predicate-
guarded atomic UPDATE and writes one event per applied transition.
Terminal states are immutable.  Late or duplicate callbacks and
duplicate dispatches are idempotent no-ops, never state regressions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple

from retriva.jobs.errors import (
    IdempotencyConflictError,
    InvalidTransitionError,
)


class JobStatus(str, Enum):
    PENDING = "pending"
    DISPATCHING = "dispatching"
    QUEUED = "queued"
    DISPATCH_UNKNOWN = "dispatch_unknown"
    RETRY_WAIT = "retry_wait"
    RUNNING = "running"
    CANCELLING = "cancelling"
    MANUAL_REVIEW = "manual_review"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Terminal states: nothing may leave them (Spec 025 §3.2 rules).
TERMINAL_JOB_STATUSES: frozenset = frozenset({
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
})

#: Non-terminal but excluded from automatic resolution and age-based
#: cleanup; only the operator exits it (owner decision, revision 2).
MANUAL_REVIEW_EXCLUDED = frozenset({JobStatus.MANUAL_REVIEW})


class AttemptStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    LOST = "lost"
    CANCELLED = "cancelled"
    DISPATCH_FAILED = "dispatch_failed"


TERMINAL_ATTEMPT_STATUSES: frozenset = frozenset({
    AttemptStatus.SUCCEEDED,
    AttemptStatus.FAILED,
    AttemptStatus.LOST,
    AttemptStatus.CANCELLED,
    AttemptStatus.DISPATCH_FAILED,
})


class PublicationState(str, Enum):
    """Attempt-level publication state (Spec 025 §3.4).

    ``prepared``   publication definitely not attempted;
    ``publishing`` publish call in flight (job status: dispatching);
    ``published``  publication confirmed (job status: queued);
    ``unknown``    publication outcome ambiguous (job status:
                   dispatch_unknown); never blindly reverted;
    ``rejected``   definite rejection before broker acceptance (job
                   returns to pending; the generation is abandoned).
    """

    PREPARED = "prepared"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    UNKNOWN = "unknown"
    REJECTED = "rejected"


class ExecutionTransport(str, Enum):
    CELERY = "celery"
    LOCAL = "local"


class RetryClass(str, Enum):
    RETRYABLE = "retryable"
    NON_RETRYABLE = "non_retryable"
    OOM_REQUEUE = "oom_requeue"
    OPERATOR_OVERRIDE = "operator_override"
    NONE = "none"


class EventActor(str, Enum):
    SYSTEM = "system"
    API = "api"
    WORKER = "worker"
    OPERATOR = "operator"


class EventType(str, Enum):
    """Bounded event vocabulary (Spec 025 §3.14; CHECK-enforced)."""

    JOB_CREATED = "job_created"
    DISPATCH_PREPARED = "dispatch_prepared"
    DISPATCH_CONFIRMED = "dispatch_confirmed"
    DISPATCH_REJECTED = "dispatch_rejected"
    DISPATCH_AMBIGUOUS = "dispatch_ambiguous"
    DISPATCH_REPUBLISHED = "dispatch_republished"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    ATTEMPT_CLAIMED = "attempt_claimed"
    ATTEMPT_SUCCEEDED = "attempt_succeeded"
    ATTEMPT_FAILED = "attempt_failed"
    ATTEMPT_LOST = "attempt_lost"
    RETRY_SCHEDULED = "retry_scheduled"
    RETRY_DISPATCHED = "retry_dispatched"
    OPERATOR_RETRY = "operator_retry"
    OPERATOR_RESOLUTION = "operator_resolution"
    RECONCILED = "reconciled"
    ANOMALY = "anomaly"


#: Bounded error-code vocabulary (safe, content-free).  Handlers map
#: their exceptions into these codes; the default is
#: ``unclassified_execution_error``.  No raw exception text is ever
#: persisted (Constitution §33/§34).
ErrorCode = str

ERROR_CODE_UNCLASSIFIED = "unclassified_execution_error"
ERROR_CODE_CANCELLED = "execution_cancelled"
ERROR_CODE_TIMEOUT = "execution_timeout"
ERROR_CODE_REDIS_OOM = "worker_oom_requeue"
ERROR_CODE_TASK_NOT_FOUND = "task_not_registered"
ERROR_CODE_INVALID_PAYLOAD = "invalid_job_payload"


#: Summary bound (characters) for every persisted error/summary field.
ERROR_SUMMARY_MAX_CHARS = 200


def sanitize_error_summary(raw: object) -> str:
    """Bounded, sanitized summary text: no newlines, no control
    characters, hard length cap.  Callers pass deliberately safe
    phrases (exception class names, stage context) — never raw
    exception serialization."""
    text = str(raw or "")
    cleaned = " ".join(text.split())
    return cleaned[:ERROR_SUMMARY_MAX_CHARS]


def error_code_for_exception(exc: Exception) -> str:
    """Map an exception to a bounded, content-free error code."""
    name = exc.__class__.__name__
    if name == "CancellationError":
        return ERROR_CODE_CANCELLED
    if isinstance(exc, TimeoutError):
        return ERROR_CODE_TIMEOUT
    if name in ("MemoryError",):
        return ERROR_CODE_REDIS_OOM
    return ERROR_CODE_UNCLASSIFIED


# ---------------------------------------------------------------------------
# Transition table (Spec 025 §3.2)
# ---------------------------------------------------------------------------

#: From each status: the statuses a transition may legally target.
#: Terminal states target nothing.  Specific predicates (cancel intent,
#: attempt evidence, retry limits, operator authorization) are enforced
#: by the repository/service layer on top of this legality table.
ALLOWED_JOB_TRANSITIONS: Mapping[JobStatus, frozenset] = {
    JobStatus.PENDING: frozenset({
        JobStatus.DISPATCHING,
        JobStatus.CANCELLED,      # T2 (nothing published)
    }),
    JobStatus.DISPATCHING: frozenset({
        JobStatus.QUEUED,          # T4 confirmed
        JobStatus.PENDING,         # T5 definite pre-acceptance rejection
        JobStatus.DISPATCH_UNKNOWN,  # T6 ambiguous
        JobStatus.CANCELLING,      # T13
    }),
    JobStatus.QUEUED: frozenset({
        JobStatus.RUNNING,         # T10 claim
        JobStatus.CANCELLING,      # T11
        # T7: dispatch_unknown -> queued is recorded as evidence
        # resolution BEFORE the claim; the queued->running claim is
        # the same claim transition.
    }),
    JobStatus.DISPATCH_UNKNOWN: frozenset({
        JobStatus.QUEUED,          # T7/T8 evidence or republication
        JobStatus.MANUAL_REVIEW,   # T9
        JobStatus.CANCELLING,      # T13
    }),
    JobStatus.RETRY_WAIT: frozenset({
        JobStatus.QUEUED,          # T20 reschedule
        JobStatus.CANCELLING,      # T13
    }),
    JobStatus.RUNNING: frozenset({
        JobStatus.SUCCEEDED,       # T17
        JobStatus.FAILED,          # T18
        JobStatus.RETRY_WAIT,      # T19
        JobStatus.CANCELLING,      # T12
    }),
    JobStatus.CANCELLING: frozenset({
        JobStatus.CANCELLED,       # T14
        JobStatus.SUCCEEDED,       # T15 (cancel lost the race)
        JobStatus.FAILED,          # T16
        JobStatus.MANUAL_REVIEW,   # uncertain fate (R4/R5)
    }),
    JobStatus.MANUAL_REVIEW: frozenset({
        JobStatus.SUCCEEDED,       # T22 operator resolution
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.QUEUED,          # operator-approved re-dispatch
    }),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset({
        JobStatus.QUEUED,          # T21 operator retry ONLY (CLI)
    }),
    JobStatus.CANCELLED: frozenset(),
}


def assert_transition_allowed(current: JobStatus, target: JobStatus) -> None:
    """Raise :class:`InvalidTransitionError` when the transition is not
    in the legality table."""
    if current not in ALLOWED_JOB_TRANSITIONS:
        raise InvalidTransitionError(
            f"unknown job status {current!r}")
    allowed = ALLOWED_JOB_TRANSITIONS[current]
    if target not in allowed:
        raise InvalidTransitionError(
            f"transition {current.value} -> {target.value} is not "
            "allowed by the durable state machine")


#: Attempt-status legality (attempt rows carry their own state).
ALLOWED_ATTEMPT_TRANSITIONS: Mapping[AttemptStatus, frozenset] = {
    AttemptStatus.QUEUED: frozenset({
        AttemptStatus.RUNNING,          # claim
        AttemptStatus.CANCELLED,        # claim refusal (never executed)
        AttemptStatus.DISPATCH_FAILED,  # definite pre-acceptance rejection
        AttemptStatus.LOST,             # reconciliation: publication lost
    }),
    AttemptStatus.RUNNING: frozenset({
        AttemptStatus.SUCCEEDED,
        AttemptStatus.FAILED,
        AttemptStatus.LOST,
        AttemptStatus.CANCELLED,
    }),
    AttemptStatus.SUCCEEDED: frozenset(),
    AttemptStatus.FAILED: frozenset(),
    AttemptStatus.LOST: frozenset(),
    AttemptStatus.CANCELLED: frozenset(),
    AttemptStatus.DISPATCH_FAILED: frozenset(),
}


def assert_attempt_transition_allowed(
        current: AttemptStatus, target: AttemptStatus) -> None:
    if current not in ALLOWED_ATTEMPT_TRANSITIONS:
        raise InvalidTransitionError(
            f"unknown attempt status {current!r}")
    if target not in ALLOWED_ATTEMPT_TRANSITIONS[current]:
        raise InvalidTransitionError(
            f"attempt transition {current.value} -> {target.value} is "
            "not allowed by the durable state machine")


# ---------------------------------------------------------------------------
# Records (bounded, repository-shaped)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JobRecord:
    """One logical job row (jobs.jobs)."""

    id: str
    tenant_id: str
    job_type: str
    payload_version: str
    status: JobStatus
    execution_transport: ExecutionTransport
    subject_type: Optional[str] = None
    subject_id: Optional[str] = None
    input_metadata: Dict[str, Any] = field(default_factory=dict)
    result_metadata: Optional[Dict[str, Any]] = None
    progress: Optional[int] = None
    progress_stage: Optional[str] = None
    progress_message: Optional[str] = None
    idempotency_key: Optional[str] = None
    requested_by: Optional[str] = None
    queue: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    cancel_requested_at: Optional[datetime] = None
    attempt_count: int = 0
    max_attempts: int = 3
    last_error_code: Optional[str] = None
    last_error_summary: Optional[str] = None
    celery_task_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    purge_after: Optional[datetime] = None


@dataclass(frozen=True)
class AttemptRecord:
    """One durable execution attempt (jobs.job_attempts).

    ``celery_task_id`` is PREALLOCATED at attempt creation and reused
    for every publication try of the same dispatch generation
    (Spec 025 §3.4); task-id reuse provides correlation only —
    duplicate-execution protection is the atomic worker-side claim.
    """

    id: str
    job_id: str
    tenant_id: str
    attempt_no: int
    dispatch_generation: int
    dispatch_token: str
    celery_task_id: Optional[str]
    publication_state: PublicationState
    status: AttemptStatus
    published_at: Optional[datetime] = None
    publication_tries: int = 0
    execution_generation: int = 1
    worker_id: Optional[str] = None
    dispatched_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    retry_class: Optional[RetryClass] = None
    error_code: Optional[str] = None
    error_summary: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class EventRecord:
    """One append-only transition event (jobs.job_events)."""

    id: str
    job_id: str
    tenant_id: str
    event_type: EventType
    actor: EventActor
    from_status: Optional[JobStatus] = None
    to_status: Optional[JobStatus] = None
    attempt_id: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None
    seq: Optional[int] = None


@dataclass(frozen=True)
class SanitizedError:
    """Bounded failure description persisted on rows (never raw
    exception serialization; Constitution §33/§34)."""

    code: str = ERROR_CODE_UNCLASSIFIED
    summary: str = ""
