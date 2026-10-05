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

"""API tests for the v2 durable job surface (Spec 025 acceptance §F):
the real FastAPI app against a scratch PostgreSQL jobs database with
the durable service bound in-process.

Covers: no manual-retry route on the public surface, server-side
tenant trust model (fixed tenant; constrained loopback development
override), tenant-scoped durable listing/get with pagination bounds,
durable-only resolution (the legacy fallback was retired by
Spec 027), durable cancel semantics (idempotent, terminal 409,
manual_review 409, unknown 404), and the idempotent submit path
through the route."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from fastapi.testclient import TestClient  # noqa: E402

from retriva.infrastructure.postgres.migrations import (  # noqa: E402
    CORE_PLATFORM_STREAM,
    load_provider_registry,
    upgrade as framework_upgrade,
)

from retriva.ingestion_api import durable_jobs  # noqa: E402
from retriva.ingestion_api.durable_jobs import (  # noqa: E402
    reset_jobs_service,
)
from retriva.ingestion_api.main import app  # noqa: E402
from retriva.jobs.config import (  # noqa: E402
    JobsSettings,
    reset_jobs_settings,
)
from retriva.jobs.dispatch import LocalPublisher  # noqa: E402
from retriva.jobs.domain import JobStatus  # noqa: E402
from retriva.jobs.registry import job_type_registry  # noqa: E402
from retriva.jobs.repository import PostgresJobsRepository  # noqa: E402
from retriva.jobs.service import JobsService  # noqa: E402
from retriva.jobs.tenant import reset_tenant_resolver  # noqa: E402

JOBS_DB = "retriva_pg_test_jobs_api"
TENANT = "api-tenant"
OTHER_TENANT = "other-tenant"
TENANT_HEADER = "x-retriva-tenant"


def _identity() -> tuple:
    return (uuid.uuid4().hex, uuid.uuid4().hex, str(uuid.uuid4()))


@pytest.fixture(scope="module")
def jobs_api_db(pg_platform_stack):
    settings = pg_platform_stack.fresh_database(JOBS_DB)
    registry = load_provider_registry("")
    from retriva.jobs.migrations import jobs_provider
    registry.register_core_stream(jobs_provider())
    result = framework_upgrade(registry, settings)
    assert [a["version"] for a in
            result[CORE_PLATFORM_STREAM][0]["applied"]] == [1]
    assert [a["version"] for a in
            result["core.jobs"][0]["applied"]] == [1]
    return SimpleNamespace(settings=settings, stack=pg_platform_stack)


@pytest.fixture()
def clean_jobs(jobs_api_db):
    """Truncate the durable job tables for tests asserting exact list
    contents (module-scoped DB would otherwise leak earlier jobs)."""
    conn = jobs_api_db.stack.admin(JOBS_DB)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE jobs.job_events, jobs.job_attempts, "
                "jobs.jobs CASCADE")
        conn.commit()
    finally:
        conn.close()
    yield


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_jobs_service()
    reset_tenant_resolver()
    reset_jobs_settings()
    yield
    reset_jobs_service()
    reset_tenant_resolver()
    reset_jobs_settings()


@pytest.fixture()
def api_env(monkeypatch):
    """Fixed-resolver development posture (mandatory tenant set)."""
    monkeypatch.setenv("RETRIVA_JOBS_DEFAULT_TENANT", TENANT)
    monkeypatch.delenv("RETRIVA_JOBS_TENANT_RESOLVER", raising=False)
    monkeypatch.delenv("RETRIVA_JOBS_TENANT_HEADER_OVERRIDE",
                       raising=False)
    monkeypatch.delenv("RETRIVA_JOBS_TENANT_HEADER_NAME", raising=False)
    reset_jobs_settings()
    reset_tenant_resolver()
    yield


@pytest.fixture()
def bound_service(api_env, jobs_api_db):
    """Bind the process-wide durable service to the scratch jobs DB
    with a LocalPublisher whose runners are recorded (never executed
    pipelines: the runner only records the delivered envelope)."""
    repo = PostgresJobsRepository(jobs_api_db.settings)
    runners = []

    def runner_factory(envelope):
        def runner():
            runners.append(envelope)
        return runner

    service = JobsService(
        repo=repo, settings=JobsSettings(),
        registry=job_type_registry(),
        publisher=LocalPublisher(runner_factory))
    durable_jobs._service = service
    return SimpleNamespace(service=service, repo=repo, runners=runners)


@pytest.fixture()
def client(api_env):
    with patch("retriva.ingestion_api.main.get_client"), \
            patch("retriva.ingestion_api.main.init_collection"):
        with TestClient(app) as test_client:
            yield test_client


def _drive_to_succeeded(bound_service, job):
    """Drive a submitted job through the durable protocol to
    ``succeeded`` with repository primitives (claim + success)."""
    repo = bound_service.repo
    attempt_id, token, task_id = _identity()
    attempt = repo.prepare_dispatch(
        tenant_id=TENANT, job_id=job.id, dispatch_token=token,
        celery_task_id=task_id, attempt_id=attempt_id)
    assert attempt is not None
    repo.record_publication_inflight(
        tenant_id=TENANT, job_id=job.id, attempt_id=attempt.id)
    assert repo.record_publication_confirmed(
        tenant_id=TENANT, job_id=job.id, attempt_id=attempt.id)
    decision = repo.claim_for_delivery(
        tenant_id=TENANT, job_id=job.id, attempt_id=attempt.id,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id,
        worker_id="celery:w1:1")
    assert decision.outcome.value == "granted"
    assert repo.complete_success(
        tenant_id=TENANT, job_id=job.id, attempt_id=attempt.id)


def _drive_to_manual_review(bound_service, job):
    repo = bound_service.repo
    attempt_id, token, task_id = _identity()
    attempt = repo.prepare_dispatch(
        tenant_id=TENANT, job_id=job.id, dispatch_token=token,
        celery_task_id=task_id, attempt_id=attempt_id)
    repo.record_publication_inflight(
        tenant_id=TENANT, job_id=job.id, attempt_id=attempt.id)
    repo.record_publication_confirmed(
        tenant_id=TENANT, job_id=job.id, attempt_id=attempt.id)
    decision = repo.claim_for_delivery(
        tenant_id=TENANT, job_id=job.id, attempt_id=attempt.id,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id,
        worker_id="celery:w1:1")
    assert decision.outcome.value == "granted"
    lost = repo.mark_execution_lost(
        tenant_id=TENANT, job_id=job.id, attempt_id=attempt.id,
        reason="heartbeat_lost", to_manual_review=True)
    assert lost.status == JobStatus.MANUAL_REVIEW


# ---------------------------------------------------------------------------
# No manual-retry route on the unauthenticated public surface (Spec
# 025 §3.6: operator CLI only)
# ---------------------------------------------------------------------------

def test_no_public_retry_route():
    paths = app.openapi()["paths"]
    assert "/api/v2/jobs/{job_id}/retry" not in paths
    assert not any("/retry" in p for p in paths
                   if p.startswith("/api/v2/jobs"))


def test_public_retry_attempt_returns_404(client, bound_service):
    job = bound_service.service.submit(
        tenant_id=TENANT, job_type="v2_document",
        execution_transport="celery",
        input_metadata={"source_uri": "/tmp/x.pdf"})
    response = client.post(f"/api/v2/jobs/{job.id}/retry")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Submit through the route: durable record + idempotency
# ---------------------------------------------------------------------------

def test_submit_via_api_creates_durable_job(client, bound_service):
    response = client.post("/api/v2/documents", json={
        "source_uri": "/api-test/doc1.pdf", "kb_id": "default"})
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert job_id
    # The route registered the local runner (BackgroundTasks executed
    # after the response; the runner records the envelope).
    assert len(bound_service.runners) == 1
    envelope = bound_service.runners[0]
    assert envelope.job_id == job_id
    assert envelope.job_type == "v2_document"
    assert envelope.tenant_id == TENANT
    assert envelope.celery_task_id

    detail = client.get(f"/api/v2/jobs/{job_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["job_id"] == job_id
    assert body["job_type"] == "v2_document"
    assert body["status"] == "dispatching"
    assert body["source"] == "/api-test/doc1.pdf"
    assert body["created_at"] and body["updated_at"]


def test_submit_via_api_is_idempotent(client, bound_service):
    payload = {"source_uri": "/api-test/idem.pdf", "kb_id": "default"}
    first = client.post("/api/v2/documents", json=payload)
    assert first.status_code == 202
    second = client.post("/api/v2/documents", json=payload)
    assert second.status_code == 202
    assert second.json()["job_id"] == first.json()["job_id"]
    # The raced second dispatch was a guarded no-op (no new runner).
    assert len(bound_service.runners) == 1


def test_other_content_is_a_distinct_job(client, bound_service):
    first = client.post("/api/v2/documents", json={
        "source_uri": "/api-test/a.pdf", "kb_id": "default"})
    second = client.post("/api/v2/documents", json={
        "source_uri": "/api-test/b.pdf", "kb_id": "default"})
    assert first.json()["job_id"] != second.json()["job_id"]
    assert len(bound_service.runners) == 2


# ---------------------------------------------------------------------------
# Server-side tenant trust model (Spec 025 §3.12)
# ---------------------------------------------------------------------------

def test_fixed_resolver_ignores_ordinary_header(client, bound_service):
    response = client.post(
        "/api/v2/documents",
        json={"source_uri": "/api-test/t.pdf", "kb_id": "default"},
        headers={TENANT_HEADER: OTHER_TENANT})
    assert response.status_code == 202
    envelope = bound_service.runners[0]
    # Ordinary request input never selects a tenant: the fixed,
    # server-configured tenant applies.
    assert envelope.tenant_id == TENANT


def test_dev_override_loopback_constrained(client, bound_service,
                                           monkeypatch):
    monkeypatch.setenv("RETRIVA_JOBS_TENANT_HEADER_OVERRIDE", "1")
    reset_jobs_settings()
    reset_tenant_resolver()
    response = client.post(
        "/api/v2/documents",
        json={"source_uri": "/api-test/ov.pdf", "kb_id": "default"},
        headers={TENANT_HEADER: OTHER_TENANT})
    assert response.status_code == 202
    assert bound_service.runners[0].tenant_id == OTHER_TENANT
    # Without the header the fixed tenant applies again; the other
    # tenant's job is not visible in the fixed tenant's list.
    listing = client.get("/api/v2/jobs")
    assert listing.status_code == 200
    ids = [j["job_id"] for j in listing.json()]
    assert response.json()["job_id"] not in ids


# ---------------------------------------------------------------------------
# Durable-first listing/get: tenant scope, filters, pagination
# ---------------------------------------------------------------------------

def test_list_empty_durable_store(client, bound_service, clean_jobs):
    response = client.get("/api/v2/jobs")
    assert response.status_code == 200
    assert response.json() == []


def test_list_tenant_scoped_and_filtered(client, bound_service,
                                         clean_jobs):
    mine = [bound_service.service.submit(
        tenant_id=TENANT, job_type="v2_document",
        execution_transport="celery",
        input_metadata={"source_uri": f"/doc/m{i}.pdf"})
        for i in range(3)]
    for i in range(2):
        bound_service.service.submit(
            tenant_id=OTHER_TENANT, job_type="v2_document",
            execution_transport="celery",
            input_metadata={"source_uri": f"/doc/o{i}.pdf"})
    mediawiki = bound_service.service.submit(
        tenant_id=TENANT, job_type="v2_mediawiki",
        execution_transport="celery",
        input_metadata={"staged_dir": "/staged/export"})

    listing = client.get("/api/v2/jobs")
    assert listing.status_code == 200
    ids = [j["job_id"] for j in listing.json()]
    assert set(ids) == {j.id for j in mine} | {mediawiki.id}

    listing = client.get("/api/v2/jobs", params={"job_type":
                                                 "v2_mediawiki"})
    assert [j["job_id"] for j in listing.json()] == [mediawiki.id]

    listing = client.get("/api/v2/jobs", params={"status": "pending"})
    assert {j["job_id"] for j in listing.json()} == \
        {j.id for j in mine} | {mediawiki.id}
    listing = client.get("/api/v2/jobs", params={"status": "running"})
    assert listing.json() == []


def test_list_pagination_bounds(client, bound_service, clean_jobs):
    for i in range(3):
        bound_service.service.submit(
            tenant_id=TENANT, job_type="v2_document",
            execution_transport="celery",
            input_metadata={"source_uri": f"/doc/p{i}.pdf"})

    page1 = client.get("/api/v2/jobs", params={"limit": 1})
    assert page1.status_code == 200
    assert len(page1.json()) == 1
    page2 = client.get("/api/v2/jobs", params={"limit": 1, "offset": 1})
    assert page2.status_code == 200
    assert page2.json()[0]["job_id"] != page1.json()[0]["job_id"]

    for params in ({"limit": 0}, {"limit": 201}, {"offset": -1}):
        assert client.get("/api/v2/jobs",
                          params=params).status_code == 422


def test_get_unknown_and_other_tenant_404(client, bound_service):
    assert client.get("/api/v2/jobs/missing-id").status_code == 404
    other = bound_service.service.submit(
        tenant_id=OTHER_TENANT, job_type="v2_document",
        execution_transport="celery",
        input_metadata={"source_uri": "/doc/other.pdf"})
    # Tenant-scoped read: another tenant's id resolves 404 here
    # (durable-only resolution; Spec 027 removed the legacy fallback).
    assert client.get(f"/api/v2/jobs/{other.id}").status_code == 404


def test_removed_legacy_fallback_404(client, bound_service):
    """Ids unknown to the durable store return plain 404 — the legacy
    in-memory/Redis fallback projection was retired by Spec 027."""
    response = client.get("/api/v2/jobs/legacy-in-memory-id")
    assert response.status_code == 404
    assert response.json()["detail"] == "Job not found"


# ---------------------------------------------------------------------------
# Durable cancellation semantics (Spec 025 §3.3)
# ---------------------------------------------------------------------------

def test_cancel_dispatching_job_idempotent(client, bound_service):
    job = bound_service.service.submit(
        tenant_id=TENANT, job_type="v2_document",
        execution_transport="celery",
        input_metadata={"source_uri": "/doc/cancel.pdf"})
    # Dispatch first: the route's no-op runner leaves the job in
    # ``dispatching`` (publication awaited in-process).
    dispatch = bound_service.service.dispatch_job(
        job, payload={"source_uri": "/doc/cancel.pdf"})
    assert dispatch is not None
    response = client.post(f"/api/v2/jobs/{job.id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelling"
    # Duplicate cancel is idempotent (durable intent, same state).
    again = client.post(f"/api/v2/jobs/{job.id}/cancel")
    assert again.status_code == 200
    assert again.json()["status"] == "cancelling"
    # The intent is durable: the stored record shows the cancelled
    # intent, not a transport-level effect.
    stored = bound_service.service.get_job(
        tenant_id=TENANT, job_id=job.id)
    assert stored.status == JobStatus.CANCELLING


def test_cancel_pending_direct_nothing_published(client, bound_service):
    """A pending job (never dispatched) cancels DIRECTLY to terminal
    ``cancelled`` (T2: nothing published — no cancelling limbo)."""
    job = bound_service.service.submit(
        tenant_id=TENANT, job_type="v2_document",
        execution_transport="celery",
        input_metadata={"source_uri": "/doc/pending.pdf"})
    response = client.post(f"/api/v2/jobs/{job.id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    stored = bound_service.service.get_job(
        tenant_id=TENANT, job_id=job.id)
    assert stored.status == JobStatus.CANCELLED


def test_cancel_terminal_returns_409(client, bound_service):
    job = bound_service.service.submit(
        tenant_id=TENANT, job_type="v2_document",
        execution_transport="celery",
        input_metadata={"source_uri": "/doc/done.pdf"})
    _drive_to_succeeded(bound_service, job)
    response = client.post(f"/api/v2/jobs/{job.id}/cancel")
    assert response.status_code == 409
    assert "already terminal" in response.json()["detail"]


def test_cancel_manual_review_returns_409(client, bound_service):
    job = bound_service.service.submit(
        tenant_id=TENANT, job_type="v2_document",
        execution_transport="celery",
        input_metadata={"source_uri": "/doc/review.pdf"})
    _drive_to_manual_review(bound_service, job)
    response = client.post(f"/api/v2/jobs/{job.id}/cancel")
    assert response.status_code == 409
    assert "operator" in response.json()["detail"]
    # manual_review is NOT terminal: the job persists for the operator.
    assert client.get(f"/api/v2/jobs/{job.id}").status_code == 200


def test_cancel_unknown_returns_404(client, bound_service):
    assert client.post(
        "/api/v2/jobs/missing-id/cancel").status_code == 404
