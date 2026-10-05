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

"""Domain tests for the durable job lifecycle (Spec 025 acceptance
§A): transition legality, terminal immutability, retry bounds,
cancellation-race matrix, manual_review semantics, sanitized errors,
and the publication-state classification — no database."""

from __future__ import annotations

import pytest

from retriva.jobs.dispatch import (
    PublicationOutcome,
    classify_publish_exception,
    transport_error_for,
)
from retriva.jobs.domain import (
    ALLOWED_ATTEMPT_TRANSITIONS,
    ALLOWED_JOB_TRANSITIONS,
    ERROR_CODE_UNCLASSIFIED,
    TERMINAL_ATTEMPT_STATUSES,
    TERMINAL_JOB_STATUSES,
    AttemptStatus,
    EventType,
    JobStatus,
    PublicationState,
    RetryClass,
    SanitizedError,
    assert_attempt_transition_allowed,
    assert_transition_allowed,
    error_code_for_exception,
    sanitize_error_summary,
)
from retriva.jobs.errors import (
    IdempotencyConflictError,
    InvalidTransitionError,
)
from retriva.jobs.registry import JobTypeSpec, JobTypeRegistry
from retriva.logger import get_logger


# --- transition legality ----------------------------------------------------


def test_every_status_has_a_transition_entry():
    for status in JobStatus:
        assert status in ALLOWED_JOB_TRANSITIONS


def test_terminal_states_immutable_except_operator_retry():
    """Terminal states target nothing — the ONE exception is the
    operator-only retry from ``failed`` (T21; never from the public
    API)."""
    assert ALLOWED_JOB_TRANSITIONS[JobStatus.SUCCEEDED] == frozenset()
    assert ALLOWED_JOB_TRANSITIONS[JobStatus.CANCELLED] == frozenset()
    assert ALLOWED_JOB_TRANSITIONS[JobStatus.FAILED] == frozenset(
        {JobStatus.QUEUED})


@pytest.mark.parametrize("current,target", [
    (JobStatus.PENDING, JobStatus.DISPATCHING),       # T3
    (JobStatus.PENDING, JobStatus.CANCELLED),         # T2
    (JobStatus.DISPATCHING, JobStatus.QUEUED),        # T4
    (JobStatus.DISPATCHING, JobStatus.PENDING),       # T5
    (JobStatus.DISPATCHING, JobStatus.DISPATCH_UNKNOWN),  # T6
    (JobStatus.DISPATCHING, JobStatus.CANCELLING),    # T13
    (JobStatus.DISPATCH_UNKNOWN, JobStatus.QUEUED),   # T7/T8
    (JobStatus.DISPATCH_UNKNOWN, JobStatus.MANUAL_REVIEW),  # T9
    (JobStatus.QUEUED, JobStatus.RUNNING),            # T10
    (JobStatus.QUEUED, JobStatus.CANCELLING),         # T11
    (JobStatus.RUNNING, JobStatus.SUCCEEDED),         # T17
    (JobStatus.RUNNING, JobStatus.FAILED),            # T18
    (JobStatus.RUNNING, JobStatus.RETRY_WAIT),        # T19
    (JobStatus.RUNNING, JobStatus.CANCELLING),        # T12
    (JobStatus.RETRY_WAIT, JobStatus.QUEUED),         # T20
    (JobStatus.RETRY_WAIT, JobStatus.CANCELLING),     # T13
    (JobStatus.CANCELLING, JobStatus.CANCELLED),      # T14
    (JobStatus.CANCELLING, JobStatus.SUCCEEDED),      # T15 (cancel lost race)
    (JobStatus.CANCELLING, JobStatus.FAILED),         # T16
    (JobStatus.CANCELLING, JobStatus.MANUAL_REVIEW),  # uncertain fate
    (JobStatus.FAILED, JobStatus.QUEUED),             # T21 operator retry
    (JobStatus.MANUAL_REVIEW, JobStatus.SUCCEEDED),   # T22
    (JobStatus.MANUAL_REVIEW, JobStatus.QUEUED),      # T22 re-dispatch
])
def test_allowed_transitions(current, target):
    assert_transition_allowed(current, target)


@pytest.mark.parametrize("current,target", [
    (JobStatus.PENDING, JobStatus.RUNNING),           # never claim from pending
    (JobStatus.PENDING, JobStatus.QUEUED),            # publication state model
    (JobStatus.DISPATCHING, JobStatus.RUNNING),
    (JobStatus.DISPATCH_UNKNOWN, JobStatus.PENDING),  # NEVER blind revert
    (JobStatus.DISPATCH_UNKNOWN, JobStatus.RUNNING),
    (JobStatus.QUEUED, JobStatus.PENDING),
    (JobStatus.RUNNING, JobStatus.PENDING),
    (JobStatus.RUNNING, JobStatus.QUEUED),
    (JobStatus.CANCELLING, JobStatus.RUNNING),
    (JobStatus.CANCELLING, JobStatus.QUEUED),
    (JobStatus.SUCCEEDED, JobStatus.FAILED),          # terminal immutable
    (JobStatus.SUCCEEDED, JobStatus.CANCELLED),
    (JobStatus.FAILED, JobStatus.SUCCEEDED),
    (JobStatus.CANCELLED, JobStatus.RUNNING),
    (JobStatus.CANCELLED, JobStatus.FAILED),
    (JobStatus.MANUAL_REVIEW, JobStatus.RUNNING),
    (JobStatus.MANUAL_REVIEW, JobStatus.PENDING),
])
def test_forbidden_transitions(current, target):
    with pytest.raises(InvalidTransitionError):
        assert_transition_allowed(current, target)


