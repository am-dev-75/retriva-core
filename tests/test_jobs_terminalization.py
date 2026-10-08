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

"""Deterministic acceptance coverage for the Spec 033 / ADR-038 incident
terminalization mechanism (Option 2, private operator path).

Every scenario drives the REAL repository/service/CLI code against a real
isolated PostgreSQL scratch cluster (core.platform + core.jobs +
core.knowledge).  Synthetic incidents mirror the quarantined live structure
(``dispatch_unknown`` job, attempt #1 failed/published, attempt #2
queued/publication-unknown at execution_generation 1, failed ingestion,
non-current zero-chunk staging version, zero manifests/effects, one owned
parse temp, known-good current version) using repository/service calls only;
no live identifiers or content ever appear here.

Covered gates: dry-run fingerprinting and zero-write proof; the accepted
legal transitions (attempt QUEUED -> LOST; job DISPATCH_UNKNOWN ->
MANUAL_REVIEW -> FAILED) with durable audit; fail-closed preconditions with
zero partial writes/audit; lost-terminability and publication-unknown
evidence preservation; idempotency; real concurrency (operator/operator and
operator/worker) with deterministic barriers; CLI restrictions and
redaction; late Celery delivery fencing; reconciliation dry-run closure; and
downstream fail-ingestion / exactly-once parse-temp release sequencing.
"""

from __future__ import annotations

import ast
import json
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import psycopg2
import pytest

psycopg2 = pytest.importorskip("psycopg2")

from retriva.jobs import cli as cli_plumbing
from retriva.jobs import terminalize as terminalize_cli
from retriva.jobs.celery_integration import run_celery_durable_task
from retriva.jobs.config import JobsSettings
from retriva.jobs.domain import (
    ALLOWED_ATTEMPT_TRANSITIONS,
    ALLOWED_JOB_TRANSITIONS,
    TERMINAL_ATTEMPT_STATUSES,
    TERMINAL_JOB_STATUSES,
    AttemptStatus,
    EventActor,
    EventType,
    JobStatus,
    PublicationState,
    SanitizedError,
)
from retriva.jobs.errors import (
    InvalidTransitionError,
    JobsError,
)
from retriva.jobs.execution import execute_durable_job
from retriva.jobs.reconcile import reconcile
from retriva.jobs.registry import job_type_registry
from retriva.jobs.repository import PostgresJobsRepository
from retriva.jobs.service import JobsService
from retriva.knowledge.ids import upload_identity
from retriva.knowledge.repository import KnowledgeRepository
from retriva.knowledge.service import KnowledgeService

REASON = "operator_fail_clean_pre_fix_ambiguous_generation_no_effects"
REPO_SRC = (Path(__file__).resolve().parent.parent
            / "src" / "retriva" / "jobs" / "repository.py")

_GOOD_FP = "sha256:" + "a" * 64
_INCIDENT_FP = "sha256:" + "b" * 64


# ---------------------------------------------------------------------------
# Real-stack fixtures (scratch PostgreSQL; no Celery, no external providers)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def term_db(knowledge_database):
    return knowledge_database


@pytest.fixture()
def repo(term_db) -> PostgresJobsRepository:
    return PostgresJobsRepository(term_db)


@pytest.fixture()
def krepo(term_db) -> KnowledgeRepository:
    return KnowledgeRepository(term_db)


@pytest.fixture()
def kservice(term_db) -> KnowledgeService:
    return KnowledgeService(KnowledgeRepository(term_db))


class ConfirmedPublisher:
    def publish(self, envelope, queue=None):
        from retriva.jobs.dispatch import (
            PublicationOutcome,
            PublishResult,
        )
        return PublishResult(PublicationOutcome.CONFIRMED)


@pytest.fixture()
def service(term_db, repo) -> JobsService:
    return JobsService(
        repo=repo, settings=JobsSettings(),
        registry=job_type_registry(), publisher=ConfirmedPublisher())


# ---------------------------------------------------------------------------
# Synthetic incident helpers
# ---------------------------------------------------------------------------


def _tenant(tag: str) -> str:
    return f"term{tag}-{uuid.uuid4().hex[:10]}"


def _identity() -> tuple:
    return (uuid.uuid4().hex, uuid.uuid4().hex, str(uuid.uuid4()))


def _admin_connect(settings):
    return psycopg2.connect(
        host=settings.host, port=settings.port,
        dbname=settings.database, user=settings.admin_user,
        password=settings.resolved_password("admin"),
        connect_timeout=10)


def _admin_exec(settings, sql: str, params=None) -> None:
    conn = _admin_connect(settings)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
    finally:
        conn.close()


def _fetch(settings, sql: str, params=None):
    conn = _admin_connect(settings)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()
    finally:
        conn.close()


class Incident:
    """One synthetic quarantined generation (live-structure mirror)."""

    def __init__(self, *, tenant, job_id, attempts, document_id,
                 staging_version_id, staging_ingestion_id,
                 good_version_id, temp_path):
        self.tenant = tenant
        self.job_id = job_id
        self.attempts = attempts           # [attempt1(RETRY), attempt2]
        self.document_id = document_id
        self.staging_version_id = staging_version_id
        self.staging_ingestion_id = staging_ingestion_id
        self.good_version_id = good_version_id
        self.temp_path = temp_path

    @property
    def attempt1(self):
        return self.attempts[0]

    @property
    def attempt(self):
        """The selected queued/publication-unknown attempt (#2)."""
        return self.attempts[-1]


def _make_known_good(kservice, krepo, tenant):
    good = kservice.register_submission(
        tenant_id=tenant, identity=upload_identity(
            "default", "synthetic-page.txt"),
        kb_ids=["default"], collection_name="termcoll",
        job_id=f"good-{uuid.uuid4().hex}", job_type="v2_upload",
        content_fingerprint=_GOOD_FP, content_size=16)
    kservice.register_manifest(
        tenant_id=tenant, version_id=good.version_id,
        chunk_id_seed=f"{good.document_id}:{_GOOD_FP}", chunk_count=2)
    with krepo.transaction(tenant) as cur:
        ordinals = [r["chunk_ordinal"]
                    for r in krepo.list_chunk_states(
                        cur, tenant_id=tenant,
                        version_id=good.version_id)]
        krepo.set_chunks_sync_state(
            cur, tenant_id=tenant, version_id=good.version_id,
            ordinals=ordinals, sync_state="verified")
    assert kservice.finalize_verified(
        tenant_id=tenant, document_id=good.document_id,
        version_id=good.version_id, ingestion_id=good.ingestion_id,
        prior_version_id=None) is True
    return good


