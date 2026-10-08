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
# implied.  See the License for the specific language governing permissions
# and limitations under the License.

"""Deterministic coverage for the MediaWiki changed-content correction.

Root cause A: changed-content replacement reused the prior version's
deterministic point ids (``canonical_doc_id``-derived), violating the
tenant-wide unique ``version_chunks.point_id`` at ``record_intent``.

Root cause B: durable retry rescheduling published an empty task payload
and a deterministic pre-publication ``TypeError`` was classified as
ambiguous (``dispatch_unknown``).

Root cause C: parse-temp files were retained for every non-success with
no terminal-path reaper.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import psycopg2
import pytest

from retriva.domain.models import ParsedDocument
from retriva.ingestion.chunker import create_chunks
from retriva.ingestion_api.upload_temp import UploadTempFile, release_job_staged_temp
from retriva.jobs.config import JobsSettings
from retriva.jobs.dispatch import (
    CeleryPublisher,
    PublicationOutcome,
    classify_publish_exception,
)
from retriva.jobs.domain import ERROR_CODE_UNCLASSIFIED, JobStatus, SanitizedError
from retriva.jobs.registry import job_type_registry
from retriva.jobs.repository import PostgresJobsRepository
from retriva.jobs.service import JobsService, task_payload_from_metadata
from retriva.knowledge.pipeline import KnowledgePipeline
from retriva.knowledge.repository import KnowledgeRepository

TENANT = "mw-cc-tenant"
COLL = "cust_iso_cc"
CANON = "mediawiki:localwiki:page:2"
SRC = "mediawiki/localwiki/page/2"


# ── A: changed-content replacement point ids ────────────────────────────────


def _seed_and_doc(pipe, fingerprint, text, job_id):
    """Emulate the worker path: the route copies the persisted version
    seed onto the ParsedDocument before chunking."""
    ctx = pipe.begin_upload(
        tenant_id=TENANT, kb_id="default", source_path=SRC,
        filename="page-2", collection_name=COLL, job_id=job_id,
        content_fingerprint=fingerprint)
    real = pipe.context_for_job(TENANT, job_id, "v2_upload")
    doc = ParsedDocument(source_path=SRC, canonical_doc_id=CANON,
                         page_title="t", content_text=text)
    if real is not None and real.chunk_id_seed:
        doc.chunk_id_seed = real.chunk_id_seed
    return ctx, doc


def test_replacement_point_ids_distinct_with_version_seed(knowledge_repo):
    pipe = KnowledgePipeline(repository=knowledge_repo)
    ctx1, doc1 = _seed_and_doc(pipe, "sha256:" + "a" * 64, "alpha " * 40, "j1")
    ids1 = [c.metadata.chunk_id for c in create_chunks(doc1)]
    assert pipe.record_intent(ctx1, ids1) is not None

    ctx2, doc2 = _seed_and_doc(pipe, "sha256:" + "b" * 64,
                               "beta gamma " * 40, "j2")
    ids2 = [c.metadata.chunk_id for c in create_chunks(doc2)]

    assert ctx1.version_id != ctx2.version_id
    # The prior version's manifest is untouched and the replacement
    # uses a distinct deterministic point id set (no unique violation).
    assert not (set(ids1) & set(ids2))
    assert pipe.record_intent(ctx2, ids2) is not None

    with knowledge_repo.transaction(TENANT) as cur:
        cur.execute(
            "SELECT version_id, count(*) AS n FROM knowledge.version_chunks "
            "WHERE tenant_id=%s GROUP BY version_id", (TENANT,))
        counts = {r["version_id"]: r["n"] for r in cur.fetchall()}
    assert counts[ctx1.version_id] == len(ids1)
    assert counts[ctx2.version_id] == len(ids2)


def test_chunker_without_seed_keeps_legacy_point_ids():
    doc = ParsedDocument(source_path=SRC, canonical_doc_id=CANON,
                         page_title="t", content_text="alpha " * 40)
    import hashlib
    expected = hashlib.md5(
        f"{CANON}_0".encode("utf-8")).hexdigest()
    assert create_chunks(doc)[0].metadata.chunk_id == expected


# ── B: retry payload + publication classification ───────────────────────────


def test_classify_typeerror_is_definite_rejection():
    assert classify_publish_exception(
        TypeError("missing required argument")) \
        == PublicationOutcome.DEFINITELY_REJECTED


def test_classify_runtimeerror_stays_ambiguous():
    assert classify_publish_exception(
        RuntimeError("mid-call reset")) == PublicationOutcome.AMBIGUOUS


class _FakeTask:
    def __init__(self, app, exc):
        self._app, self._exc = app, exc

    def apply_async(self, kwargs=None, task_id=None, queue=None):
        self._app.calls.append({"kwargs": dict(kwargs or {}),
                                "task_id": task_id, "queue": queue})
        if self._exc is not None:
            raise self._exc


class _FakeApp:
    def __init__(self, exc=None):
        self.calls = []
        self.tasks = {
            "retriva.ingestion_api.tasks.process_document_task":
                _FakeTask(self, exc),
        }


@pytest.fixture()
def pg_jobs_repo(pg_jobs_db):
    return PostgresJobsRepository(pg_jobs_db.settings)


@pytest.fixture(scope="module")
def pg_jobs_db(pg_platform_stack):
    settings = pg_platform_stack.fresh_database("retriva_pg_test_mwcc")
    from retriva.infrastructure.postgres.migrations import (
        CORE_PLATFORM_STREAM, load_provider_registry,
        upgrade as framework_upgrade,
    )
    registry = load_provider_registry("")
    from retriva.jobs.migrations import jobs_provider
    registry.register_core_stream(jobs_provider())
    result = framework_upgrade(registry, settings)
    assert [a["version"]
            for a in result[CORE_PLATFORM_STREAM][0]["applied"]] == [1]
    return SimpleNamespace(settings=settings)


def _document_metadata():
    return {
        "source_uri": "/synthetic/page.txt", "content_type": "text/plain",
        "user_metadata": {"k": "v"}, "parser_hint": None,
        "temp_path": "/staging/tmp.json", "doc_id": "docX",
        "content_hash": "sha256:" + "c" * 64, "kb_id": "default",
        "source_paths": ["/synthetic/page.txt"], "content_size": 12,
        "ingestion_status": "completed", "created_at": "2026-01-01T00:00:00Z",
        "collection_name": None, "input_fingerprint": "internal-value",
    }


def test_typeerror_publication_is_definitely_rejected(pg_jobs_repo):
    repo = pg_jobs_repo
    app = _FakeApp(TypeError("missing required positional arguments"))
    service = JobsService(
        repo=repo, settings=JobsSettings(), registry=job_type_registry(),
        publisher=CeleryPublisher(lambda: app))
    job = service.submit(tenant_id=TENANT, job_type="v2_document",
                         execution_transport="celery",
                         input_metadata=_document_metadata())
    service.dispatch_job(job, payload={})
    refreshed = service.get_job(tenant_id=TENANT, job_id=job.id)
    assert refreshed.status == JobStatus.PENDING  # definitive, retryable
    assert refreshed.status != JobStatus.DISPATCH_UNKNOWN


def test_reschedule_due_reconstructs_task_payload(pg_jobs_repo):
    repo = pg_jobs_repo
    app = _FakeApp()
    service = JobsService(
        repo=repo, settings=JobsSettings(), registry=job_type_registry(),
        publisher=CeleryPublisher(lambda: app))
    job = service.submit(tenant_id=TENANT, job_type="v2_document",
                         execution_transport="celery",
                         input_metadata=_document_metadata())
    app.calls.clear()
    attempt = repo.prepare_dispatch(
        tenant_id=TENANT, job_id=job.id, dispatch_token="tok",
        celery_task_id="task-1", attempt_id="att-1")
    repo.record_publication_inflight(
        tenant_id=TENANT, job_id=job.id, attempt_id=attempt.id)
    repo.record_publication_confirmed(
        tenant_id=TENANT, job_id=job.id, attempt_id=attempt.id)
    repo.claim_for_delivery(
        tenant_id=TENANT, job_id=job.id, attempt_id=attempt.id,
        dispatch_token="tok", celery_task_id="task-1", worker_id="w1")
    decision = repo.complete_failure(
        tenant_id=TENANT, job_id=job.id, attempt_id=attempt.id,
        error=SanitizedError(code=ERROR_CODE_UNCLASSIFIED,
                             summary="KnowledgeRepositoryError"),
        retryable=True)
    assert decision.retry_scheduled
    refreshed = repo.get_job(tenant_id=TENANT, job_id=job.id)
    assert refreshed.status == JobStatus.RETRY_WAIT
    _backdate(pg_jobs_repo, job.id)
    app.calls.clear()
    assert service.reschedule_due(job.id, TENANT) is True
    assert len(app.calls) == 1
    kwargs = app.calls[0]["kwargs"]
    # The retry publish carries the accepted task payload (no empty
    # payload) and excludes internal metadata.
    assert kwargs.get("source_uri") == "/synthetic/page.txt"
    assert kwargs.get("content_hash") == "sha256:" + "c" * 64
    assert "input_fingerprint" not in kwargs
    expected = task_payload_from_metadata(
        repo.get_job(tenant_id=TENANT, job_id=job.id))
    for key, value in expected.items():
        assert kwargs.get(key) == value


def _backdate(repo, job_id):
    import psycopg2
    settings = repo._settings
    conn = psycopg2.connect(
        host=settings.host, port=settings.port, dbname=settings.database,
        user=settings.admin_user,
        password=settings.admin_password.get_secret_value())
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs.jobs SET scheduled_at = now() - "
                "interval '5 minutes' WHERE id = %s", (job_id,))
    finally:
        conn.close()


# ── C: staged temp ownership ────────────────────────────────────────────────


def test_release_staged_temp_idempotent_missing_safe(tmp_path, monkeypatch):
    import retriva.ingestion_api.upload_temp as ut
    monkeypatch.setattr(ut, "staging_root", lambda: str(tmp_path))
    staged = tmp_path / "tmpABC.txt"
    staged.write_bytes(b"x")
    meta = {"temp_path": str(staged)}
    assert release_job_staged_temp(meta) is True
    assert not staged.exists()
    assert release_job_staged_temp(meta) is False  # missing-safe
    assert release_job_staged_temp({}) is False
    assert release_job_staged_temp(None) is False


def test_release_refuses_out_of_root_and_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"x")
    assert UploadTempFile.cleanup(str(outside), root=str(root)) is False
    assert outside.exists()

    target = root / "target.txt"
    target.write_bytes(b"x")
    link = root / "link.txt"
    os.symlink(target, link)
    assert UploadTempFile.cleanup(str(link), root=str(root)) is False
    assert target.exists()