def test_attempt_terminal_states():
    assert AttemptStatus.SUCCEEDED in TERMINAL_ATTEMPT_STATUSES
    assert AttemptStatus.DISPATCH_FAILED in TERMINAL_ATTEMPT_STATUSES
    assert AttemptStatus.QUEUED not in TERMINAL_ATTEMPT_STATUSES
    assert AttemptStatus.RUNNING not in TERMINAL_ATTEMPT_STATUSES
    with pytest.raises(InvalidTransitionError):
        assert_attempt_transition_allowed(
            AttemptStatus.SUCCEEDED, AttemptStatus.RUNNING)
    assert_attempt_transition_allowed(
        AttemptStatus.QUEUED, AttemptStatus.RUNNING)


def test_manual_review_is_bounded_and_non_terminal():
    """manual_review: bounded, non-terminal, operator-exited only —
    never a generic error state (owner acceptance note)."""
    assert JobStatus.MANUAL_REVIEW not in TERMINAL_JOB_STATUSES
    allowed = ALLOWED_JOB_TRANSITIONS[JobStatus.MANUAL_REVIEW]
    assert allowed == frozenset({
        JobStatus.SUCCEEDED, JobStatus.FAILED,
        JobStatus.CANCELLED, JobStatus.QUEUED})
    # No automatic entry from a generic error path: only from
    # dispatch_unknown (T9), uncertain cancellation (§3.3), or
    # reconciliation classification.
    enterings = [s for s, targets in ALLOWED_JOB_TRANSITIONS.items()
                 if JobStatus.MANUAL_REVIEW in targets]
    assert set(enterings) == {
        JobStatus.DISPATCH_UNKNOWN, JobStatus.CANCELLING}


# --- publication-state classification (§3.4) --------------------------------


def test_publish_exception_classification_defaults_to_ambiguous():
    """Any unmapped exception class defaults to AMBIGUOUS (fail-safe):
    a mid-call reset must never be treated as proof of rejection."""
    assert classify_publish_exception(
        RuntimeError("x")) is PublicationOutcome.AMBIGUOUS
    assert classify_publish_exception(
        TimeoutError("t")) is PublicationOutcome.AMBIGUOUS


def test_publish_dial_refusal_is_definite():
    assert classify_publish_exception(
        ConnectionRefusedError()) is PublicationOutcome.DEFINITELY_REJECTED


def test_transport_error_is_bounded_and_content_free():
    error = transport_error_for(TimeoutError("secret payload 1234"))
    assert error.code == "execution_timeout"
    assert error.summary == "TimeoutError"
    assert "secret" not in error.summary


# --- sanitized errors --------------------------------------------------------


def test_sanitize_error_summary_bounds_and_strips():
    raw = "line1\nline2\twith\ttabs  spaces"
    summary = sanitize_error_summary(raw)
    assert "\n" not in summary and "\t" not in summary
    assert len(sanitize_error_summary("x" * 5000)) <= 200


def test_error_code_mapping_is_bounded():
    assert error_code_for_exception(
        TimeoutError()) == "execution_timeout"

    class CancellationError(Exception):
        pass

    assert error_code_for_exception(
        CancellationError()) == "execution_cancelled"
    assert error_code_for_exception(
        ValueError("x")) == ERROR_CODE_UNCLASSIFIED


def test_idempotency_conflict_is_a_distinct_error():
    assert issubclass(IdempotencyConflictError, Exception)


# --- job-type registry --------------------------------------------------------


def test_registry_is_bounded_and_deterministic():
    registry = JobTypeRegistry()
    registry.register(JobTypeSpec(
        job_type="v2_document", task_name="t1", restart_safe=True))
    with pytest.raises(Exception):
        registry.register(JobTypeSpec(job_type="v2_document",
                                      task_name="t1"))
    with pytest.raises(Exception):
        registry.register(JobTypeSpec(job_type="has space",
                                      task_name="t2"))
    spec = registry.require("v2_document")
    assert spec.restart_safe is True
    with pytest.raises(Exception):
        registry.require("unknown_type")


def test_builtin_registry_marks_ingestion_restart_safe():
    from retriva.jobs.registry import job_type_registry
    registry = job_type_registry()
    assert registry.require("v2_document").restart_safe
    assert registry.require("v2_mediawiki").restart_safe
    with pytest.raises(Exception):
        registry.require("v1_chunks")  # legacy flows: not registered


# --- event vocabulary ---------------------------------------------------------


def test_event_types_are_bounded():
    assert len(list(EventType)) == 18
    values = {e.value for e in EventType}
    assert "anomaly" in values and "reconciled" in values


def test_retry_classes_are_bounded():
    values = {r.value for r in RetryClass}
    assert values == {"retryable", "non_retryable", "oom_requeue",
                      "operator_override", "none"}


def test_publication_states_are_bounded():
    values = {p.value for p in PublicationState}
    assert values == {"prepared", "publishing", "published",
                      "unknown", "rejected"}


def _unused_logger():  # keep logger import explicit for parity
    return get_logger(__name__)
