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

"""Durable-jobs canonical lock-order tests (Spec 029 / ADR-034).

Two complementary gates on real isolated PostgreSQL:

1. A static invariant check over ``PostgresJobsRepository`` source
   proving that no method acquires a ``job_attempts`` lock before the
   corresponding ``jobs`` lock.
2. A deterministic concurrency matrix driving the real repository
   methods with a ``threading.Barrier`` for each known race pair; the
   pre-fix inverse cycle reproduced as ``40P01`` deadlocks here (see
   the baseline control in the Spec 029 evidence), so these tests fail
   closed on any regression and assert final-state consistency.

Bounded transient-retry behavior (defense in depth) is unit-tested
directly against the decorator.
"""

from __future__ import annotations

import ast
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import psycopg2
import pytest

from retriva.infrastructure.postgres.migrations import (
    CORE_PLATFORM_STREAM,
    load_provider_registry,
    upgrade as framework_upgrade,
)
from retriva.jobs.config import JobsSettings
from retriva.jobs.dispatch import (
    PublishResult,
    PublicationOutcome,
)
from retriva.jobs.domain import (
    JobStatus,
    SanitizedError,
)
from retriva.jobs.registry import job_type_registry
from retriva.jobs.repository import (
    TRANSIENT_RETRY_ATTEMPTS,
    PostgresJobsRepository,
    _retry_on_transient,
    transient_retry_metrics,
)
from retriva.jobs.service import JobsService

TENANT = "tenant-lock"
LOCK_DB = "retriva_pg_test_lockorder"
REPO_SRC = (Path(__file__).resolve().parent.parent
            / "src" / "retriva" / "jobs" / "repository.py")


@pytest.fixture(scope="module")
def lock_db(pg_platform_stack):
    settings = pg_platform_stack.fresh_database(LOCK_DB)
    registry = load_provider_registry("")
    from retriva.jobs.migrations import jobs_provider
    registry.register_core_stream(jobs_provider())
    result = framework_upgrade(registry, settings)
    assert [a["version"] for a in
            result[CORE_PLATFORM_STREAM][0]["applied"]] == [1]
    return SimpleNamespace(settings=settings, stack=pg_platform_stack)


@pytest.fixture()
def repo(lock_db) -> PostgresJobsRepository:
    return PostgresJobsRepository(lock_db.settings)


@pytest.fixture()
def service(lock_db, repo) -> JobsService:
    class ConfirmedPublisher:
        def publish(self, envelope, queue=None):
            return PublishResult(PublicationOutcome.CONFIRMED)

    return JobsService(
        repo=repo, settings=JobsSettings(),
        registry=job_type_registry(), publisher=ConfirmedPublisher())


def _identity() -> tuple:
    return (uuid.uuid4().hex, uuid.uuid4().hex, str(uuid.uuid4()))


def _submit(service, tenant=TENANT) -> str:
    return service.submit(
        tenant_id=tenant, job_type="v2_document",
        execution_transport="celery",
        input_metadata={"source_uri": "/tmp/lockorder.txt"}).id


def _queued(repo, service, tenant=TENANT):
    """job ``queued`` + attempt ``queued``/``published``."""
    job_id = _submit(service, tenant)
    aid, tok, task = _identity()
    attempt = repo.prepare_dispatch(
        tenant_id=tenant, job_id=job_id, dispatch_token=tok,
        celery_task_id=task, attempt_id=aid)
    repo.record_publication_inflight(
        tenant_id=tenant, job_id=job_id, attempt_id=attempt.id)
    repo.record_publication_confirmed(
        tenant_id=tenant, job_id=job_id, attempt_id=attempt.id)
    return job_id, attempt


def _running(repo, service, tenant=TENANT, worker="celery:w1:1"):
    """job ``running`` + attempt ``running`` (claimed)."""
    job_id, attempt = _queued(repo, service, tenant)
    repo.claim_for_delivery(
        tenant_id=tenant, job_id=job_id, attempt_id=attempt.id,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id, worker_id=worker)
    return job_id, repo.get_attempt(tenant_id=tenant,
                                    attempt_id=attempt.id)


