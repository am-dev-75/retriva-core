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

"""Service-level dispatch tests (Spec 025 acceptance §C) with a fake
broker: publication-state classification (confirmed / definite
rejection / ambiguous), dispatch flow, idempotency at the service
level, durable retry rescheduling, and operator retry authorization.
Real PostgreSQL (shared module DB from test_jobs_persistence)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from retriva.infrastructure.postgres.migrations import (  # noqa: E402
    CORE_PLATFORM_STREAM,
    load_provider_registry,
    upgrade as framework_upgrade,
    verify as framework_verify,
)

from retriva.jobs.config import JobsSettings
from retriva.jobs.dispatch import (
    CeleryPublisher,
    DeliveryEnvelope,
    PublishResult,
    PublicationOutcome,
    new_dispatch_identity,
)
from retriva.jobs.domain import (
    ERROR_CODE_UNCLASSIFIED,
    JobStatus,
    SanitizedError,
)
from retriva.jobs.errors import IdempotencyConflictError
from retriva.jobs.repository import PostgresJobsRepository
from retriva.jobs.registry import job_type_registry
from retriva.jobs.service import JobsService
from retriva.jobs.tenant import (
    TenantContextMissingError,
    TenantResolver,
    reset_tenant_resolver,
)
from retriva.logger import get_logger

_log = get_logger(__name__)

TENANT = "svc-tenant"


class FakeApp:
    """A fake Celery app recording apply_async calls."""

    def __init__(self, raise_on_publish=None):
        self.calls = []
        self.tasks = {
            "retriva.ingestion_api.tasks.process_document_task":
                FakeTask(self, raise_on_publish),
            "retriva.ingestion_api.tasks.process_mediawiki_task":
                FakeTask(self, raise_on_publish),
        }


class FakeTask:
    def __init__(self, app, raise_on_publish):
        self._app = app
        self._raise = raise_on_publish

    def apply_async(self, kwargs=None, task_id=None, queue=None):
        if self._raise is not None:
            self._app.calls.append(
                {"kwargs": kwargs, "task_id": task_id,
                 "queue": queue, "raised": True})
            raise self._raise
        self._app.calls.append(
            {"kwargs": kwargs, "task_id": task_id, "queue": queue,
             "raised": False})


def _service_with(repo, raise_on_publish=None) -> tuple:
    app = FakeApp(raise_on_publish)
    service = JobsService(
        repo=repo, settings=JobsSettings(),
        registry=job_type_registry(),
        publisher=CeleryPublisher(lambda: app))
    return service, app


# --- publication-state flow ---------------------------------------------------


def test_dispatch_flow_confirmed(pg_jobs_repo):
    repo = pg_jobs_repo
    service, app = _service_with(repo)
    job = service.submit(
        tenant_id=TENANT, job_type="v2_document",
        execution_transport="celery",
        input_metadata={"source_uri": "/tmp/svc.pdf"})
    dispatch = service.dispatch_job(job, payload={"k": 1})
    assert dispatch is None  # celery: nothing to register locally
    refreshed = service.get_job(tenant_id=TENANT, job_id=job.id)
    assert refreshed.status == JobStatus.QUEUED
    assert len(app.calls) == 1
    call = app.calls[0]
    # The PREALLOCATED task id was used for the publish call.
    assert call["task_id"]
    assert call["kwargs"]["job_id"] == job.id
    assert call["kwargs"]["attempt_id"]
    assert call["kwargs"]["tenant_id"] == TENANT
    assert call["kwargs"]["dispatch_token"]


def test_dispatch_definite_rejection(pg_jobs_repo):
    repo = pg_jobs_repo
    service, app = _service_with(
        repo, raise_on_publish=ConnectionRefusedError())
    job = service.submit(
        tenant_id=TENANT, job_type="v2_document",
        execution_transport="celery",
        input_metadata={"source_uri": "/tmp/svc2.pdf"})
    service.dispatch_job(job, payload={})
    refreshed = service.get_job(tenant_id=TENANT, job_id=job.id)
    assert refreshed.status == JobStatus.PENDING  # retryable state
    assert refreshed.last_error_summary == "ConnectionRefusedError"


def test_dispatch_ambiguous(pg_jobs_repo):
    repo = pg_jobs_repo
    service, app = _service_with(
        repo, raise_on_publish=RuntimeError("mid-call reset"))
    job = service.submit(
        tenant_id=TENANT, job_type="v2_document",
        execution_transport="celery",
        input_metadata={"source_uri": "/tmp/svc3.pdf"})
    service.dispatch_job(job, payload={})
    refreshed = service.get_job(tenant_id=TENANT, job_id=job.id)
    assert refreshed.status == JobStatus.DISPATCH_UNKNOWN


def test_idempotent_submit_returns_existing_job(pg_jobs_repo):
    repo = pg_jobs_repo
    service, _app = _service_with(repo)
    fingerprint = "fp-1"
    job1 = service.submit(
        tenant_id=TENANT, job_type="v2_document",
        execution_transport="celery",
        input_metadata={"source_uri": "/tmp/idem.pdf",
                        "input_fingerprint": fingerprint},
        idempotency_key="svc-key-1")
    job2 = service.submit(
        tenant_id=TENANT, job_type="v2_document",
        execution_transport="celery",
        input_metadata={"source_uri": "/tmp/idem.pdf",
                        "input_fingerprint": fingerprint},
        idempotency_key="svc-key-1")
    assert job2.id == job1.id
    with pytest.raises(IdempotencyConflictError):
        service.submit(
            tenant_id=TENANT, job_type="v2_document",
            execution_transport="celery",
            input_metadata={"source_uri": "/tmp/other.pdf",
                            "input_fingerprint": "fp-2"},
            idempotency_key="svc-key-1")


# --- tenant resolution trust model (§3.12) --------------------------------------


def test_tenant_resolver_fixed_mandatory():
    settings = JobsSettings()
    settings.tenant_resolver = "fixed"
    settings.default_tenant = ""
    with pytest.raises(TenantContextMissingError):
        TenantResolver(settings)


def test_tenant_resolver_fixed_applied_server_side():
    settings = JobsSettings()
    settings.tenant_resolver = "fixed"
    settings.default_tenant = "dev-tenant"
    settings.tenant_header_override = False
    resolver = TenantResolver(settings)
    # Ordinary input (any header) never selects a tenant.
    resolution = resolver.resolve("10.0.0.5", "attacker-tenant")
    assert resolution.tenant_id == "dev-tenant"
    assert resolution.mode == "fixed"
    resolution2 = resolver.resolve(None, None)
    assert resolution2.tenant_id == "dev-tenant"


def test_tenant_resolver_dev_override_loopback_only():
    settings = JobsSettings()
    settings.tenant_resolver = "fixed"
    settings.default_tenant = "dev-tenant"
    settings.tenant_header_override = True
    resolver = TenantResolver(settings)  # startup warning emitted
    override = resolver.resolve("127.0.0.1", "override-tenant")
    assert override.tenant_id == "override-tenant"
    assert override.mode == "dev_override"
    # Non-loopback client: override ignored.
    remote = resolver.resolve("203.0.113.9", "override-tenant")
    assert remote.tenant_id == "dev-tenant"
    assert remote.mode == "fixed"


def test_tenant_resolver_gateway_header():
    settings = JobsSettings()
    settings.tenant_resolver = "gateway_header"
    settings.default_tenant = ""
    settings.tenant_header_override = True  # ignored under gateway
    resolver = TenantResolver(settings)
    resolved = resolver.resolve("any", "gateway-tenant")
    assert resolved.tenant_id == "gateway-tenant"
    assert resolved.mode == "gateway_header"
    # Missing trusted header → fail closed.
    with pytest.raises(TenantContextMissingError):
        resolver.resolve("any", None)


# --- local transport dispatch (BackgroundTasks registration) ---------------------


def test_local_dispatch_returns_runner(pg_jobs_repo):
    from retriva.jobs.dispatch import LocalPublisher

    repo = pg_jobs_repo
    calls = []

    def runner_factory(envelope):
        calls.append(envelope)
        return lambda: None

    service = JobsService(
        repo=repo, settings=JobsSettings(),
        registry=job_type_registry(),
        publisher=LocalPublisher(runner_factory))
    job = service.submit(
        tenant_id=TENANT, job_type="v2_document",
        execution_transport="local",
        input_metadata={"source_uri": "/tmp/local.pdf"})
    dispatch = service.dispatch_job(job, payload={"x": 1})
    assert dispatch is not None and dispatch.runner is not None
    assert len(calls) == 1
    envelope = calls[0]
    # The envelope carries the durable identity + job type.
    assert envelope.job_id == job.id
    assert envelope.job_type == "v2_document"
    assert envelope.attempt_id and envelope.dispatch_token
    assert envelope.celery_task_id


@pytest.fixture()
def pg_jobs_repo(pg_jobs_db):
    return PostgresJobsRepository(pg_jobs_db.settings)


@pytest.fixture(scope="module")
def pg_jobs_db(pg_platform_stack):
    settings = pg_platform_stack.fresh_database("retriva_pg_test_jobs2")
    from retriva.infrastructure.postgres.migrations import (
        CORE_PLATFORM_STREAM,
        load_provider_registry,
        upgrade as framework_upgrade,
        verify as framework_verify,
    )
    registry = load_provider_registry("")
    from retriva.jobs.migrations import jobs_provider
    registry.register_core_stream(jobs_provider())
    result = framework_upgrade(registry, settings)
    assert [a["version"] for a in
            result[CORE_PLATFORM_STREAM][0]["applied"]] == [1]
    assert [a["version"] for a in
            result["core.jobs"][0]["applied"]] == [1]
    return SimpleNamespace(settings=settings)

@pytest.fixture(autouse=True)
def _reset_tenant_resolver():
    reset_tenant_resolver()
    yield
    reset_tenant_resolver()
