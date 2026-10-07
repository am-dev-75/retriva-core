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

"""Spec 030 / ADR-035: canonical upload idempotency + temp-file lifecycle."""

from __future__ import annotations

import os
import threading
import uuid
from types import SimpleNamespace

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from retriva.infrastructure.postgres.migrations import (  # noqa: E402
    CORE_PLATFORM_STREAM,
    load_provider_registry,
    upgrade as framework_upgrade,
)
from retriva.ingestion_api.durable_jobs import (  # noqa: E402
    UPLOAD_INPUT_SCHEMA,
    canonical_upload_identity,
    upload_input_fingerprint,
)
from retriva.ingestion_api.upload_temp import UploadTempFile  # noqa: E402
from retriva.jobs.config import JobsSettings  # noqa: E402
from retriva.jobs.dispatch import (  # noqa: E402
    PublishResult, PublicationOutcome,
)
from retriva.jobs.errors import IdempotencyConflictError  # noqa: E402
from retriva.jobs.registry import job_type_registry  # noqa: E402
from retriva.jobs.repository import PostgresJobsRepository  # noqa: E402
from retriva.jobs.service import JobsService  # noqa: E402

TENANT = "tenant-upidem"
DB = "retriva_pg_test_upidem"

BASE = dict(
    source_uri="specimen.txt", content_type="text/plain",
    user_metadata={"kb_ids": ["default"], "b": "2", "a": "1"},
    parser_hint=None, doc_id="doc-x", content_hash="sha256:" + "a" * 64,
    kb_id="default", source_paths=["specimen.txt"], content_size=42,
    ingestion_status="completed", collection_name=None)


def _fp(overrides=None, tenant=TENANT):
    payload = dict(BASE)
    payload.update(overrides or {})
    return upload_input_fingerprint(payload, tenant_id=tenant)


# -- canonical identity ------------------------------------------------------

def test_fingerprint_excludes_temp_path_and_created_at():
    a = _fp({"temp_path": "/tmp/A-1", "created_at": "2026-01-01T00:00:00Z"})
    b = _fp({"temp_path": "/tmp/B-2", "created_at": "2026-01-02T03:04:05Z"})
    assert a == b


def test_fingerprint_metadata_order_and_defaults():
    a = _fp({"user_metadata": {"a": "1", "b": "2"},
             "temp_path": "/t/A", "created_at": "x"})
    b = _fp({"user_metadata": {"b": "2", "a": "1"},
             "temp_path": "/t/Z", "created_at": "y"})
    assert a == b
    # schema is part of the hashed identity
    assert canonical_upload_identity(BASE, tenant_id=TENANT)["schema"] \
        == UPLOAD_INPUT_SCHEMA


def test_fingerprint_differs_on_material_change():
    assert _fp({"content_hash": "sha256:" + "b" * 64}) != _fp()
    assert _fp({"kb_id": "other"}) != _fp()
    assert _fp({"user_metadata": {"kb_ids": ["default"], "a": "9"}}) != _fp()
    assert _fp(tenant="tenant-other") != _fp()
    assert _fp({"source_uri": "other.txt"}) != _fp()


# -- worker payload contract (regression: payload_version must NOT leak) -----

def test_upload_payload_has_no_payload_version_and_binds_local_handler():
    import inspect
    from retriva.ingestion_api.durable_jobs import _upload_job_payload
    from retriva.ingestion_api.routers.v2_documents import (
        process_document_v2)
    payload = _upload_job_payload(
        source_path="f.txt", content_type="text/plain",
        user_metadata={"kb_ids": ["default"]}, parser_hint=None,
        temp_path="/t/x", doc_id="d", content_hash="sha256:" + "a" * 64,
        kb_id="default", source_paths=["f.txt"], content_size=1,
        ingestion_status="completed", created_at="x", collection_name=None)
    assert "payload_version" not in payload
    params = set(inspect.signature(process_document_v2).parameters)
    # collection_name is popped by the handler adapters before the call.
    unexpected = set(payload) - params - {"collection_name"}
    assert unexpected == set(), f"unexpected handler kwargs: {unexpected}"


def test_upload_payload_binds_celery_task_signature():
    import inspect
    from retriva.ingestion_api.durable_jobs import _upload_job_payload
    from celery import Celery
    from retriva.ingestion_api.tasks import _register_tasks
    app = Celery("sigcheck")
    _register_tasks(app)
    task = app.tasks["retriva.ingestion_api.tasks.process_document_task"]
    params = set(inspect.signature(task.run).parameters)
    payload = _upload_job_payload(
        source_path="f.txt", content_type="text/plain",
        user_metadata=None, parser_hint=None, temp_path="/t/x", doc_id="d",
        content_hash="sha256:" + "a" * 64, kb_id="default",
        source_paths=["f.txt"], content_size=1,
        ingestion_status="completed", created_at="x", collection_name=None)
    auth = {"job_id", "attempt_id", "tenant_id", "dispatch_token",
            "celery_task_id"}
    unexpected = set(payload) - params - auth
    assert "payload_version" not in payload
    assert unexpected == set(), f"unexpected task kwargs: {unexpected}"


def test_kb_ids_canonicalized_as_sorted_dedup_set():
    a = _fp({"user_metadata": {"kb_ids": ["b", "a", "b"]}})
    b = _fp({"user_metadata": {"kb_ids": ["a", "b"]}})
    assert a == b  # order- and duplicate-insensitive
    c = _fp({"user_metadata": {"kb_ids": ["a", "c"]}})
    assert a != c  # membership matters (no silent drop)