def _run_pair(fn_a, fn_b, rounds):
    """Run two real repository methods concurrently behind a barrier;
    return the list of raised exceptions per round."""
    errors = []
    for _ in range(rounds):
        barrier = threading.Barrier(2)
        seen = []

        def wrap(fn):
            barrier.wait()
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 - classified
                seen.append(exc)

        threads = [threading.Thread(target=wrap, args=(fn_a,)),
                   threading.Thread(target=wrap, args=(fn_b,))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        errors.extend(seen)
    return errors


def _assert_no_deadlock(errors):
    deadlocks = [e for e in errors
                 if isinstance(e, psycopg2.errors.DeadlockDetected)
                 or getattr(e, "pgcode", None) == "40P01"]
    assert not deadlocks, (
        f"{len(deadlocks)} known-cycle deadlock(s) remained: "
        f"{[type(e).__name__ for e in deadlocks]}")
    non_deadlock = [e for e in errors if not isinstance(
        e, (psycopg2.errors.DeadlockDetected,))]
    assert not non_deadlock, (
        "unexpected concurrent errors: "
        f"{[(type(e).__name__, str(e)[:80]) for e in non_deadlock]}")


def _one_terminal_effect(repo, job_id, tenant=TENANT):
    """No duplicated terminal effect across attempts and job."""
    attempts = repo.attempts_for_job(tenant_id=tenant, job_id=job_id)
    for status in ("succeeded", "failed"):
        n = sum(1 for a in attempts if a.status.value == status)
        assert n <= 1, f"duplicate {status} attempts: {n}"
    job = repo.get_job(tenant_id=tenant, job_id=job_id)
    assert job.status in list(JobStatus)
    return job


# --- static invariant -------------------------------------------------------

def test_repository_lock_order_invariant():
    """No repository method acquires a job_attempts lock before the
    corresponding jobs lock (Spec 029 / ADR-034 R1)."""
    tree = ast.parse(REPO_SRC.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef)
               and n.name == "PostgresJobsRepository")

    def events(fn):
        ev = []
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)):
                continue
            attr = node.func.attr
            if attr == "_lock_job_row":
                ev.append((node.lineno, "jobs"))
            elif attr == "_lock_attempt_row":
                ev.append((node.lineno, "attempts"))
            elif (attr == "execute" and node.args
                  and isinstance(node.args[0], ast.Constant)
                  and isinstance(node.args[0].value, str)):
                sql = node.args[0].value
                if "jobs.jobs" in sql and (
                        "FOR UPDATE" in sql
                        or sql.strip().startswith("UPDATE jobs.jobs")
                        or "INSERT INTO jobs.jobs" in sql):
                    ev.append((node.lineno, "jobs"))
                elif "jobs.job_attempts" in sql and (
                        "FOR UPDATE" in sql
                        or sql.strip().startswith(
                            "UPDATE jobs.job_attempts")):
                    ev.append((node.lineno, "attempts"))
        ev.sort(key=lambda x: x[0])
        return [r for _, r in ev]

    checked = 0
    for fn in cls.body:
        if not isinstance(fn, ast.FunctionDef) or fn.name.startswith(
                "_lock_"):
            continue
        rels = events(fn)
        if "jobs" in rels and "attempts" in rels:
            checked += 1
            assert rels.index("jobs") < rels.index("attempts"), (
                f"{fn.name} acquires attempts before jobs: {rels}")
    assert checked >= 9, f"too few two-relation methods checked: {checked}"


# --- deterministic concurrency matrix ---------------------------------------

def test_claim_vs_publication_confirmed(repo, service):
    errors = []
    for _ in range(40):
        job_id, attempt = _queued(repo, service)
        errors.extend(_run_pair(
            lambda: repo.record_publication_confirmed(
                tenant_id=TENANT, job_id=job_id, attempt_id=attempt.id),
            lambda: repo.claim_for_delivery(
                tenant_id=TENANT, job_id=job_id, attempt_id=attempt.id,
                dispatch_token=attempt.dispatch_token,
                celery_task_id=attempt.celery_task_id,
                worker_id="celery:w1:1"),
            rounds=1))
        _one_terminal_effect(repo, job_id)
    _assert_no_deadlock(errors)


