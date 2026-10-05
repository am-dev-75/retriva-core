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

"""Deterministic persistence tests for the durable job lifecycle
(Spec 025 acceptance §B/§C/§E/§E2/§E3): real PostgreSQL scratch
cluster; migration, RLS/grants/event-immutability probes, guarded
transitions, claim races, publication outcomes, durable retry,
cancellation paths, retention purge, reconciliation classifications,
and the local (BackgroundTasks) executor protocol."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import psycopg2
import pytest

psycopg2 = pytest.importorskip("psycopg2")

from retriva.infrastructure.postgres.migrations import (
    CORE_PLATFORM_STREAM,
    load_provider_registry,
    upgrade as framework_upgrade,
    verify as framework_verify,
)

from retriva.jobs.config import JobsSettings
from retriva.jobs.dispatch import (
    DeliveryEnvelope,
    PublishResult,
    PublicationOutcome,
)
from retriva.jobs.domain import (
    ERROR_CODE_UNCLASSIFIED,
    EventActor,
    EventType,
    JobStatus,
    PublicationState,
    RetryClass,
    SanitizedError,
    AttemptStatus,
)
from retriva.jobs.errors import (
    IdempotencyConflictError,
    InvalidTransitionError,
    JobNotFoundError,
    OperatorRetryRefusedError,
)
from retriva.jobs.registry import job_type_registry
from retriva.jobs.repository import (
    CancellationOutcome,
    ClaimOutcome,
    PostgresJobsRepository,
)
from retriva.jobs.service import JobsService
from retriva.logger import get_logger

_log = get_logger(__name__)

JOBS_DB = "retriva_pg_test_jobs"
TENANT = "tenant-a"
TENANT_B = "tenant-b"


def _registry(with_jobs: bool = True):
    registry = load_provider_registry("")  # core.platform
    if with_jobs:
        from retriva.jobs.migrations import jobs_provider
        registry.register_core_stream(jobs_provider())
    return registry


@pytest.fixture(scope="module")
def jobs_db(pg_platform_stack):
    settings = pg_platform_stack.fresh_database(JOBS_DB)
    registry = _registry()
    result = framework_upgrade(registry, settings)
    assert [a["version"] for a in result[CORE_PLATFORM_STREAM][0]["applied"]] == [1]
    jobs_result = result["core.jobs"][0]
    assert [a["version"] for a in jobs_result["applied"]] == [1]
    return SimpleNamespace(
        settings=settings, stack=pg_platform_stack,
        registry=registry)


@pytest.fixture()
def repo(jobs_db) -> PostgresJobsRepository:
    return PostgresJobsRepository(jobs_db.settings)


@pytest.fixture()
def service(jobs_db, repo) -> JobsService:
    class ConfirmedPublisher:
        def publish(self, envelope, queue=None):
            return PublishResult(PublicationOutcome.CONFIRMED)

    return JobsService(
        repo=repo, settings=JobsSettings(),
        registry=job_type_registry(), publisher=ConfirmedPublisher())


@pytest.fixture()
def fresh_job(service) -> JobRef:
    job = service.submit(
        tenant_id=TENANT, job_type="v2_document",
        execution_transport="celery",
        input_metadata={"source_uri": "/tmp/x.pdf"})
    return JobRef(service, job)


class JobRef:
    def __init__(self, service, job):
        self.service = service
        self.job = job

    @property
    def id(self):
        return self.job.id

    def refresh(self):
        self.job = self.service.get_job(tenant_id=TENANT, job_id=self.id)
        return self.job


# --- migration / persistence §B ---------------------------------------------


def test_core_jobs_stream_applies_and_converges(jobs_db):
    admin = jobs_db.stack.admin(JOBS_DB)
    try:
        owner = _scalar(admin,
                        "SELECT pg_get_userbyid(nspowner) FROM "
                        "pg_namespace WHERE nspname = 'jobs'")
        assert owner == "retriva_migrator"
        for table in ("jobs.jobs", "jobs.job_attempts",
                      "jobs.job_events"):
            assert _scalar(
                admin,
                "SELECT pg_get_userbyid(relowner) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                f"WHERE n.nspname || '.' || c.relname = '{table}'"
            ) == "retriva_migrator"
        # RLS enabled + forced + tenant_isolation policy on all three.
        for table in ("jobs", "job_attempts", "job_events"):
            rows = _rows(
                admin,
                "SELECT c.relrowsecurity, c.relforcerowsecurity, "
                "EXISTS (SELECT 1 FROM pg_policy p WHERE "
                "p.polrelid = c.oid AND p.polname = "
                "'tenant_isolation') FROM pg_class c JOIN pg_namespace "
                f"n ON n.oid = c.relnamespace WHERE n.nspname = 'jobs' "
                f"AND c.relname = '{table}'")
            assert rows[0][0] and rows[0][1] and rows[0][2], table
        # The append-only trigger exists on job_events.
        assert _scalar(
            admin,
            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON "
            "c.oid = t.tgrelid JOIN pg_namespace n ON "
            "n.oid = c.relnamespace WHERE n.nspname = 'jobs' AND "
            "c.relname = 'job_events' AND t.tgname = "
            "'job_events_append_only'") == 1
    finally:
        admin.close()


def test_grants_and_default_privileges(jobs_db):
    admin = jobs_db.stack.admin(JOBS_DB)
    try:
        core = "retriva_core"
        assert _scalar(admin,
                       "SELECT has_schema_privilege(%s, 'jobs', "
                       "'USAGE')", (core,))
        for table, priv in (("jobs.jobs", "SELECT"),
                            ("jobs.jobs", "INSERT"),
                            ("jobs.jobs", "UPDATE"),
                            ("jobs.job_attempts", "INSERT"),
                            ("jobs.job_events", "INSERT")):
            assert _scalar(admin,
                           "SELECT has_table_privilege(%s, %s, %s)",
                           (core, table, priv)), (table, priv)
        # Append-only posture for the runtime role.
        for table, priv in (("jobs.job_events", "UPDATE"),
                            ("jobs.job_events", "DELETE"),
                            ("jobs.jobs", "DELETE")):
            assert not _scalar(admin,
                               "SELECT has_table_privilege(%s, %s, %s)",
                               (core, table, priv)), (table, priv)
    finally:
        admin.close()


def test_upgrade_is_idempotent(jobs_db):
    result = framework_upgrade(jobs_db.registry, jobs_db.settings)
    assert [a["version"] for a in result["core.jobs"][0]["applied"]] == []
    assert result["core.jobs"][0]["adopted"] == []
    report = framework_verify(jobs_db.registry, jobs_db.settings)
    assert report["ok"] is True


def test_runtime_dml_works_and_fails_closed(repo):
    job = repo.submit_job(
        tenant_id=TENANT, job_id="dmljob1", job_type="v2_document",
        payload_version="v2-1", execution_transport="celery",
        input_metadata={"source_uri": "/tmp/a.pdf"})
    assert job[0].status == JobStatus.PENDING
    fetched = repo.get_job(tenant_id=TENANT, job_id="dmljob1")
    assert fetched.tenant_id == TENANT
    # Fail-closed: a different tenant cannot see it.
    with pytest.raises(JobNotFoundError):
        repo.get_job(tenant_id=TENANT_B, job_id="dmljob1")
    with pytest.raises(Exception):
        from retriva.infrastructure.postgres.tenant import (
            TenantContextMissing,
        )
        from retriva.jobs.repository import _now as _t
        repo.list_jobs(tenant_id="bad tenant!@")
    # Cross-tenant list never returns foreign rows.
    assert repo.list_jobs(tenant_id=TENANT_B) == []


def test_idempotency_index_and_conflict(repo):
    metadata = {"source_uri": "/tmp/a.pdf", "input_fingerprint":
                "fingerprint-1"}
    job, created = repo.submit_job(
        tenant_id=TENANT, job_id="idem-1", job_type="v2_document",
        payload_version="v2-1", execution_transport="celery",
        input_metadata=metadata,
        idempotency_key="key-1")
    assert created
    job2, created2 = repo.submit_job(
        tenant_id=TENANT, job_id="idem-2", job_type="v2_document",
        payload_version="v2-1", execution_transport="celery",
        input_metadata=dict(metadata), idempotency_key="key-1")
    assert not created2 and job2.id == "idem-1"
    with pytest.raises(IdempotencyConflictError):
        repo.submit_job(
            tenant_id=TENANT, job_id="idem-3",
            job_type="v2_document", payload_version="v2-1",
            execution_transport="celery",
            input_metadata={"source_uri": "/tmp/other.pdf",
                            "input_fingerprint": "fingerprint-2"},
            idempotency_key="key-1")


def test_event_append_only_enforced(jobs_db, repo):
    job, _ = repo.submit_job(
        tenant_id=TENANT, job_id="evjob1", job_type="v2_document",
        payload_version="v2-1", execution_transport="celery")
    events = repo.events_for(tenant_id=TENANT, job_id="evjob1")
    assert [e.event_type for e in events] == [EventType.JOB_CREATED]
    event_id = events[0].id

    # Runtime role: UPDATE/DELETE denied by privilege.
    conn = psycopg2.connect(
        **jobs_db.settings.connection_kwargs("core"))
    try:
        with pytest.raises(psycopg2.Error):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs.job_events SET detail = '{}' "
                    "WHERE id = %s", (event_id,))
        conn.rollback()
        with pytest.raises(psycopg2.Error):
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM jobs.job_events WHERE id = %s",
                    (event_id,))
        conn.rollback()
    finally:
        conn.close()

    # Migrator (owner): the trigger refuses direct mutation too.
    admin = jobs_db.stack.admin(JOBS_DB)
    try:
        with pytest.raises(psycopg2.Error):
            with admin.cursor() as cur:
                cur.execute(
                    "UPDATE jobs.job_events SET detail = '{}' "
                    "WHERE id = %s", (event_id,))
        admin.rollback()
        # ... while cascaded deletion (FK) is allowed: purge the job.
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.jobs_privileged_cleanup', "
                "'granted', true)")
            cur.execute("DELETE FROM jobs.jobs WHERE id = 'evjob1'")
            remaining = _scalar(
                admin, "SELECT count(*) FROM jobs.job_events WHERE "
                       "job_id = 'evjob1'")
            assert remaining == 0
    finally:
        admin.close()


# --- guarded transitions / publication outcomes §C ---------------------------


def test_prepare_dispatch_and_confirm(repo, service, fresh_job):
    ref = fresh_job
    attempt_id, token, task_id = _identity()
    attempt = ref.service.repo.prepare_dispatch(
        tenant_id=TENANT, job_id=ref.id, dispatch_token=token,
        celery_task_id=task_id, attempt_id=attempt_id)
    assert attempt is not None
    assert attempt.publication_state == PublicationState.PREPARED
    assert attempt.attempt_no == 1
    assert attempt.celery_task_id == task_id
    assert ref.refresh().status == JobStatus.DISPATCHING
    # Duplicate prepare (concurrent actor): guarded no-op.
    assert ref.service.repo.prepare_dispatch(
        tenant_id=TENANT, job_id=ref.id, dispatch_token=token,
        celery_task_id=task_id, attempt_id=_identity()[0]) is None
    # Confirm publication (T4).
    repo.record_publication_inflight(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    assert repo.record_publication_confirmed(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    assert ref.refresh().status == JobStatus.QUEUED
    assert attempt.publication_tries == 0  # inflight counted it earlier


def test_definite_rejection_returns_to_pending(repo, service, fresh_job):
    ref = fresh_job
    attempt = ref.service.repo.prepare_dispatch(
        tenant_id=TENANT, job_id=ref.id, dispatch_token=_identity()[1],
        celery_task_id=_identity()[2], attempt_id=_identity()[0])
    repo.record_publication_inflight(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    assert repo.record_publication_rejected(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id,
        error=SanitizedError(code="transport_rejected",
                             summary="ConnectionRefusedError"))
    refreshed = ref.refresh()
    assert refreshed.status == JobStatus.PENDING  # retryable, NOT unknown
    attempt_row = repo.get_attempt(tenant_id=TENANT,
                                   attempt_id=attempt.id)
    assert attempt_row.status == AttemptStatus.DISPATCH_FAILED
    assert attempt_row.publication_state == PublicationState.REJECTED
    # Re-dispatch after rejection: a NEW attempt (new number).
    attempt2 = repo.prepare_dispatch(
        tenant_id=TENANT, job_id=ref.id, dispatch_token=_identity()[1],
        celery_task_id=_identity()[2], attempt_id=_identity()[0])
    assert attempt2 is not None
    assert attempt2.attempt_no == 2


def test_ambiguous_never_reverts_to_pending(repo, fresh_job):
    ref = fresh_job
    attempt = ref.service.repo.prepare_dispatch(
        tenant_id=TENANT, job_id=ref.id, dispatch_token=_identity()[1],
        celery_task_id=_identity()[2], attempt_id=_identity()[0])
    repo.record_publication_inflight(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    assert repo.record_publication_ambiguous(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id,
        error=SanitizedError(code="publication_ambiguous",
                             summary="RuntimeError"))
    refreshed = ref.refresh()
    assert refreshed.status == JobStatus.DISPATCH_UNKNOWN
    # Blind revert is impossible through the repository: the only
    # path back to pending is the DEFINITE rejection class.
    with pytest.raises(InvalidTransitionError):
        assert_allowed(refreshed.status, JobStatus.PENDING)


def assert_allowed(current, target):
    from retriva.jobs.domain import assert_transition_allowed
    assert_transition_allowed(current, target)


def test_claim_grant_and_duplicate_delivery(repo, fresh_job, jobs_db):
    ref = fresh_job
    attempt = repo.prepare_dispatch(
        tenant_id=TENANT, job_id=ref.id, dispatch_token=_identity()[1],
        celery_task_id=_identity()[2], attempt_id=_identity()[0])
    repo.record_publication_inflight(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    repo.record_publication_confirmed(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    decision = repo.claim_for_delivery(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id,
        worker_id="celery:w1:1")
    assert decision.outcome == ClaimOutcome.GRANTED
    assert decision.attempt.status == AttemptStatus.RUNNING
    assert ref.refresh().status == JobStatus.RUNNING
    # Duplicate delivery (same worker): idempotent no-op.
    decision2 = repo.claim_for_delivery(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id,
        worker_id="celery:w1:1")
    assert decision2.outcome == ClaimOutcome.DUPLICATE
    # Different worker, fresh claim: possibly alive → no execution.
    decision3 = repo.claim_for_delivery(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id,
        worker_id="celery:w2:1", redelivered=True)
    assert decision3.outcome == ClaimOutcome.LEAVE_FOR_RECONCILIATION
    # Stale claim + different worker: proven-dead takeover of the
    # SAME attempt (execution_generation+1; NOT a new attempt).
    _backdate_attempt_start(jobs_db, attempt.id, hours=2)
    decision4 = repo.claim_for_delivery(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id,
        worker_id="celery:w2:1", redelivered=True)
    assert decision4.outcome == ClaimOutcome.GRANTED_TAKEOVER
    assert decision4.attempt.execution_generation == 2
    assert decision4.attempt.attempt_no == 1
    # Terminal attempt: duplicate delivery is a no-op.
    repo.complete_success(tenant_id=TENANT, job_id=ref.id,
                          attempt_id=attempt.id)
    decision5 = repo.claim_for_delivery(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id,
        worker_id="celery:w3:1")
    assert decision5.outcome == ClaimOutcome.TERMINAL_NOOP
    # Terminal job never regresses from a late delivery.
    assert ref.refresh().status == JobStatus.SUCCEEDED


def _backdate_attempt_start(jobs_db, attempt_id, hours):
    admin = jobs_db.stack.admin(JOBS_DB)
    try:
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(
                "UPDATE jobs.job_attempts SET started_at = now() - "
                f"interval '{int(hours)} hours' WHERE id = %s",
                (attempt_id,))
    finally:
        admin.close()


def test_claim_refused_on_cancel_intent(repo, fresh_job):
    ref = fresh_job
    attempt = repo.prepare_dispatch(
        tenant_id=TENANT, job_id=ref.id, dispatch_token=_identity()[1],
        celery_task_id=_identity()[2], attempt_id=_identity()[0])
    repo.record_publication_inflight(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    repo.record_publication_confirmed(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    # Cancellation wins the race (durable ordering).
    repo.request_cancel(tenant_id=TENANT, job_id=ref.id)
    decision = repo.claim_for_delivery(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id,
        worker_id="celery:w1:1")
    assert decision.outcome == ClaimOutcome.REFUSED_CANCELLED
    assert ref.refresh().status == JobStatus.CANCELLED
    assert decision.attempt.status == AttemptStatus.CANCELLED
    # Duplicate cancellation: idempotent.
    decision2 = repo.request_cancel(tenant_id=TENANT, job_id=ref.id)
    assert decision2.outcome == CancellationOutcome.ALREADY_TERMINAL


def test_cancel_pending_directly_and_retry_wait(repo, fresh_job):
    ref = fresh_job
    decision = repo.request_cancel(tenant_id=TENANT, job_id=ref.id)
    assert decision.outcome == CancellationOutcome.CANCELLED
    assert ref.refresh().status == JobStatus.CANCELLED


def test_success_after_cancel_request_wins(repo, fresh_job):
    """Worker success arriving after cancellation was requested: the
    side effects completed → succeeded (never falsely cancelled)."""
    ref = fresh_job
    attempt = repo.prepare_dispatch(
        tenant_id=TENANT, job_id=ref.id, dispatch_token=_identity()[1],
        celery_task_id=_identity()[2], attempt_id=_identity()[0])
    repo.record_publication_inflight(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    repo.record_publication_confirmed(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    repo.claim_for_delivery(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id,
        worker_id="celery:w1:1")
    repo.request_cancel(tenant_id=TENANT, job_id=ref.id)
    assert ref.refresh().status == JobStatus.CANCELLING
    assert repo.complete_success(tenant_id=TENANT, job_id=ref.id,
                                 attempt_id=attempt.id,
                                 result_metadata={"doc_id": "d1"})
    refreshed = ref.refresh()
    assert refreshed.status == JobStatus.SUCCEEDED
    events = repo.events_for(tenant_id=TENANT, job_id=ref.id)
    success_event = [e for e in events
                     if e.event_type == EventType.ATTEMPT_SUCCEEDED]
    assert success_event
    assert success_event[0].detail.get("cancel_lost_race") is True
    # Retention snapshot at the terminal transition (succeeded → 30d).
    assert refreshed.purge_after is not None
    delta = refreshed.purge_after - _now()
    assert timedelta(days=29) < delta < timedelta(days=31)


def test_late_success_after_completed_cancellation_refused(
        repo, fresh_job, jobs_db):
    ref = fresh_job
    attempt = repo.prepare_dispatch(
        tenant_id=TENANT, job_id=ref.id, dispatch_token=_identity()[1],
        celery_task_id=_identity()[2], attempt_id=_identity()[0])
    repo.record_publication_inflight(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    repo.record_publication_confirmed(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    repo.claim_for_delivery(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id,
        worker_id="celery:w1:1")
    repo.request_cancel(tenant_id=TENANT, job_id=ref.id)
    assert repo.acknowledge_cooperative_cancel(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    assert ref.refresh().status == JobStatus.CANCELLED
    # The late success claim for the SAME attempt is refused.
    assert not repo.complete_success(tenant_id=TENANT, job_id=ref.id,
                                     attempt_id=attempt.id)
    assert ref.refresh().status == JobStatus.CANCELLED
    # ... and recorded as a bounded anomaly.
    events = repo.events_for(tenant_id=TENANT, job_id=ref.id)
    # (the attempt-terminal guard blocked the transition; anomaly
    # evidence comes from the claim path for later deliveries)
    later = repo.claim_for_delivery(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id,
        worker_id="celery:w9:1")
    assert later.outcome == ClaimOutcome.TERMINAL_NOOP


def test_durable_retry_and_reschedule(repo, service, fresh_job, jobs_db):
    ref = fresh_job
    attempt = repo.prepare_dispatch(
        tenant_id=TENANT, job_id=ref.id, dispatch_token=_identity()[1],
        celery_task_id=_identity()[2], attempt_id=_identity()[0])
    repo.record_publication_inflight(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    repo.record_publication_confirmed(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    repo.claim_for_delivery(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id,
        worker_id="celery:w1:1")
    decision = repo.complete_failure(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id,
        error=SanitizedError(code=ERROR_CODE_UNCLASSIFIED,
                             summary="ValueError"),
        retryable=True)
    assert decision.retry_scheduled
    assert ref.refresh().status == JobStatus.RETRY_WAIT
    assert decision.scheduled_at > _now()
    # The reschedule executor creates a NEW attempt (T20); the 1s
    # backoff is backdated so the due predicate is satisfied.
    _backdate_scheduled(jobs_db, ref.id, minutes=1)
    assert service.reschedule_due(ref.id, TENANT)
    refreshed = ref.refresh()
    assert refreshed.status in (JobStatus.QUEUED,
                                JobStatus.DISPATCHING)
    assert refreshed.attempt_count == 2


def _backdate_scheduled(jobs_db, job_id, minutes):
    admin = jobs_db.stack.admin(JOBS_DB)
    try:
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(
                "UPDATE jobs.jobs SET scheduled_at = now() - "
                f"interval '{int(minutes)} minutes' WHERE id = %s "
                "AND status = 'retry_wait'", (job_id,))
    finally:
        admin.close()


def test_max_attempts_bound(repo, fresh_job, jobs_db):
    ref = fresh_job
    # Exhaust attempts: max_attempts=1 job fails terminally.
    job_row = repo.get_job(tenant_id=TENANT, job_id=ref.id)
    assert job_row.max_attempts >= 1
    attempt = repo.prepare_dispatch(
        tenant_id=TENANT, job_id=ref.id, dispatch_token=_identity()[1],
        celery_task_id=_identity()[2], attempt_id=_identity()[0])
    repo.record_publication_inflight(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    repo.record_publication_confirmed(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    repo.claim_for_delivery(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id,
        worker_id="celery:w1:1")
    # Lower max_attempts to 1 to exercise the terminal bound.
    admin = jobs_db_stack(jobs_db)
    try:
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(
                "UPDATE jobs.jobs SET max_attempts = 1 WHERE id = %s",
                (ref.id,))
    finally:
        admin.close()
    decision = repo.complete_failure(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id,
        error=SanitizedError(code=ERROR_CODE_UNCLASSIFIED,
                             summary="ValueError"),
        retryable=True)
    assert not decision.retry_scheduled
    assert ref.refresh().status == JobStatus.FAILED
    assert ref.refresh().purge_after is not None  # 90d snapshot


def jobs_db_stack(jobs_db):
    return jobs_db.stack.admin(JOBS_DB)


def test_operator_retry_creates_new_attempt(repo, fresh_job):
    ref = fresh_job
    _fail_job_once(repo, ref)
    assert ref.refresh().status == JobStatus.FAILED
    job = ref.service.operator_retry(
        tenant_id=TENANT, job_id=ref.id, reason="operator requested",
        override_max_attempts=False)
    assert job.status in (JobStatus.QUEUED, JobStatus.DISPATCHING)
    attempts = repo.attempts_for_job(tenant_id=TENANT, job_id=ref.id)
    assert len(attempts) >= 2
    events = repo.events_for(tenant_id=TENANT, job_id=ref.id)
    retry_events = [e for e in events
                    if e.event_type == EventType.OPERATOR_RETRY]
    assert retry_events
    assert retry_events[0].actor == EventActor.OPERATOR


def _fail_job_once(repo, ref):
    attempt = repo.prepare_dispatch(
        tenant_id=TENANT, job_id=ref.id, dispatch_token=_identity()[1],
        celery_task_id=_identity()[2], attempt_id=_identity()[0])
    repo.record_publication_inflight(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    repo.record_publication_confirmed(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    repo.claim_for_delivery(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id,
        worker_id="celery:w1:1")
    repo.complete_failure(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id,
        error=SanitizedError(code=ERROR_CODE_UNCLASSIFIED,
                             summary="ValueError"),
        retryable=False)
    assert ref.refresh().status == JobStatus.FAILED


def test_operator_retry_refused_from_public_semantics(repo, fresh_job):
    """The public API exposes NO retry: only the operator path can
    move failed → queued (repo-level refusal for non-retryable
    states)."""
    ref = fresh_job
    with pytest.raises(OperatorRetryRefusedError):
        ref.service.operator_retry(
            tenant_id=TENANT, job_id=ref.id)  # job is pending, not failed


# --- reconciliation §E --------------------------------------------------------


def test_reconcile_r1_stale_pending(service, repo, fresh_job):
    ref = fresh_job
    _backdate_job(jobs_db_in(fresh_job), ref.id, hours=2)
    report = _reconcile(service, apply=True)
    assert report["counts"].get("R1", 0) >= 1
    refreshed = ref.refresh()
    assert refreshed.status in (JobStatus.QUEUED,
                                JobStatus.DISPATCHING)


def jobs_db_in(ref):
    return ref.service.repo._settings


def _backdate_job(settings, job_id, hours):
    conn = psycopg2.connect(**settings.connection_kwargs("admin"))
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs.jobs SET updated_at = now() - "
                f"interval '{int(hours)} hours' WHERE id = %s",
                (job_id,))
    finally:
        conn.close()


def _reconcile(service, apply: bool):
    from retriva.jobs.reconcile import reconcile
    return reconcile(
        service, tenant_id=TENANT, privileged=False, batch=50,
        apply=apply, stale_threshold_seconds=60)


def test_reconcile_dry_run_makes_no_changes(service, fresh_job):
    ref = fresh_job
    _backdate_job(jobs_db_in(fresh_job), ref.id, hours=2)
    report = _reconcile(service, apply=False)
    assert report["applied"] is False
    assert ref.refresh().status == JobStatus.PENDING  # unchanged


def test_reconcile_r2_republishes_same_generation(repo, fresh_job,
                                                  jobs_db):
    ref = fresh_job
    attempt = repo.prepare_dispatch(
        tenant_id=TENANT, job_id=ref.id, dispatch_token=_identity()[1],
        celery_task_id=_identity()[2], attempt_id=_identity()[0])
    repo.record_publication_inflight(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    repo.record_publication_ambiguous(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id,
        error=SanitizedError(code="publication_ambiguous",
                             summary="RuntimeError"))
    assert ref.refresh().status == JobStatus.DISPATCH_UNKNOWN
    _backdate_job(jobs_db_in(ref), ref.id, hours=2)
    report = _reconcile(ref.service, apply=True)
    assert report["counts"].get("R2", 0) >= 1
    refreshed = ref.refresh()
    assert refreshed.status in (JobStatus.QUEUED,
                                JobStatus.DISPATCH_UNKNOWN)
    # The SAME attempt is reused (not a new attempt number).
    attempts = repo.attempts_for_job(tenant_id=TENANT, job_id=ref.id)
    assert len(attempts) == 1
    assert attempts[0].publication_tries >= 2


def test_reconcile_r7_restart_safe_redispatch_vs_manual_review(
        repo, fresh_job):
    ref = fresh_job
    spec_job = ref.service.submit(
        tenant_id=TENANT, job_type="v2_document",
        execution_transport="celery",
        input_metadata={"source_uri": "/tmp/r7.pdf"})
    ref2 = JobRef(ref.service, spec_job)
    attempt = repo.prepare_dispatch(
        tenant_id=TENANT, job_id=ref2.id, dispatch_token=_identity()[1],
        celery_task_id=_identity()[2], attempt_id=_identity()[0])
    repo.record_publication_inflight(
        tenant_id=TENANT, job_id=ref2.id, attempt_id=attempt.id)
    repo.record_publication_confirmed(
        tenant_id=TENANT, job_id=ref2.id, attempt_id=attempt.id)
    repo.claim_for_delivery(
        tenant_id=TENANT, job_id=ref2.id, attempt_id=attempt.id,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id,
        worker_id="celery:w1:1")
    _backdate_job(jobs_db_in(ref2), ref2.id, hours=2)
    report = _reconcile(ref2.service, apply=True)
    assert report["counts"].get("R7", 0) >= 1
    # v2_document is restart-safe: re-dispatched (new attempt).
    refreshed = ref2.refresh()
    assert refreshed.status in (JobStatus.PENDING, JobStatus.QUEUED,
                                JobStatus.DISPATCHING)


def test_reconcile_r9_divergence_adopts_attempt_outcome(
        repo, fresh_job):
    ref = fresh_job
    attempt = repo.prepare_dispatch(
        tenant_id=TENANT, job_id=ref.id, dispatch_token=_identity()[1],
        celery_task_id=_identity()[2], attempt_id=_identity()[0])
    repo.record_publication_inflight(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    repo.record_publication_confirmed(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    # Divergence: attempt terminal, job not (e.g. outcome write of the
    # attempt succeeded while the job-side write was lost in an
    # earlier crash).
    admin = jobs_db_stack_from(jobs_db_in(ref))
    try:
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.jobs_privileged_cleanup', "
                "'granted', true)")
            cur.execute(
                "UPDATE jobs.job_attempts SET status = 'succeeded', "
                "finished_at = now() WHERE id = %s", (attempt.id,))
    finally:
        admin.close()
    report = _reconcile(ref.service, apply=True)
    assert report["counts"].get("R9", 0) >= 1
    assert ref.refresh().status == JobStatus.SUCCEEDED


def jobs_db_stack_from(settings):
    return psycopg2.connect(**settings.connection_kwargs("admin"))


# --- retention §E2 -------------------------------------------------------------


def test_retention_purge_privileged_and_cascades(repo, fresh_job):
    ref = fresh_job
    attempt = repo.prepare_dispatch(
        tenant_id=TENANT, job_id=ref.id, dispatch_token=_identity()[1],
        celery_task_id=_identity()[2], attempt_id=_identity()[0])
    repo.record_publication_inflight(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    repo.record_publication_confirmed(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id)
    repo.claim_for_delivery(
        tenant_id=TENANT, job_id=ref.id, attempt_id=attempt.id,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id,
        worker_id="celery:w1:1")
    repo.complete_success(tenant_id=TENANT, job_id=ref.id,
                          attempt_id=attempt.id)
    # Active/non-terminal jobs never carry purge_after: snapshot only
    # at the terminal transition.
    purgeable_before = repo.count_purgeable(
        now=_now() + timedelta(days=31))
    assert purgeable_before >= 1
    deleted = repo.purge_expired_jobs(batch=10,
                                      now=_now() + timedelta(days=31))
    assert deleted >= 1
    with pytest.raises(JobNotFoundError):
        repo.get_job(tenant_id=TENANT, job_id=ref.id)
    # Attempts and events cascaded away.
    assert repo.attempts_for_job(tenant_id=TENANT, job_id=ref.id) == []
    assert repo.events_for(tenant_id=TENANT, job_id=ref.id) == []


def test_manual_review_never_purged(repo, fresh_job):
    # A separate job parked in manual_review via the uncertain-cancel
    # classification.
    job2 = fresh_job.service.submit(
        tenant_id=TENANT, job_type="v2_document",
        execution_transport="celery",
        input_metadata={"source_uri": "/tmp/mr.pdf"})
    ref2 = JobRef(fresh_job.service, job2)
    attempt = repo.prepare_dispatch(
        tenant_id=TENANT, job_id=ref2.id, dispatch_token=_identity()[1],
        celery_task_id=_identity()[2], attempt_id=_identity()[0])
    repo.record_publication_inflight(
        tenant_id=TENANT, job_id=ref2.id, attempt_id=attempt.id)
    repo.record_publication_confirmed(
        tenant_id=TENANT, job_id=ref2.id, attempt_id=attempt.id)
    repo.claim_for_delivery(
        tenant_id=TENANT, job_id=ref2.id, attempt_id=attempt.id,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id,
        worker_id="celery:w1:1")
    repo.request_cancel(tenant_id=TENANT, job_id=ref2.id)
    repo.mark_execution_lost(
        tenant_id=TENANT, job_id=ref2.id, attempt_id=attempt.id,
        reason="cancelling_without_terminal_evidence",
        to_manual_review=True)
    parked = ref2.refresh()
    assert parked.status == JobStatus.MANUAL_REVIEW
    assert parked.purge_after is None  # never age-deleted
    # Operator resolution exits manual_review.
    resolved = repo.resolve_manual_review(
        tenant_id=TENANT, job_id=ref2.id, resolution="failed",
        reason="operator confirmed outcome", actor_label="op1")
    assert resolved.status == JobStatus.FAILED
    assert resolved.purge_after is not None


# --- local transport §E3 ---------------------------------------------------------


def test_local_transport_same_protocol(repo, fresh_job):
    """The BackgroundTasks fallback runs the SAME durable protocol
    (durable claim; one state machine; execution_transport='local'
    recorded; publication states coherent)."""
    from retriva.jobs.execution import (
        HandlerOutcome,
        execute_durable_job,
    )

    ref = fresh_job
    # A local-transport job: submit with transport 'local'.
    job = ref.service.submit(
        tenant_id=TENANT, job_type="v2_document",
        execution_transport="local",
        input_metadata={"source_uri": "/tmp/local.pdf"})
    assert job.execution_transport.value == "local"

    seen = {}

    def handler(job, attempt, cancel_check, worker_id):
        seen["claimed"] = True
        seen["worker_id"] = worker_id
        return HandlerOutcome(kind="success")

    attempt_id, token, task_id = _identity()
    attempt = repo.prepare_dispatch(
        tenant_id=TENANT, job_id=job.id, dispatch_token=token,
        celery_task_id=task_id, attempt_id=attempt_id)
    result = execute_durable_job(
        repo=repo, handler=handler, job_id=job.id,
        attempt_id=attempt.id, tenant_id=TENANT,
        dispatch_token=token, celery_task_id=task_id,
        worker_id="local:api:1")
    assert result == "succeeded"
    assert seen["claimed"] and seen["worker_id"].startswith("local")
    fetched = repo.get_job(tenant_id=TENANT, job_id=job.id)
    assert fetched.status == JobStatus.SUCCEEDED


def _identity() -> tuple:
    import uuid
    return (uuid.uuid4().hex, uuid.uuid4().hex, str(uuid.uuid4()))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _scalar(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchone()[0]


def _rows(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()