def _seed_incident(service, repo, kservice, settings, *,
                   temp_path: str) -> Incident:
    """Build the live-structure incident through real code paths."""
    tenant = _tenant("inc")
    good = _make_known_good(kservice, KnowledgeRepository(settings),
                            tenant)
    job_id = uuid.uuid4().hex
    # Changed-content resubmission -> same document, new staging version
    # with an ingestion correlated to the incident job.
    staging = kservice.register_submission(
        tenant_id=tenant, identity=upload_identity(
            "default", "synthetic-page.txt"),
        kb_ids=["default"], collection_name="termcoll",
        job_id=job_id, job_type="v2_document",
        content_fingerprint=_INCIDENT_FP, content_size=16,
        document_id=good.document_id)
    assert staging.document_id == good.document_id
    assert staging.version_id != good.version_id
    # The live incident has a failed ingestion whose target version is
    # still a zero-chunk non-current staging version.
    kservice.mark_ingestion_state(
        tenant_id=tenant, ingestion_id=staging.ingestion_id,
        sync_state="failed", error_code="parse_failed")

    job = service.submit(
        tenant_id=tenant, job_type="v2_document",
        execution_transport="celery", job_id=job_id,
        input_metadata={
            "source_uri": "/synthetic/page.txt",
            "content_hash": _INCIDENT_FP,
            "temp_path": temp_path,
        })

    # attempt #1: published -> claimed -> failed (retry scheduled)
    a1_id, tok1, task1 = _identity()
    assert repo.prepare_dispatch(
        tenant_id=tenant, job_id=job.id, dispatch_token=tok1,
        celery_task_id=task1, attempt_id=a1_id) is not None
    assert repo.record_publication_inflight(
        tenant_id=tenant, job_id=job.id, attempt_id=a1_id) is True
    assert repo.record_publication_confirmed(
        tenant_id=tenant, job_id=job.id, attempt_id=a1_id) is True
    from retriva.jobs.repository import ClaimOutcome
    claim = repo.claim_for_delivery(
        tenant_id=tenant, job_id=job.id, attempt_id=a1_id,
        dispatch_token=tok1, celery_task_id=task1,
        worker_id="celery:synthetic:1")
    assert claim.outcome == ClaimOutcome.GRANTED
    failure = repo.complete_failure(
        tenant_id=tenant, job_id=job.id, attempt_id=a1_id,
        error=SanitizedError(code="synthetic_failure",
                             summary="synthetic_failure"),
        retryable=True)
    assert failure is not None and failure.retry_scheduled
    _admin_exec(
        settings,
        "UPDATE jobs.jobs SET scheduled_at = now() - "
        "interval '5 minutes' WHERE id = %s", (job.id,))

    # attempt #2: queued + publication unknown (ambiguous publish)
    a2_id, tok2, task2 = _identity()
    a2 = repo.prepare_retry_dispatch(
        tenant_id=tenant, job_id=job.id, dispatch_token=tok2,
        celery_task_id=task2, attempt_id=a2_id)
    assert a2 is not None and a2.attempt_no == 2
    assert repo.record_publication_inflight(
        tenant_id=tenant, job_id=job.id, attempt_id=a2_id) is True
    assert repo.record_publication_ambiguous(
        tenant_id=tenant, job_id=job.id, attempt_id=a2_id,
        error=SanitizedError(code="publication_ambiguous",
                             summary="synthetic ambiguous publish")) is True

    job_row = repo.get_job(tenant_id=tenant, job_id=job.id)
    assert job_row.status == JobStatus.DISPATCH_UNKNOWN
    attempt2 = repo.get_attempt(tenant_id=tenant, attempt_id=a2_id)
    assert attempt2.status == AttemptStatus.QUEUED
    assert attempt2.publication_state == PublicationState.UNKNOWN
    assert attempt2.execution_generation == 1

    return Incident(
        tenant=tenant, job_id=job.id,
        attempts=[repo.get_attempt(tenant_id=tenant, attempt_id=a1_id),
                  attempt2],
        document_id=good.document_id,
        staging_version_id=staging.version_id,
        staging_ingestion_id=staging.ingestion_id,
        good_version_id=good.version_id,
        temp_path=temp_path)


def _dry_run(service, inc: Incident, **overrides):
    kwargs = dict(
        tenant_id=inc.tenant, job_id=inc.job_id,
        attempt_id=inc.attempt.id,
        expected_execution_generation=1,
        expected_publication_state="unknown",
        reason=REASON, dry_run=True)
    kwargs.update(overrides)
    return service.terminalize_no_effect_dispatch_unknown_generation(
        **{k: v for k, v in kwargs.items() if v is not None})


def _apply(service, inc: Incident, fingerprint: str, **overrides):
    kwargs = dict(
        tenant_id=inc.tenant, job_id=inc.job_id,
        attempt_id=inc.attempt.id,
        expected_execution_generation=1,
        expected_publication_state="unknown", reason=REASON,
        evidence_fingerprint=fingerprint, dry_run=False)
    kwargs.update(overrides)
    return service.terminalize_no_effect_dispatch_unknown_generation(
        **{k: v for k, v in kwargs.items() if v is not None})


def _fingerprint(service, inc: Incident) -> str:
    return _dry_run(service, inc)["evidence_fingerprint"]


def _snapshot(settings, inc: Incident) -> dict:
    jobs = _fetch(
        settings,
        "SELECT status, attempt_count, cancel_requested_at, finished_at, "
        "purge_after, updated_at FROM jobs.jobs WHERE id = %s",
        (inc.job_id,))
    attempts = _fetch(
        settings,
        "SELECT id, status, publication_state, execution_generation, "
        "finished_at, error_code, error_summary, updated_at "
        "FROM jobs.job_attempts WHERE job_id = %s ORDER BY attempt_no",
        (inc.job_id,))
    events = _fetch(
        settings,
        "SELECT count(*) AS n, coalesce(max(seq), 0) AS s "
        "FROM jobs.job_events WHERE job_id = %s", (inc.job_id,))
    ingestion = _fetch(
        settings,
        "SELECT sync_state, error_code FROM knowledge.ingestions "
        "WHERE ingestion_id = %s", (inc.staging_ingestion_id,))
    version = _fetch(
        settings,
        "SELECT status FROM knowledge.document_versions "
        "WHERE version_id = %s", (inc.staging_version_id,))
    chunks = _fetch(
        settings,
        "SELECT count(*) AS n FROM knowledge.version_chunks "
        "WHERE version_id = %s", (inc.staging_version_id,))
    ops = _fetch(
        settings,
        "SELECT count(*) AS n FROM knowledge.qdrant_operations "
        "WHERE ingestion_id = %s", (inc.staging_ingestion_id,))
    document = _fetch(
        settings,
        "SELECT current_version_id, serving_generation FROM "
        "knowledge.documents WHERE document_id = %s",
        (inc.document_id,))
    return {
        "jobs": jobs, "attempts": attempts, "events": events,
        "ingestion": ingestion, "version": version, "chunks": chunks,
        "ops": ops, "document": document,
    }


def _assert_no_partial_writes(settings, inc: Incident, before: dict):
    assert _snapshot(settings, inc) == before, (
        "a rejected path must leave zero partial writes")


def _expect_rejected(service, settings, inc: Incident, *,
                     contains: str, **overrides) -> JobsError:
    """Dry-run may fail closed (core preconditions) or emit the
    fingerprint; in both cases the apply path must reject with zero
    partial writes and zero audit side effects."""
    before = _snapshot(settings, inc)
    try:
        fp = _dry_run(service, inc, **overrides)["evidence_fingerprint"]
    except JobsError as exc:
        assert contains in str(exc), str(exc)
        _assert_no_partial_writes(settings, inc, before)
        return exc
    with pytest.raises(JobsError) as err:
        _apply(service, inc, fp, **overrides)
    assert contains in str(err.value), str(err.value)
    _assert_no_partial_writes(settings, inc, before)
    return err.value


