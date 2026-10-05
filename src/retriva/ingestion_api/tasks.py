# Copyright (C) 2026 Andrea Marson (am.dev.75@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Celery task wrappers for the v2 ingestion pipeline.

These tasks run inside the Celery worker process (not the FastAPI process).
They call the same ``process_document_v2`` / ``process_mediawiki_export``
functions used by the BackgroundTasks path, but with Redis-backed job state
and cancellation signals.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Optional

from retriva.config import settings
from retriva.logger import get_logger

logger = get_logger(__name__)

# ── Lazy Celery import ────────────────────────────────────────────────────
# We import celery only when this module is loaded by the worker.  When the
# API process imports it (to call ``.delay()``), the celery_app module handles
# the conditional import.

_celery = None

def _get_celery():
    global _celery
    if _celery is None:
        from retriva.ingestion_api.celery_app import get_celery_app
        _celery = get_celery_app()
    return _celery


# ── Redis-backed helpers (job state + cancellation) ──────────────────────

def _redis_client():
    """Return a Redis client, or None if Redis is not available."""
    try:
        import redis
        return redis.from_url(settings.celery_broker_url, decode_responses=True)
    except Exception:
        return None


def _set_job_state(job_id: str, state: dict) -> None:
    """Store job state in Redis as JSON."""
    r = _redis_client()
    if r is None:
        return
    r.setex(f"retriva:job:{job_id}", 7 * 24 * 3600, json.dumps(state))


def _get_job_state(job_id: str) -> Optional[dict]:
    """Retrieve job state from Redis, or None if not found."""
    r = _redis_client()
    if r is None:
        return None
    raw = r.get(f"retriva:job:{job_id}")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _delete_job_state(job_id: str) -> None:
    """Remove job state from Redis."""
    r = _redis_client()
    if r is None:
        return
    r.delete(f"retriva:job:{job_id}")


def _increment_retry_count(content_hash: str) -> int:
    """Increment and return the retry count for a given content hash.

    Used to track OOM-kill re-queues (which bypass Celery's own retry
    counter) and prevent infinite retry loops.
    """
    r = _redis_client()
    if r is None:
        return 0
    key = f"retriva:retry:{content_hash}"
    count = r.incr(key)
    r.expire(key, 24 * 3600)  # TTL: 24 hours
    return count


def _clear_retry_count(content_hash: str) -> None:
    """Clear the retry count after successful completion."""
    r = _redis_client()
    if r is None:
        return
    r.delete(f"retriva:retry:{content_hash}")


def _set_cancel_flag(job_id: str) -> None:
    """Set the cancellation flag in Redis."""
    r = _redis_client()
    if r is None:
        return
    r.setex(f"retriva:cancel:{job_id}", 24 * 3600, "1")


def _is_cancel_requested(job_id: str) -> bool:
    """Check the cancellation flag in Redis."""
    r = _redis_client()
    if r is None:
        return False
    return r.exists(f"retriva:cancel:{job_id}") > 0


def _clear_cancel_flag(job_id: str) -> None:
    """Remove the cancellation flag."""
    r = _redis_client()
    if r is None:
        return
    r.delete(f"retriva:cancel:{job_id}")


# ── Task definitions ─────────────────────────────────────────────────────
#
# Spec 025 integration: the task bodies run the DURABLE worker
# protocol (PostgreSQL-authoritative attempt lifecycle: atomic claim,
# throttled durable cancellation checks, durable progress, durable
# retry scheduling).  Celery retry counters are diagnostic only; the
# Redis job-state/cancel keys and the Redis OOM counter are retired
# from the integrated flow (diagnostic helpers remain for the legacy
# status fallback only).  No execution happens outside the durable
# attempt model.