def test_nested_metadata_recursively_canonicalized():
    a = _fp({"user_metadata": {"tags": ["x", "y"],
                               "nested": {"b": 2, "a": 1}}})
    b = _fp({"user_metadata": {"nested": {"a": 1, "b": 2},
                               "tags": ["y", "x"]}})
    assert a == b  # nested dict/list preserved and canonicalized
    assert a != _fp({"user_metadata": {"tags": ["x"]}})


# -- UploadTempFile ownership ------------------------------------------------

def test_tempfile_lifecycle_idempotent_and_missing_safe(tmp_path):
    root = str(tmp_path / "tmp")
    owned = UploadTempFile.create(b"hello", suffix=".txt", root=root)
    assert os.path.exists(owned.path)
    assert owned.release() is True
    assert not os.path.exists(owned.path)
    assert owned.release() is False  # idempotent
    assert UploadTempFile.cleanup(owned.path, root=root) is False  # missing


def test_tempfile_transfer_prevents_request_cleanup(tmp_path):
    root = str(tmp_path / "tmp")
    owned = UploadTempFile.create(b"x", suffix=".txt", root=root)
    path = owned.transfer()
    assert owned.transferred
    assert owned.release() is False
    assert os.path.exists(path)  # worker now owns it
    UploadTempFile.cleanup(path, root=root)


def test_tempfile_refuses_out_of_root_and_symlink(tmp_path):
    root = str(tmp_path / "tmp")
    os.makedirs(root, exist_ok=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    assert UploadTempFile.cleanup(str(outside), root=root) is False
    assert outside.exists()
    link = os.path.join(root, "link.txt")
    try:
        os.symlink(str(outside), link)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert UploadTempFile.cleanup(link, root=root) is False
    assert outside.exists()


def test_context_manager_releases_on_error(tmp_path):
    root = str(tmp_path / "tmp")
    owned = UploadTempFile.create(b"x", suffix=".txt", root=root)
    path = owned.path
    with pytest.raises(RuntimeError):
        with owned:
            raise RuntimeError("boom")
    assert not os.path.exists(path)


# -- durable idempotency semantics (real PostgreSQL) -------------------------

@pytest.fixture(scope="module")
def up_db(pg_platform_stack):
    settings = pg_platform_stack.fresh_database(DB)
    registry = load_provider_registry("")
    from retriva.jobs.migrations import jobs_provider
    registry.register_core_stream(jobs_provider())
    result = framework_upgrade(registry, settings)
    assert [a["version"] for a in
            result[CORE_PLATFORM_STREAM][0]["applied"]] == [1]
    return settings


@pytest.fixture()
def service(up_db) -> JobsService:
    class Confirmed:
        def publish(self, envelope, queue=None):
            return PublishResult(PublicationOutcome.CONFIRMED)

    return JobsService(repo=PostgresJobsRepository(up_db),
                       settings=JobsSettings(),
                       registry=job_type_registry(),
                       publisher=Confirmed())


def _submit(service, payload, key):
    return service.submit(
        tenant_id=TENANT, job_type="v2_upload", execution_transport="local",
        input_metadata=dict(payload, input_fingerprint=_fp(payload)),
        idempotency_key=key, subject_type="document", subject_id="doc-x",
        _with_status=True)


def test_equivalent_duplicate_reuses_one_job(service):
    key = "v2up:reuse-" + uuid.uuid4().hex
    job1, created1 = _submit(service, dict(BASE, temp_path="/t/A",
                                           created_at="2026-01-01T00:00:00Z"),
                             key)
    job2, created2 = _submit(service, dict(BASE, temp_path="/t/B",
                                           created_at="2026-01-02T00:00:00Z"),
                             key)
    assert created1 is True
    assert created2 is False
    assert job1.id == job2.id


def test_same_key_different_identity_conflicts(service):
    key = "v2up:conflict-" + uuid.uuid4().hex
    _submit(service, dict(BASE, temp_path="/t/A", created_at="x"), key)
    with pytest.raises(IdempotencyConflictError):
        _submit(service, dict(BASE, content_hash="sha256:" + "b" * 64,
                              temp_path="/t/B", created_at="y"), key)


def test_different_key_same_content_creates_distinct_job(service):
    payload = dict(BASE, temp_path="/t/A", created_at="x")
    job1, c1 = _submit(service, payload, "v2up:k1-" + uuid.uuid4().hex)
    job2, c2 = _submit(service, payload, "v2up:k2-" + uuid.uuid4().hex)
    assert c1 and c2
    assert job1.id != job2.id


def test_concurrent_equivalent_duplicates_one_job(service):
    key = "v2up:race-" + uuid.uuid4().hex
    results = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        try:
            job, created = _submit(
                service, dict(BASE, temp_path="/t/" + uuid.uuid4().hex,
                              created_at=uuid.uuid4().hex), key)
            created = False if not created else True
            results.append(("ok", job.id, created))
        except IdempotencyConflictError:
            results.append(("conflict", None, None))
        except Exception as exc:  # noqa: BLE001
            results.append((type(exc).__name__, None, None))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    ok = [r for r in results if r[0] == "ok"]
    assert len(ok) == 2, results
    ids = {r[1] for r in ok}
    assert len(ids) == 1, f"expected one durable job, got {ids}"
    assert sum(1 for r in ok if r[2]) == 1, results


# -- HTTP 409 mapping --------------------------------------------------------

def test_idempotency_conflict_maps_to_409():
    import asyncio
    from retriva.ingestion_api.main import _idempotency_conflict_handler
    resp = asyncio.run(_idempotency_conflict_handler(
        None, IdempotencyConflictError("x")))
    assert resp.status_code == 409
    body = resp.body.decode()
    assert "upload_idempotency_conflict" in body
    for leaked in ("Traceback", "SELECT", "temp_path", "sha256"):
        assert leaked not in body