def test_claim_vs_complete_success(repo, service):
    errors = []
    for _ in range(30):
        job_id, attempt = _running(repo, service)
        errors.extend(_run_pair(
            lambda: repo.claim_for_delivery(
                tenant_id=TENANT, job_id=job_id, attempt_id=attempt.id,
                dispatch_token=attempt.dispatch_token,
                celery_task_id=attempt.celery_task_id,
                worker_id="celery:w2:1", redelivered=True),
            lambda: repo.complete_success(
                tenant_id=TENANT, job_id=job_id, attempt_id=attempt.id),
            rounds=1))
        _one_terminal_effect(repo, job_id)
    _assert_no_deadlock(errors)


def test_claim_vs_complete_failure(repo, service):
    errors = []
    for _ in range(30):
        job_id, attempt = _running(repo, service)
        errors.extend(_run_pair(
            lambda: repo.claim_for_delivery(
                tenant_id=TENANT, job_id=job_id, attempt_id=attempt.id,
                dispatch_token=attempt.dispatch_token,
                celery_task_id=attempt.celery_task_id,
                worker_id="celery:w2:1", redelivered=True),
            lambda: repo.complete_failure(
                tenant_id=TENANT, job_id=job_id, attempt_id=attempt.id,
                error=SanitizedError(code="boom", summary="boom"),
                retryable=True),
            rounds=1))
        _one_terminal_effect(repo, job_id)
    _assert_no_deadlock(errors)


def test_claim_vs_request_cancel(repo, service):
    errors = []
    for _ in range(30):
        job_id, attempt = _queued(repo, service)
        errors.extend(_run_pair(
            lambda: repo.claim_for_delivery(
                tenant_id=TENANT, job_id=job_id, attempt_id=attempt.id,
                dispatch_token=attempt.dispatch_token,
                celery_task_id=attempt.celery_task_id,
                worker_id="celery:w1:1"),
            lambda: repo.request_cancel(
                tenant_id=TENANT, job_id=job_id),
            rounds=1))
        _one_terminal_effect(repo, job_id)
    _assert_no_deadlock(errors)


def test_progress_vs_request_cancel(repo, service):
    errors = []
    for _ in range(30):
        job_id, attempt = _running(repo, service)
        errors.extend(_run_pair(
            lambda: repo.record_progress(
                tenant_id=TENANT, job_id=job_id, progress=50,
                stage="parse", message=None),
            lambda: repo.request_cancel(
                tenant_id=TENANT, job_id=job_id),
            rounds=1))
        _one_terminal_effect(repo, job_id)
    _assert_no_deadlock(errors)


def test_success_vs_failure(repo, service):
    errors = []
    for _ in range(30):
        job_id, attempt = _running(repo, service)
        errors.extend(_run_pair(
            lambda: repo.complete_success(
                tenant_id=TENANT, job_id=job_id, attempt_id=attempt.id),
            lambda: repo.complete_failure(
                tenant_id=TENANT, job_id=job_id, attempt_id=attempt.id,
                error=SanitizedError(code="boom", summary="boom"),
                retryable=False),
            rounds=1))
        _one_terminal_effect(repo, job_id)
    _assert_no_deadlock(errors)


def test_success_vs_cancellation_ack(repo, service):
    errors = []
    for _ in range(30):
        job_id, attempt = _running(repo, service)
        repo.request_cancel(tenant_id=TENANT, job_id=job_id)
        errors.extend(_run_pair(
            lambda: repo.complete_success(
                tenant_id=TENANT, job_id=job_id, attempt_id=attempt.id),
            lambda: repo.acknowledge_cooperative_cancel(
                tenant_id=TENANT, job_id=job_id, attempt_id=attempt.id),
            rounds=1))
        _one_terminal_effect(repo, job_id)
    _assert_no_deadlock(errors)