def _register_tasks(app):
    """Register Celery tasks on the app. Called once during app init
    (idempotent: BOTH sides of the dispatch need the same registry —
    the worker consumes by these names and the publisher resolves the
    preallocated task by name (Spec 025 dispatch); a second call is a
    no-op)."""
    if getattr(app, "_retriva_tasks_registered", False):
        return
    app._retriva_tasks_registered = True

    from retriva.ingestion_api.durable_jobs import (
        run_document_job,
        run_mediawiki_job,
    )

    @app.task(
        name="retriva.ingestion_api.tasks.process_document_task",
        bind=True,
        max_retries=settings.celery_task_max_retries,
        acks_late=True,
    )
    def process_document_task(
        self,
        job_id: str,
        attempt_id: str,
        tenant_id: str,
        dispatch_token: str,
        celery_task_id: str,
        source_uri: str,
        content_type: Optional[str],
        user_metadata: Optional[Dict[str, object]],
        parser_hint: Optional[str],
        temp_path: Optional[str] = None,
        doc_id: Optional[str] = None,
        content_hash: Optional[str] = None,
        kb_id: str = "default",
        source_paths: Optional[List[str]] = None,
        content_size: Optional[int] = None,
        ingestion_status: str = "completed",
        created_at: Optional[str] = None,
        collection_name: Optional[str] = None,
    ):
        """Durable v2 document ingestion attempt (Spec 025).

        The protocol arguments carry the durable job/attempt identity
        (dispatched with the PREALLOCATED Celery task id); the claim
        protocol owns duplicate-delivery classification.
        """
        return run_document_job(
            self,
            job_id=job_id,
            attempt_id=attempt_id,
            tenant_id=tenant_id,
            dispatch_token=dispatch_token,
            celery_task_id=celery_task_id,
            payload=dict(
                source_uri=source_uri,
                content_type=content_type,
                user_metadata=user_metadata,
                parser_hint=parser_hint,
                temp_path=temp_path,
                doc_id=doc_id,
                content_hash=content_hash,
                kb_id=kb_id,
                source_paths=source_paths,
                content_size=content_size,
                ingestion_status=ingestion_status,
                created_at=created_at,
                collection_name=collection_name,
            ),
        )

    @app.task(
        name="retriva.ingestion_api.tasks.process_mediawiki_task",
        bind=True,
        max_retries=settings.celery_task_max_retries,
        acks_late=True,
    )
    def process_mediawiki_task(
        self,
        job_id: str,
        attempt_id: str,
        tenant_id: str,
        dispatch_token: str,
        celery_task_id: str,
        staged_dir: str,
        user_metadata: Optional[Dict[str, object]],
        kb_id: str,
        collection_name: Optional[str] = None,
    ):
        """Durable MediaWiki export ingestion attempt (Spec 025)."""
        return run_mediawiki_job(
            self,
            job_id=job_id,
            attempt_id=attempt_id,
            tenant_id=tenant_id,
            dispatch_token=dispatch_token,
            celery_task_id=celery_task_id,
            payload=dict(
                staged_dir=staged_dir,
                user_metadata=user_metadata,
                kb_id=kb_id,
                collection_name=collection_name,
            ),
        )


# ── Legacy cancellation surface (still used by legacy flows) ─────────────

def request_task_cancellation(job_id: str) -> bool:
    """Request cancellation of a Celery task.

    Returns True if the cancellation flag was set.
    """
    app = _get_celery()
    if app is None:
        return False

    # Revoke the Celery task (best-effort transport aid only; NEVER a
    # guarantee that running work is interrupted — Spec 025 §3.3).
    app.control.revoke(job_id, terminate=False)
    # Legacy Redis flag for legacy cooperative cancellation.
    _set_cancel_flag(job_id)
    return True


def get_task_status(job_id: str) -> Optional[dict]:
    """Retrieve job status from Redis, falling back to Celery result backend."""
    # First check our Redis job state
    state = _get_job_state(job_id)
    if state is not None:
        return state

    # Fall back to Celery AsyncResult
    app = _get_celery()
    if app is None:
        return None

    result = app.AsyncResult(job_id)
    return {
        "job_id": job_id,
        "status": _celery_state_to_job_status(result.state),
        "source": "",
        "job_type": "v2_document",
        "current_stage": None,
        "stages_completed": [],
        "stage_detail": None,
        "progress": None,
        "created_at": "",
        "updated_at": "",
        "error": str(result.result) if result.failed() else None,
    }


def _celery_state_to_job_status(state: str) -> str:
    """Map Celery task states to JobStatus values."""
    mapping = {
        "PENDING": "pending",
        "STARTED": "running",
        "SUCCESS": "completed",
        "FAILURE": "failed",
        "RETRY": "running",
        "REVOKED": "cancelled",
    }
    return mapping.get(state, "pending")
