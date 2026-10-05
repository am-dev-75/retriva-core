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

"""Publication-state dispatch (Spec 025 §3.4; ADR-030 Decision 5).

Two-phase dispatch with an explicit publication-state model.  A
publication exception or timeout is NEVER equated with proof that the
broker rejected the message: outcomes are classified as confirmed /
definitely-rejected-before-acceptance / ambiguous / not-attempted,
with every unmapped exception class defaulting to AMBIGUOUS
(fail-safe).

The Celery task id is PREALLOCATED before publishing and used for the
publish call; Celery is never waited on to allocate an opaque id.
Task-id reuse provides correlation only — NO broker-level
deduplication is claimed; duplicate-execution protection is the
durable, atomic worker-side claim plus idempotent handlers.

A transactional outbox remains a documented escalation option only
(Spec 025 §3.4) and is not introduced in this phase.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional

from retriva.jobs.config import JobsSettings
from retriva.jobs.domain import (
    ERROR_CODE_TIMEOUT,
    ERROR_CODE_UNCLASSIFIED,
    JobRecord,
    PublicationState,
    SanitizedError,
    sanitize_error_summary,
)
from retriva.jobs.registry import JobTypeRegistry
from retriva.logger import get_logger

_log = get_logger(__name__)


class PublicationOutcome(str, Enum):
    """Exactly four publication outcomes (Spec 025 §3.4).

    ``not_attempted``  the publish call never ran (crash before the
                       publish step): attempt stays ``prepared``;
    ``confirmed``      the publish call returned (broker accepted);
    ``definitely_rejected``: a mapped, bounded exception class that
                       occurs before the broker could accept;
    ``ambiguous``      timeout, connection reset mid-call, any
                       unmapped exception, or a crash window.
    """

    NOT_ATTEMPTED = "not_attempted"
    CONFIRMED = "confirmed"
    DEFINITELY_REJECTED = "definitely_rejected"
    AMBIGUOUS = "ambiguous"


#: Bounded exception classes that occur BEFORE the broker could accept
#: the message (dial-level refusal, pre-acceptance authentication
#: failure).  Every unmapped class defaults to AMBIGUOUS (fail-safe):
#: a mid-call reset must never be treated as proof of rejection.
_DEFINITE_REJECTION_CLASSES: tuple = (ConnectionRefusedError,)


def _definite_rejection_classes() -> tuple:
    """Lazily extend the bounded map with importable transport
    exceptions (redis authentication failure at connect)."""
    classes = list(_DEFINITE_REJECTION_CLASSES)
    try:
        import redis.exceptions as redis_exc
        if hasattr(redis_exc, "AuthenticationError"):
            classes.append(redis_exc.AuthenticationError)
    except Exception:  # noqa: BLE001 - optional transport
        pass
    return tuple(classes)


def classify_publish_exception(exc: Exception) -> PublicationOutcome:
    """Classify a publish-time exception (bounded map; unmapped
    classes → AMBIGUOUS)."""
    if isinstance(exc, _definite_rejection_classes()):
        return PublicationOutcome.DEFINITELY_REJECTED
    return PublicationOutcome.AMBIGUOUS


def transport_error_for(exc: Exception) -> SanitizedError:
    """Bounded, content-free transport error (no raw exception
    serialization)."""
    name = exc.__class__.__name__
    if isinstance(exc, TimeoutError):
        return SanitizedError(
            code=ERROR_CODE_TIMEOUT,
            summary=sanitize_error_summary(name))
    return SanitizedError(
        code=ERROR_CODE_UNCLASSIFIED,
        summary=sanitize_error_summary(name))


@dataclass(frozen=True)
class DeliveryEnvelope:
    """Everything the worker protocol needs, persisted BEFORE
    publishing (job/attempt identity, preallocated task id, dispatch
    token/generation, tenant, correlation metadata)."""

    job_id: str
    attempt_id: str
    tenant_id: str
    dispatch_token: str
    celery_task_id: str
    task_name: str
    job_type: str
    payload: Dict[str, Any]


class PublishResult:
    outcome: PublicationOutcome
    error: Optional[SanitizedError] = None

    def __init__(self, outcome: PublicationOutcome,
                 error: Optional[SanitizedError] = None) -> None:
        self.outcome = outcome
        self.error = error


def new_dispatch_identity() -> tuple:
    """Fresh dispatch identity: attempt id + dispatch token +
    PREALLOCATED Celery task id (uuid4, Core-generated)."""
    return (uuid.uuid4().hex, uuid.uuid4().hex, str(uuid.uuid4()))


class CeleryPublisher:
    """Publishes prepared attempts to Celery using the preallocated
    task id; classifies outcomes per §3.4."""

    def __init__(self, app_getter: Callable[[], Any],
                 registry: Optional[JobTypeRegistry] = None) -> None:
        self._app_getter = app_getter
        self._registry = registry

    def publish(self, envelope: DeliveryEnvelope,
                queue: Optional[str] = None) -> PublishResult:
        try:
            app = self._app_getter()
            if app is None:
                return PublishResult(
                    PublicationOutcome.AMBIGUOUS,
                    transport_error_for(RuntimeError(
                        "celery app unavailable")))
            task_name = envelope.task_name
            task = app.tasks.get(task_name)
            if task is None:
                # Registration order bug: fail safe as ambiguous with
                # a bounded code.
                return PublishResult(
                    PublicationOutcome.AMBIGUOUS,
                    SanitizedError(
                        code="task_not_registered",
                        summary=sanitize_error_summary(task_name)))
            kwargs = dict(envelope.payload)
            kwargs.update({
                "job_id": envelope.job_id,
                "attempt_id": envelope.attempt_id,
                "tenant_id": envelope.tenant_id,
                "dispatch_token": envelope.dispatch_token,
                "celery_task_id": envelope.celery_task_id,
            })
            options: Dict[str, Any] = {"task_id": envelope.celery_task_id}
            if queue:
                options["queue"] = queue
            task.apply_async(kwargs=kwargs, **options)
            return PublishResult(PublicationOutcome.CONFIRMED)
        except Exception as exc:  # noqa: BLE001 - classified below
            outcome = classify_publish_exception(exc)
            _log.warning(
                "publication outcome classified: outcome=%s "
                "exception=%s", outcome.value, exc.__class__.__name__)
            return PublishResult(outcome,
                                 transport_error_for(exc))


class LocalPublishResult(PublishResult):
    """Local transport: the publish call schedules the executor
    in-process (genuinely equivalent to ``queued``: accepted for
    execution, awaiting the claim)."""

    def __init__(self, runner: Optional[Callable[[], Any]]) -> None:
        super().__init__(PublicationOutcome.CONFIRMED)
        self.runner = runner


class LocalPublisher:
    """The BackgroundTasks fallback publisher on the SAME durable
    lifecycle (Spec 025 §3.10): records publication as confirmed
    (in-process schedule accepted; ``queued`` semantics are genuinely
    equivalent — the executor claims promptly through the same claim
    protocol) and returns the runner callable the route registers."""

    def __init__(self, runner_factory: Callable[
            [DeliveryEnvelope], Callable[[], Any]]) -> None:
        self._runner_factory = runner_factory

    def publish(self, envelope: DeliveryEnvelope,
                queue: Optional[str] = None) -> LocalPublishResult:
        # Local transport never fails at publish time (no broker); a
        # process crash before the executor claims is covered by
        # reconciliation (R7 with local-transport evidence).
        return LocalPublishResult(self._runner_factory(envelope))


def publication_state_for(outcome: PublicationOutcome
                          ) -> Optional[PublicationState]:
    """Map a publication outcome to the durable attempt state."""
    return {
        PublicationOutcome.CONFIRMED: PublicationState.PUBLISHED,
        PublicationOutcome.DEFINITELY_REJECTED:
            PublicationState.REJECTED,
        PublicationOutcome.AMBIGUOUS: PublicationState.UNKNOWN,
    }.get(outcome)


def job_status_after_publication(outcome: PublicationOutcome):
    """Job status after a publication outcome write (Spec 025 §3.4):
    confirmed → queued; definite rejection → pending (retryable);
    ambiguous → dispatch_unknown."""
    from retriva.jobs.domain import JobStatus
    return {
        PublicationOutcome.CONFIRMED: JobStatus.QUEUED,
        PublicationOutcome.DEFINITELY_REJECTED: JobStatus.PENDING,
        PublicationOutcome.AMBIGUOUS: JobStatus.DISPATCH_UNKNOWN,
    }.get(outcome)