def test_two_worker_claim(repo, service):
    errors = []
    for _ in range(30):
        job_id, attempt = _queued(repo, service)
        granted = []

        def claim(worker):
            decision = repo.claim_for_delivery(
                tenant_id=TENANT, job_id=job_id, attempt_id=attempt.id,
                dispatch_token=attempt.dispatch_token,
                celery_task_id=attempt.celery_task_id, worker_id=worker)
            if decision.outcome.value in ("granted", "granted_takeover"):
                granted.append(worker)

        barrier = threading.Barrier(2)
        seen = []

        def wrap(worker):
            barrier.wait()
            try:
                claim(worker)
            except BaseException as exc:  # noqa: BLE001
                seen.append(exc)

        threads = [threading.Thread(target=wrap, args=("celery:a:1",)),
                   threading.Thread(target=wrap, args=("celery:b:1",))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        errors.extend(seen)
        assert len(granted) == 1, f"claimants: {granted}"
        _one_terminal_effect(repo, job_id)
    _assert_no_deadlock(errors)


def test_stale_recovery_vs_completion(repo, service):
    errors = []
    for _ in range(25):
        job_id, attempt = _running(repo, service)
        errors.extend(_run_pair(
            lambda: repo.mark_execution_lost(
                tenant_id=TENANT, job_id=job_id, attempt_id=attempt.id,
                reason="worker_lost", redispatch=True),
            lambda: repo.complete_success(
                tenant_id=TENANT, job_id=job_id, attempt_id=attempt.id),
            rounds=1))
        _one_terminal_effect(repo, job_id)
    _assert_no_deadlock(errors)


def test_cancel_unclaimed_vs_claim(repo, service):
    errors = []
    for _ in range(25):
        job_id, attempt = _queued(repo, service)
        repo.request_cancel(tenant_id=TENANT, job_id=job_id)
        errors.extend(_run_pair(
            lambda: repo.cancel_unclaimed_attempt(
                tenant_id=TENANT, job_id=job_id, attempt_id=attempt.id),
            lambda: repo.claim_for_delivery(
                tenant_id=TENANT, job_id=job_id, attempt_id=attempt.id,
                dispatch_token=attempt.dispatch_token,
                celery_task_id=attempt.celery_task_id,
                worker_id="celery:w1:1"),
            rounds=1))
        _one_terminal_effect(repo, job_id)
    _assert_no_deadlock(errors)


# --- bounded transient retry (defense in depth) -----------------------------

def test_transient_retry_replays_then_succeeds():
    class Flaky:
        def __init__(self, fails):
            self.calls = 0
            self.fails = fails

        @_retry_on_transient
        def run(self):
            self.calls += 1
            if self.calls <= self.fails:
                raise psycopg2.errors.DeadlockDetected()
            return "ok"

    f = Flaky(fails=1)
    assert f.run() == "ok"
    assert f.calls == 2


def test_transient_retry_is_bounded_and_exhausts():
    class AlwaysDeadlock:
        def __init__(self):
            self.calls = 0

        @_retry_on_transient
        def run(self):
            self.calls += 1
            raise psycopg2.errors.DeadlockDetected()

    a = AlwaysDeadlock()
    with pytest.raises(psycopg2.errors.DeadlockDetected):
        a.run()
    assert a.calls == TRANSIENT_RETRY_ATTEMPTS


def test_transient_retry_does_not_retry_domain_errors():
    class Boom:
        def __init__(self):
            self.calls = 0

        @_retry_on_transient
        def run(self):
            self.calls += 1
            raise ValueError("not retryable")

    b = Boom()
    with pytest.raises(ValueError):
        b.run()
    assert b.calls == 1


def test_transient_retry_metrics_low_cardinality():
    keys = set(transient_retry_metrics())
    assert keys == {"transient_retries", "retries_exhausted"}
    for value in transient_retry_metrics().values():
        assert isinstance(value, int)