def _events(settings, job_id) -> list:
    return _fetch(
        settings,
        "SELECT event_type, actor, from_status, to_status, attempt_id, "
        "detail FROM jobs.job_events WHERE job_id = %s ORDER BY seq",
        (job_id,))


def _event_counts(settings, job_id) -> dict:
    counts = {}
    for row in _events(settings, job_id):
        counts[row[0]] = counts.get(row[0], 0) + 1
    return counts


def _isolated_temp(tmp_path):
    """A synthetic staging root with one owned parse temp file."""
    root = tmp_path / "staging"
    root.mkdir(exist_ok=True)
    owned = root / f"parse-{uuid.uuid4().hex[:8]}.tmp"
    owned.write_bytes(b'{"synthetic": "page"}')
    return root, owned


# ---------------------------------------------------------------------------
# Dry-run and happy path
# ---------------------------------------------------------------------------


def test_dry_run_stable_fingerprint_and_zero_writes(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    before = _snapshot(term_db, inc)

    first = _dry_run(service, inc)
    assert first["outcome"] == "dry_run"
    assert len(first["evidence_fingerprint"]) == 64
    second = _dry_run(service, inc)
    assert second["evidence_fingerprint"] == first["evidence_fingerprint"]
    assert second["evidence"] == first["evidence"]

    ev = first["evidence"]
    assert ev["job_status"] == "dispatch_unknown"
    assert ev["attempt_status"] == "queued"
    assert ev["publication_state"] == "unknown"
    assert ev["execution_generation"] == 1
    assert ev["expected_generation"] == 1
    assert ev["attempt_no"] == 2
    assert ev["running_attempts"] == 0
    assert ev["later_attempts"] == 0
    assert ev["queued_attempts"] == 1
    assert ev["cancel_requested"] is False
    assert ev["chunks"] == 0
    assert ev["qdrant_operations"] == 0
    assert ev["ingestions"] == 1
    assert ev["ingestion_states"] == ["failed"]
    assert ev["version_statuses"] == ["staging"]
    assert ev["target_is_current"] is False
    assert ev["current_version_present"] is True

    # Zero writes: every durable row is byte-identical, no event added.
    _assert_no_partial_writes(term_db, inc, before)


def test_apply_terminalizes_with_legal_transitions_and_final_states(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    fp = _fingerprint(service, inc)

    result = _apply(service, inc, fp)
    assert result["outcome"] == "terminalized"
    assert result["job_status"] == "failed"
    assert result["attempt_status"] == "lost"

    job = repo.get_job(tenant_id=inc.tenant, job_id=inc.job_id)
    attempt = repo.get_attempt(tenant_id=inc.tenant,
                               attempt_id=inc.attempt.id)
    assert job.status == JobStatus.FAILED
    assert job.status in TERMINAL_JOB_STATUSES
    assert attempt.status == AttemptStatus.LOST
    assert attempt.status in TERMINAL_ATTEMPT_STATUSES
    assert job.finished_at is not None
    assert attempt.finished_at is not None
    # The bounded reason is persisted on the attempt.
    assert attempt.error_code == REASON[:64]
    assert attempt.error_summary == REASON


def test_apply_audit_events_exactly_once_and_correlated(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    events_before = len(_events(term_db, inc.job_id))
    fp = _fingerprint(service, inc)
    _apply(service, inc, fp)

    rows = _events(term_db, inc.job_id)
    new = rows[events_before:]
    types = [r[0] for r in new]
    assert types == ["attempt_lost", "operator_resolution",
                     "operator_resolution"]
    lost = new[0]
    assert lost[1] == EventActor.OPERATOR.value
    assert lost[4] == inc.attempt.id
    assert lost[5]["from"] == "queued" and lost[5]["to"] == "lost"
    assert lost[5]["reason"] == REASON
    assert lost[5]["evidence_fingerprint"] == fp
    t9, t22 = new[1], new[2]
    assert t9[2] == "dispatch_unknown" and t9[3] == "manual_review"
    assert t22[2] == "manual_review" and t22[3] == "failed"
    assert t9[5]["step"] == "T9" and t22[5]["step"] == "T22"
    assert t9[5]["evidence_fingerprint"] == fp
    assert t22[5]["evidence_fingerprint"] == fp
    # The observed no-effect evidence snapshot is durably embedded once.
    observed = t22[5]["observed"]
    assert observed["chunks"] == 0 and observed["qdrant_operations"] == 0
    assert observed["target_is_current"] is False
    # Exactly one attempt_lost and exactly two operator_resolution events
    # exist for the whole incident (no duplicate terminal effect).
    counts = _event_counts(term_db, inc.job_id)
    assert counts.get("attempt_lost") == 1
    assert counts.get("operator_resolution") == 2


def test_manual_review_intermediate_not_visible_before_commit(
        service, repo, kservice, term_db, tmp_path, monkeypatch):
    """The intermediate MANUAL_REVIEW state is durable audit evidence but
    never an actionable inter-transaction window: while the single apply
    transaction is still open, another connection observes the pre-apply
    state, and after commit the job is terminal FAILED."""
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    fp = _fingerprint(service, inc)

    reached_t9 = threading.Event()
    release_t9 = threading.Event()
    original = PostgresJobsRepository._record_event

    def gated(self, cur, **kwargs):
        original(self, cur, **kwargs)
        if (kwargs.get("event_type") == EventType.OPERATOR_RESOLUTION
                and (kwargs.get("detail") or {}).get("step") == "T9"):
            reached_t9.set()
            assert release_t9.wait(timeout=30)

    monkeypatch.setattr(PostgresJobsRepository, "_record_event", gated)

    box = {}

    def run():
        box["result"] = _apply(service, inc, fp)

    thread = threading.Thread(target=run)
    thread.start()
    assert reached_t9.wait(timeout=30), "T9 event was not reached"
    # The apply transaction is open (job row locked, T9 recorded but
    # uncommitted): a separate connection must not observe the
    # intermediate state.
    status = _fetch(
        term_db,
        "SELECT status FROM jobs.jobs WHERE id = %s", (inc.job_id,))
    assert status[0][0] == "dispatch_unknown"
    pending = _fetch(
        term_db,
        "SELECT count(*) FROM jobs.job_events WHERE job_id = %s "
        "AND event_type = 'operator_resolution'", (inc.job_id,))
    assert pending[0][0] == 0
    release_t9.set()
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert box["result"]["outcome"] == "terminalized"
    status = _fetch(
        term_db,
        "SELECT status FROM jobs.jobs WHERE id = %s", (inc.job_id,))
    assert status[0][0] == "failed"
    assert repo.get_job(tenant_id=inc.tenant,
                        job_id=inc.job_id).status == JobStatus.FAILED


def test_unrelated_rows_remain_unchanged(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    other_tenant = _tenant("other")
    other = _seed_incident(service, repo, kservice, term_db,
                           temp_path=str(owned))
    other_before = _snapshot(term_db, other)
    # Same-tenant decoy job in a different state.
    decoy = service.submit(
        tenant_id=inc.tenant, job_type="v2_document",
        execution_transport="celery",
        input_metadata={"source_uri": "/synthetic/decoy.txt"})
    decoy_events_before = _events(term_db, decoy.id)

    fp = _fingerprint(service, inc)
    _apply(service, inc, fp)

    assert _snapshot(term_db, other) == other_before
    decoy_after = repo.get_job(tenant_id=inc.tenant, job_id=decoy.id)
    assert decoy_after.status == JobStatus.PENDING
    assert _events(term_db, decoy.id) == decoy_events_before


# ---------------------------------------------------------------------------
# Preconditions and no-effect proof (each rejected case proves zero writes)
# ---------------------------------------------------------------------------


def test_reject_wrong_tenant(service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    with pytest.raises(JobsError) as err:
        _dry_run(service, inc, tenant_id=_tenant("wrong"))
    assert "not found" in str(err.value)
    # Apply with a foreign tenant fails closed too.
    with pytest.raises(JobsError):
        _apply(service, inc, "0" * 64, tenant_id=_tenant("wrong"))


def test_reject_unsupported_reason_at_both_layers(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    before = _snapshot(term_db, inc)
    with pytest.raises(JobsError) as err:
        _dry_run(service, inc, reason="some_other_reason")
    assert "unsupported reason" in str(err.value)
    with pytest.raises(JobsError) as err:
        repo.terminalize_no_effect_dispatch_unknown_generation(
            tenant_id=inc.tenant, job_id=inc.job_id,
            attempt_id=inc.attempt.id,
            expected_execution_generation=1,
            expected_publication_state="unknown",
            reason="some_other_reason",
            evidence_fingerprint="0" * 64)
    assert "unsupported reason" in str(err.value)
    _assert_no_partial_writes(term_db, inc, before)


def test_reject_wrong_job_state(service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    _admin_exec(term_db,
                "UPDATE jobs.jobs SET status = 'queued' WHERE id = %s",
                (inc.job_id,))
    _expect_rejected(service, term_db, inc,
                     contains="job is not dispatch_unknown")


def test_reject_wrong_attempt_state(service, repo, kservice, term_db,
                                    tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    _admin_exec(term_db,
                "UPDATE jobs.job_attempts SET status = 'cancelled' "
                "WHERE id = %s", (inc.attempt.id,))
    _expect_rejected(service, term_db, inc,
                     contains="attempt is not queued")


def test_reject_publication_state_not_unknown(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    _admin_exec(term_db,
                "UPDATE jobs.job_attempts SET publication_state = "
                "'published' WHERE id = %s", (inc.attempt.id,))
    _expect_rejected(service, term_db, inc,
                     contains="publication state mismatch")


def test_reject_attempt_belongs_to_another_job(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    other_job = service.submit(
        tenant_id=inc.tenant, job_type="v2_document",
        execution_transport="celery",
        input_metadata={"source_uri": "/synthetic/x.txt"})
    aid, tok, task = _identity()
    repo.prepare_dispatch(tenant_id=inc.tenant, job_id=other_job.id,
                          dispatch_token=tok, celery_task_id=task,
                          attempt_id=aid)
    with pytest.raises(JobsError) as err:
        _dry_run(service, inc, attempt_id=aid)
    assert "attempt not found" in str(err.value)


def test_reject_generation_mismatch(service, repo, kservice, term_db,
                                    tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    _expect_rejected(service, term_db, inc,
                     contains="generation mismatch",
                     expected_execution_generation=2)


def test_reject_later_attempt_exists(service, repo, kservice, term_db,
                                     tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    _admin_exec(
        term_db,
        "INSERT INTO jobs.job_attempts (id, job_id, tenant_id, attempt_no, "
        "dispatch_generation, dispatch_token, celery_task_id, "
        "publication_state, status, execution_generation) VALUES "
        "(%s, %s, %s, 3, 1, %s, %s, 'prepared', 'queued', 1)",
        (uuid.uuid4().hex, inc.job_id, inc.tenant, uuid.uuid4().hex,
         str(uuid.uuid4())))
    _expect_rejected(service, term_db, inc,
                     contains="later_attempt_exists")


def test_reject_later_generation_exists(service, repo, kservice, term_db,
                                        tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    # An earlier attempt number carrying a higher execution_generation
    # still means a later generation exists for this job.
    _admin_exec(term_db,
                "UPDATE jobs.job_attempts SET execution_generation = 2 "
                "WHERE id = %s", (inc.attempt1.id,))
    _expect_rejected(service, term_db, inc,
                     contains="later_attempt_exists")


def test_reject_running_claimant_or_active_claim(
        service, repo, kservice, term_db, tmp_path):
    """An active claim (durable claimant evidence: running attempt with a
    worker id) must fail the no-effect proof.  The durable jobs schema has
    no separate heartbeat/lease relation; the running attempt row is the
    authoritative claim evidence."""
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    _admin_exec(
        term_db,
        "UPDATE jobs.job_attempts SET status = 'running', worker_id = %s "
        "WHERE id = %s", ("celery:synthetic:9", inc.attempt1.id))
    _expect_rejected(service, term_db, inc,
                     contains="running_attempt_exists")


def test_reject_cancellation_pending(service, repo, kservice, term_db,
                                     tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    _admin_exec(term_db,
                "UPDATE jobs.jobs SET cancel_requested_at = now() "
                "WHERE id = %s", (inc.job_id,))
    _expect_rejected(service, term_db, inc,
                     contains="cancellation_pending")


def test_reject_pending_retry_reschedule_or_republish(
        service, repo, kservice, term_db, tmp_path):
    """A second queued claimant (retry/reschedule/republish pending)
    breaks the single-unclaimed-generation proof."""
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    _admin_exec(term_db,
                "UPDATE jobs.job_attempts SET status = 'queued', "
                "finished_at = NULL WHERE id = %s", (inc.attempt1.id,))
    _expect_rejected(service, term_db, inc,
                     contains="queued_attempt_count")


def test_reject_terminal_success_evidence(
        service, repo, kservice, term_db, tmp_path):
    """A durably succeeded attempt for the job is terminal-success
    evidence and must fail the no-effect proof."""
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    _admin_exec(
        term_db,
        "UPDATE jobs.job_attempts SET status = 'succeeded', "
        "finished_at = now() WHERE id = %s", (inc.attempt1.id,))
    _expect_rejected(service, term_db, inc,
                     contains="terminal success evidence")


def test_reject_committed_manifest_chunks_exist(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    _admin_exec(
        term_db,
        "INSERT INTO knowledge.version_chunks (tenant_id, version_id, "
        "chunk_ordinal, point_id, chunk_contract_version, sync_state) "
        "VALUES (%s, %s, 0, %s, 'synthetic-chunk/1', 'expected')",
        (inc.tenant, inc.staging_version_id, uuid.uuid4().hex))
    _expect_rejected(service, term_db, inc,
                     contains="chunks_exist")


def test_reject_qdrant_operation_or_business_effect_exists(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    _admin_exec(
        term_db,
        "INSERT INTO knowledge.qdrant_operations (op_id, tenant_id, "
        "ingestion_id, document_id, version_id, op_type, "
        "collection_name, op_state, attempt_no) VALUES "
        "(%s, %s, %s, %s, %s, 'upsert_batch', 'termcoll', 'prepared', 1)",
        (uuid.uuid4().hex, inc.tenant, inc.staging_ingestion_id,
         inc.document_id, inc.staging_version_id))
    _expect_rejected(service, term_db, inc,
                     contains="qdrant_operations_exist")


def test_reject_staging_version_is_current(service, repo, kservice,
                                           term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    _admin_exec(
        term_db,
        "UPDATE knowledge.documents SET current_version_id = %s "
        "WHERE document_id = %s",
        (inc.staging_version_id, inc.document_id))
    _expect_rejected(service, term_db, inc,
                     contains="target_is_current")


def test_reject_known_good_current_version_missing(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    _admin_exec(
        term_db,
        "UPDATE knowledge.documents SET current_version_id = NULL "
        "WHERE document_id = %s", (inc.document_id,))
    _expect_rejected(service, term_db, inc,
                     contains="no_known_good_current_version")


def test_reject_ingestion_state_incompatible(service, repo, kservice,
                                             term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    _admin_exec(
        term_db,
        "UPDATE knowledge.ingestions SET sync_state = 'parsing' "
        "WHERE ingestion_id = %s", (inc.staging_ingestion_id,))
    _expect_rejected(service, term_db, inc,
                     contains="ingestion_not_failed")


def test_reject_version_not_staging(service, repo, kservice, term_db,
                                    tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    _admin_exec(
        term_db,
        "UPDATE knowledge.document_versions SET status = 'failed' "
        "WHERE version_id = %s", (inc.staging_version_id,))
    _expect_rejected(service, term_db, inc,
                     contains="version_not_staging")


def test_reject_stale_fingerprint_after_state_change(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    fp = _fingerprint(service, inc)
    before = _snapshot(term_db, inc)
    # A concurrent, evidence-visible state change (a second ingestion row
    # correlated to the job) makes the previously emitted fingerprint
    # stale.
    _admin_exec(
        term_db,
        "INSERT INTO knowledge.ingestions (ingestion_id, tenant_id, "
        "job_id, job_type, document_id, target_version_id, "
        "collection_name, sync_state) VALUES "
        "(%s, %s, %s, 'v2_document', %s, %s, 'termcoll', 'failed')",
        (uuid.uuid4().hex, inc.tenant, inc.job_id, inc.document_id,
         inc.good_version_id))
    with pytest.raises(JobsError) as err:
        _apply(service, inc, fp)
    assert "fingerprint mismatch" in str(err.value)
    latest = _snapshot(term_db, inc)
    # No operator mutation happened; the only difference is the synthetic
    # concurrent ingestion row inserted by this test.
    assert latest["jobs"] == before["jobs"]
    assert latest["attempts"] == before["attempts"]
    assert latest["events"] == before["events"]


def test_reject_wrong_and_missing_fingerprint(service, repo, kservice,
                                              term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    before = _snapshot(term_db, inc)
    with pytest.raises(JobsError) as err:
        _apply(service, inc, "f" * 64)
    assert "fingerprint mismatch" in str(err.value)
    with pytest.raises(JobsError) as err:
        service.terminalize_no_effect_dispatch_unknown_generation(
            tenant_id=inc.tenant, job_id=inc.job_id,
            attempt_id=inc.attempt.id,
            expected_execution_generation=1,
            expected_publication_state="unknown", reason=REASON,
            dry_run=False)
    assert "requires the dry-run evidence fingerprint" in str(err.value)
    _assert_no_partial_writes(term_db, inc, before)


def test_reject_already_terminal_with_conflicting_fingerprint(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    fp = _fingerprint(service, inc)
    assert _apply(service, inc, fp)["outcome"] == "terminalized"
    before = _snapshot(term_db, inc)
    with pytest.raises(JobsError) as err:
        _apply(service, inc, "a" * 64)
    assert "conflicting evidence" in str(err.value)
    _assert_no_partial_writes(term_db, inc, before)


# ---------------------------------------------------------------------------
# Legal transition semantics
# ---------------------------------------------------------------------------


def test_no_direct_queued_to_failed_transition():
    from retriva.jobs.domain import assert_attempt_transition_allowed
    assert AttemptStatus.FAILED not in \
        ALLOWED_ATTEMPT_TRANSITIONS[AttemptStatus.QUEUED]
    with pytest.raises(InvalidTransitionError):
        assert_attempt_transition_allowed(
            AttemptStatus.QUEUED, AttemptStatus.FAILED)
    assert AttemptStatus.LOST in \
        ALLOWED_ATTEMPT_TRANSITIONS[AttemptStatus.QUEUED]


def test_no_direct_dispatch_unknown_to_failed_transition():
    assert JobStatus.FAILED not in \
        ALLOWED_JOB_TRANSITIONS[JobStatus.DISPATCH_UNKNOWN]
    from retriva.jobs.domain import assert_transition_allowed
    with pytest.raises(InvalidTransitionError):
        assert_transition_allowed(
            JobStatus.DISPATCH_UNKNOWN, JobStatus.FAILED)
    assert JobStatus.MANUAL_REVIEW in \
        ALLOWED_JOB_TRANSITIONS[JobStatus.DISPATCH_UNKNOWN]
    assert JobStatus.FAILED in \
        ALLOWED_JOB_TRANSITIONS[JobStatus.MANUAL_REVIEW]


def test_lost_preserves_publication_unknown_evidence(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    before = repo.get_attempt(tenant_id=inc.tenant,
                              attempt_id=inc.attempt.id)
    fp = _fingerprint(service, inc)
    _apply(service, inc, fp)
    after = repo.get_attempt(tenant_id=inc.tenant,
                             attempt_id=inc.attempt.id)
    assert after.status == AttemptStatus.LOST
    assert after.publication_state == PublicationState.UNKNOWN
    assert after.publication_tries == before.publication_tries
    assert after.dispatch_token == before.dispatch_token
    assert after.celery_task_id == before.celery_task_id


def test_lost_is_terminal_non_retryable_and_not_republishable(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    fp = _fingerprint(service, inc)
    _apply(service, inc, fp)

    attempt = repo.get_attempt(tenant_id=inc.tenant,
                               attempt_id=inc.attempt.id)
    assert attempt.status in TERMINAL_ATTEMPT_STATUSES
    # No retry state machine accepts a LOST attempt: it is a terminal
    # attempt status with no outgoing transitions.
    assert ALLOWED_ATTEMPT_TRANSITIONS[AttemptStatus.LOST] == frozenset()
    # A late delivery cannot claim it (terminal no-op).
    from retriva.jobs.repository import ClaimOutcome
    decision = repo.claim_for_delivery(
        tenant_id=inc.tenant, job_id=inc.job_id,
        attempt_id=inc.attempt.id,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id or attempt.id,
        worker_id="celery:synthetic:late", redelivered=True)
    assert decision.outcome == ClaimOutcome.TERMINAL_NOOP
    # Reconciliation never proposes a republish for the terminal job.
    report = reconcile(service, tenant_id=inc.tenant, privileged=False,
                       batch=50, apply=False)
    assert not any(item["job_id"] == inc.job_id for item in report["items"])


def test_existing_unrelated_lost_paths_unchanged():
    # The accepted transition table outside this mechanism is untouched.
    assert ALLOWED_ATTEMPT_TRANSITIONS[AttemptStatus.QUEUED] == frozenset({
        AttemptStatus.RUNNING, AttemptStatus.CANCELLED,
        AttemptStatus.DISPATCH_FAILED, AttemptStatus.LOST})
    assert AttemptStatus.LOST in \
        ALLOWED_ATTEMPT_TRANSITIONS[AttemptStatus.RUNNING]
    assert ALLOWED_JOB_TRANSITIONS[JobStatus.DISPATCH_UNKNOWN] == \
        frozenset({JobStatus.QUEUED, JobStatus.MANUAL_REVIEW,
                   JobStatus.CANCELLING})


# ---------------------------------------------------------------------------
# Idempotency and concurrency
# ---------------------------------------------------------------------------


def test_exact_repeat_is_idempotent_with_no_new_events(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    fp = _fingerprint(service, inc)
    assert _apply(service, inc, fp)["outcome"] == "terminalized"
    before = _snapshot(term_db, inc)

    replay = _apply(service, inc, fp)
    assert replay["outcome"] == "idempotent_replay"
    assert replay["job_status"] == "failed"
    assert replay["evidence_fingerprint"] == fp
    _assert_no_partial_writes(term_db, inc, before)


def test_conflicting_reason_fails_closed(service, repo, kservice, term_db,
                                         tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    fp = _fingerprint(service, inc)
    before = _snapshot(term_db, inc)
    with pytest.raises(JobsError):
        repo.terminalize_no_effect_dispatch_unknown_generation(
            tenant_id=inc.tenant, job_id=inc.job_id,
            attempt_id=inc.attempt.id,
            expected_execution_generation=1,
            expected_publication_state="unknown",
            reason=REASON + "_other",
            evidence_fingerprint=fp)
    _assert_no_partial_writes(term_db, inc, before)


def test_two_concurrent_operator_applies_produce_one_mutation(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    fp = _fingerprint(service, inc)
    pre = _event_counts(term_db, inc.job_id)

    barrier = threading.Barrier(2)
    results, errors = [], []

    def run():
        barrier.wait(timeout=30)
        try:
            results.append(_apply(service, inc, fp))
        except BaseException as exc:  # noqa: BLE001 - classified
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    for t in threads:
        assert not t.is_alive(), "stuck transaction"

    assert not errors, [(type(e).__name__, str(e)[:80]) for e in errors]
    outcomes = sorted(r["outcome"] for r in results)
    assert outcomes == ["idempotent_replay", "terminalized"]
    counts = _event_counts(term_db, inc.job_id)
    assert counts.get("attempt_lost") == pre.get("attempt_lost", 0) + 1
    assert counts.get("operator_resolution") == \
        pre.get("operator_resolution", 0) + 2
    # No new claim/success/failure event beyond the seeded incident.
    for event in ("attempt_claimed", "attempt_succeeded", "attempt_failed"):
        assert counts.get(event) == pre.get(event)
    assert repo.get_job(tenant_id=inc.tenant,
                        job_id=inc.job_id).status == JobStatus.FAILED


def test_worker_claim_wins_first_operator_fails_closed(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    attempt = inc.attempt
    from retriva.jobs.repository import ClaimOutcome
    decision = repo.claim_for_delivery(
        tenant_id=inc.tenant, job_id=inc.job_id,
        attempt_id=attempt.id, dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id,
        worker_id="celery:synthetic:winner")
    assert decision.outcome == ClaimOutcome.GRANTED

    before = _snapshot(term_db, inc)
    with pytest.raises(JobsError) as err:
        _dry_run(service, inc)
    assert "job is not dispatch_unknown" in str(err.value)
    with pytest.raises(JobsError):
        _apply(service, inc, "0" * 64)
    # No partial operator transition and no operator audit side effect.
    after = _snapshot(term_db, inc)
    assert after["jobs"] == before["jobs"]
    assert after["attempts"] == before["attempts"]
    assert after["events"] == before["events"]
    counts = _event_counts(term_db, inc.job_id)
    assert counts.get("attempt_lost") is None
    assert counts.get("operator_resolution") is None


def test_operator_wins_first_worker_cannot_claim_or_execute(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    fp = _fingerprint(service, inc)
    _apply(service, inc, fp)
    before = _snapshot(term_db, inc)

    calls = []

    def handler(**kwargs):  # pragma: no cover - must never run
        calls.append(kwargs)

    attempt = repo.get_attempt(tenant_id=inc.tenant,
                               attempt_id=inc.attempt.id)
    outcome = execute_durable_job(
        repo=repo, handler=handler, job_id=inc.job_id,
        attempt_id=attempt.id, tenant_id=inc.tenant,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id or attempt.id,
        worker_id="celery:synthetic:late", redelivered=True)
    assert outcome == "terminal_noop"
    assert calls == []
    _assert_no_partial_writes(term_db, inc, before)


def test_claim_versus_terminalize_race_no_deadlock(
        service, repo, kservice, term_db, tmp_path):
    """Deterministic barrier race between the real worker claim and the
    real operator apply; both lock jobs -> job_attempts and serialize."""
    rounds = 8
    for _ in range(rounds):
        _, owned = _isolated_temp(tmp_path)
        inc = _seed_incident(service, repo, kservice, term_db,
                             temp_path=str(owned))
        fp = _fingerprint(service, inc)
        attempt = inc.attempt
        pre = _event_counts(term_db, inc.job_id)
        barrier = threading.Barrier(2)
        errors = []

        def claim():
            barrier.wait(timeout=30)
            try:
                repo.claim_for_delivery(
                    tenant_id=inc.tenant, job_id=inc.job_id,
                    attempt_id=attempt.id,
                    dispatch_token=attempt.dispatch_token,
                    celery_task_id=attempt.celery_task_id,
                    worker_id="celery:synthetic:race", redelivered=True)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def operator():
            barrier.wait(timeout=30)
            try:
                _apply(service, inc, fp)
            except JobsError:
                pass  # fail-closed when the claim won the race
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=claim),
                   threading.Thread(target=operator)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        for t in threads:
            assert not t.is_alive(), "stuck transaction (deadlock?)"
        deadlocks = [e for e in errors
                     if isinstance(e, psycopg2.errors.DeadlockDetected)
                     or getattr(e, "pgcode", None) == "40P01"]
        assert not deadlocks, "job-before-attempt lock order violated"
        assert not errors, [(type(e).__name__, str(e)[:80])
                            for e in errors]

        job = repo.get_job(tenant_id=inc.tenant, job_id=inc.job_id)
        attempt_row = repo.get_attempt(tenant_id=inc.tenant,
                                       attempt_id=attempt.id)
        counts = _event_counts(term_db, inc.job_id)
        if job.status == JobStatus.FAILED:
            assert attempt_row.status == AttemptStatus.LOST
            assert counts.get("attempt_lost", 0) == \
                pre.get("attempt_lost", 0) + 1
            assert counts.get("attempt_claimed", 0) == \
                pre.get("attempt_claimed", 0)
        else:
            assert job.status == JobStatus.RUNNING
            assert attempt_row.status == AttemptStatus.RUNNING
            assert counts.get("attempt_claimed", 0) == \
                pre.get("attempt_claimed", 0) + 1
            assert counts.get("attempt_lost", 0) == \
                pre.get("attempt_lost", 0)


def test_duplicate_terminal_callback_is_harmless(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    fp = _fingerprint(service, inc)
    _apply(service, inc, fp)
    before = _snapshot(term_db, inc)

    # A late/duplicate terminal callback for the already-terminal attempt
    # is an idempotent no-op (never running -> refused).
    assert repo.complete_success(
        tenant_id=inc.tenant, job_id=inc.job_id,
        attempt_id=inc.attempt.id) is False
    failure = repo.complete_failure(
        tenant_id=inc.tenant, job_id=inc.job_id,
        attempt_id=inc.attempt.id,
        error=SanitizedError(code="late", summary="late"),
        retryable=True)
    assert failure is None
    _assert_no_partial_writes(term_db, inc, before)


def test_lock_order_and_bounded_scan_of_terminalization_source():
    """Static invariants: job-before-attempt locking and no unbounded
    scan inside the new mechanism."""
    tree = ast.parse(REPO_SRC.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef)
               and n.name == "PostgresJobsRepository")
    methods = {fn.name: fn for fn in cls.body
               if isinstance(fn, ast.FunctionDef)}

    def lock_events(fn):
        events = []
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr == "_lock_job_row":
                events.append((node.lineno, "jobs"))
            elif node.func.attr == "_lock_attempt_row":
                events.append((node.lineno, "attempts"))
            elif (node.func.attr == "execute" and node.args
                  and isinstance(node.args[0], ast.Constant)
                  and isinstance(node.args[0].value, str)):
                sql = node.args[0].value.strip()
                if sql.startswith("UPDATE jobs.jobs") or (
                        "jobs.jobs" in sql and "FOR UPDATE" in sql):
                    events.append((node.lineno, "jobs"))
                elif sql.startswith("UPDATE jobs.job_attempts") or (
                        "jobs.job_attempts" in sql
                        and "FOR UPDATE" in sql):
                    events.append((node.lineno, "attempts"))
        events.sort()
        return [kind for _, kind in events]

    for name in ("terminalize_no_effect_dispatch_unknown_generation",
                 "_no_effect_evidence", "no_effect_evidence"):
        rels = lock_events(methods[name])
        if "jobs" in rels and "attempts" in rels:
            assert rels.index("jobs") < rels.index("attempts"), (
                f"{name} acquires attempts before jobs: {rels}")

    for name in ("terminalize_no_effect_dispatch_unknown_generation",
                 "_no_effect_evidence"):
        fn = methods[name]
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                sql = node.args[0].value
                head = sql.strip().split()[0].upper()
                if head in ("SELECT", "UPDATE", "DELETE"):
                    assert "WHERE" in sql.upper(), (
                        f"unbounded statement in {name}: {sql[:60]}")


# ---------------------------------------------------------------------------
# CLI and exposure
# ---------------------------------------------------------------------------


def _cli_args(inc, *extra):
    return [
        "--tenant", inc.tenant, "--job-id", inc.job_id,
        "--attempt-id", inc.attempt.id, "--execution-generation", "1",
        "--reason", REASON, *extra,
    ]


def test_cli_dry_run_is_default(service, repo, kservice, term_db,
                                tmp_path, monkeypatch, capsys):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    monkeypatch.setattr(
        "retriva.ingestion_api.durable_jobs.jobs_service", lambda: service)
    before = _snapshot(term_db, inc)
    code = terminalize_cli.main(_cli_args(inc))
    assert code == cli_plumbing.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["outcome"] == "dry_run"
    assert len(out["evidence_fingerprint"]) == 64
    _assert_no_partial_writes(term_db, inc, before)


def test_cli_apply_requires_fingerprint_and_applies(
        service, repo, kservice, term_db, tmp_path, monkeypatch, capsys):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    monkeypatch.setattr(
        "retriva.ingestion_api.durable_jobs.jobs_service", lambda: service)

    code = terminalize_cli.main(_cli_args(inc, "--apply"))
    assert code == cli_plumbing.EXIT_FAILURE
    assert "fingerprint" in capsys.readouterr().out

    code = terminalize_cli.main(_cli_args(inc))
    fp = json.loads(capsys.readouterr().out)["evidence_fingerprint"]
    code = terminalize_cli.main(
        _cli_args(inc, "--apply", "--evidence-fingerprint", fp))
    assert code == cli_plumbing.EXIT_APPLIED
    out = json.loads(capsys.readouterr().out)
    assert out["outcome"] == "terminalized"
    job = repo.get_job(tenant_id=inc.tenant, job_id=inc.job_id)
    assert job.status == JobStatus.FAILED

    # Unsupported reason fails closed (no writes).
    code = terminalize_cli.main([
        "--tenant", inc.tenant, "--job-id", inc.job_id,
        "--attempt-id", inc.attempt.id, "--execution-generation", "1",
        "--reason", "not_the_bounded_reason"])
    assert code == cli_plumbing.EXIT_FAILURE
    assert "unsupported reason" in capsys.readouterr().out


def test_cli_exact_scope_inputs_are_mandatory():
    parser = terminalize_cli._parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    # Every scope input is individually required and typed.
    for missing in ("--tenant", "--job-id", "--attempt-id",
                    "--execution-generation", "--reason"):
        with pytest.raises(SystemExit):
            args = ["--tenant", "t", "--job-id", "j", "--attempt-id", "a",
                    "--execution-generation", "1", "--reason", REASON]
            idx = args.index(missing)
            parser.parse_args(args[:idx] + args[idx + 2:])


def test_cli_has_no_batch_wildcard_or_discovery_mode():
    import argparse
    parser = terminalize_cli._parser()
    options = set()
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        options.update(action.option_strings)
    assert options == {
        "--tenant", "--job-id", "--attempt-id", "--execution-generation",
        "--publication-state", "--reason", "--actor",
        "--evidence-fingerprint", "--dry-run", "--apply", "--json"}
    for forbidden in ("--all-tenants", "--batch", "--wildcard", "--scan",
                      "--discover", "--all"):
        assert forbidden not in options


def test_cli_json_output_is_stable_and_redacted(
        service, repo, kservice, term_db, tmp_path, monkeypatch, capsys):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    monkeypatch.setattr(
        "retriva.ingestion_api.durable_jobs.jobs_service", lambda: service)
    code = terminalize_cli.main(_cli_args(inc, "--json"))
    assert code == cli_plumbing.EXIT_OK
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert set(payload.keys()) == {"outcome", "evidence",
                                   "evidence_fingerprint"}
    # No identifiers, tenant values, or filesystem paths leak.
    assert inc.tenant not in out
    assert inc.job_id not in out
    assert inc.attempt.id not in out
    assert str(owned.parent) not in out
    assert "/" not in out
    # Stable across repeated dry-runs.
    code = terminalize_cli.main(_cli_args(inc, "--json"))
    assert json.loads(capsys.readouterr().out) == payload


def test_mechanism_is_not_exposed_through_tenant_apis():
    src_root = Path(__file__).resolve().parent.parent / "src" / "retriva"
    exposed_dirs = [src_root / "ingestion_api" / "routers",
                    src_root / "openai_api"]
    hits = []
    for directory in exposed_dirs:
        for path in directory.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "terminalize" in text:
                hits.append(str(path))
    assert hits == [], f"terminalize leaked into API surfaces: {hits}"


# ---------------------------------------------------------------------------
# Late delivery and reconciliation
# ---------------------------------------------------------------------------


def test_late_celery_delivery_is_fenced_after_terminalization(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    fp = _fingerprint(service, inc)
    _apply(service, inc, fp)
    before = _snapshot(term_db, inc)

    calls = []

    def handler(**kwargs):  # pragma: no cover - must never execute
        calls.append(kwargs)

    attempt = repo.get_attempt(tenant_id=inc.tenant,
                               attempt_id=inc.attempt.id)
    task = SimpleNamespace(request=SimpleNamespace(redelivered=True))
    outcome = run_celery_durable_task(
        task, repo=repo, handler=handler, job_id=inc.job_id,
        attempt_id=attempt.id, tenant_id=inc.tenant,
        dispatch_token=attempt.dispatch_token,
        celery_task_id=attempt.celery_task_id or attempt.id)
    assert outcome in ("terminal_noop", "stale_delivery")
    assert calls == []
    # No task body/business effect, no new attempt/generation, no temp
    # reacquisition, job stays FAILED and the attempt stays LOST.
    after = _snapshot(term_db, inc)
    assert after == before
    assert owned.exists()
    assert repo.get_job(tenant_id=inc.tenant,
                        job_id=inc.job_id).status == JobStatus.FAILED
    assert repo.get_attempt(tenant_id=inc.tenant,
                            attempt_id=attempt.id).status == \
        AttemptStatus.LOST
    attempts = repo.attempts_for_job(tenant_id=inc.tenant,
                                     job_id=inc.job_id)
    assert len(attempts) == 2
    assert all(a.execution_generation == 1 for a in attempts)


def test_reconciliation_dry_run_never_reopens_terminalized_generation(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    fp = _fingerprint(service, inc)
    _apply(service, inc, fp)
    before = _snapshot(term_db, inc)

    report = reconcile(service, tenant_id=inc.tenant, privileged=False,
                       batch=50, apply=False)
    assert report["applied"] is False
    assert report["scope"] == inc.tenant
    assert isinstance(report["counts"], dict)
    assert len(report["items"]) <= 50
    for item in report["items"]:
        assert {"job_id", "classification", "action", "note"} <= set(item)
    # R2 must not propose a republish, R7 must not classify a stale run,
    # R9 must not resolve a divergence for the terminalized generation.
    assert not any(item["job_id"] == inc.job_id for item in report["items"])
    _assert_no_partial_writes(term_db, inc, before)


# ---------------------------------------------------------------------------
# Downstream sequencing
# ---------------------------------------------------------------------------


def test_terminalization_does_not_touch_ingestion_version_temp_or_qdrant(
        service, repo, kservice, term_db, tmp_path, monkeypatch):
    import retriva.ingestion_api.upload_temp as upload_temp
    root, owned = _isolated_temp(tmp_path)
    monkeypatch.setattr(upload_temp, "staging_root", lambda: str(root))
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    before = _snapshot(term_db, inc)
    contents_before = owned.read_bytes()

    fp = _fingerprint(service, inc)
    _apply(service, inc, fp)

    after = _snapshot(term_db, inc)
    # Terminalization itself changes ONLY the job/attempt/events: the
    # ingestion, version, chunks, Qdrant-operation evidence and document
    # current version are untouched, and the parse temp is preserved.
    assert after["ingestion"] == before["ingestion"]
    assert after["version"] == before["version"]
    assert after["chunks"] == before["chunks"]
    assert after["ops"] == before["ops"]
    assert after["document"] == before["document"]
    assert owned.exists()
    assert owned.read_bytes() == contents_before


def test_fail_ingestion_closes_zero_chunk_staging_incident(
        service, repo, kservice, term_db, tmp_path):
    _, owned = _isolated_temp(tmp_path)
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    fp = _fingerprint(service, inc)
    _apply(service, inc, fp)

    kservice.fail_ingestion(
        tenant_id=inc.tenant, ingestion_id=inc.staging_ingestion_id,
        version_id=inc.staging_version_id, error_code="parse_failed")

    rows = _fetch(
        term_db,
        "SELECT sync_state FROM knowledge.ingestions "
        "WHERE ingestion_id = %s", (inc.staging_ingestion_id,))
    assert rows[0][0] == "failed"
    rows = _fetch(
        term_db,
        "SELECT status FROM knowledge.document_versions "
        "WHERE version_id = %s", (inc.staging_version_id,))
    assert rows[0][0] == "failed"
    # The known-good current version remains current and serving.
    rows = _fetch(
        term_db,
        "SELECT current_version_id FROM knowledge.documents "
        "WHERE document_id = %s", (inc.document_id,))
    assert rows[0][0] == inc.good_version_id
    rows = _fetch(
        term_db,
        "SELECT status FROM knowledge.document_versions "
        "WHERE version_id = %s", (inc.good_version_id,))
    assert rows[0][0] == "indexed"
    # Qdrant-operation evidence stays empty and no Spec 031 deletion
    # operation was created anywhere in the tenant.
    rows = _fetch(
        term_db,
        "SELECT count(*) FROM knowledge.qdrant_operations "
        "WHERE tenant_id = %s AND op_type = 'delete_points'",
        (inc.tenant,))
    assert rows[0][0] == 0


def test_parse_temp_release_exactly_once_after_closure(
        service, repo, kservice, term_db, tmp_path, monkeypatch):
    import retriva.ingestion_api.upload_temp as upload_temp
    root, owned = _isolated_temp(tmp_path)
    bystander = root / "parse-bystander.tmp"
    bystander.write_bytes(b"other")
    monkeypatch.setattr(upload_temp, "staging_root", lambda: str(root))
    inc = _seed_incident(service, repo, kservice, term_db,
                         temp_path=str(owned))
    fp = _fingerprint(service, inc)
    _apply(service, inc, fp)

    # Terminalization must not release the temp; the claimant ownership
    # ends only after the job is durably terminal, then the existing
    # helper removes exactly one file.
    assert owned.exists()
    assert upload_temp.release_job_staged_temp(
        {"temp_path": str(owned)}) is True
    assert not owned.exists()
    assert bystander.exists()
    assert upload_temp.release_job_staged_temp(
        {"temp_path": str(owned)}) is False  # missing-safe repeat no-op
    assert upload_temp.release_job_staged_temp({}) is False
    assert upload_temp.release_job_staged_temp(None) is False
    assert bystander.exists()


def test_parse_temp_release_confined_no_traversal_or_symlink(
        tmp_path, monkeypatch):
    import retriva.ingestion_api.upload_temp as upload_temp
    from retriva.ingestion_api.upload_temp import UploadTempFile
    root = tmp_path / "staging"
    root.mkdir()
    monkeypatch.setattr(upload_temp, "staging_root", lambda: str(root))
    outside = tmp_path / "outside.tmp"
    outside.write_bytes(b"x")
    assert upload_temp.release_staged_temp(str(outside)) is False
    assert outside.exists()
    target = root / "target.tmp"
    target.write_bytes(b"x")
    link = root / "link.tmp"
    link.symlink_to(target)
    assert upload_temp.release_staged_temp(str(link)) is False
    assert target.exists()
    assert upload_temp.release_staged_temp(None) is False
    assert UploadTempFile.cleanup("", root=str(root)) is False
