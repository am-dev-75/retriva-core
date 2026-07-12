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

def _register_tasks(app):
    """Register Celery tasks on the app. Called once during app init."""

    from retriva.ingestion_api.job_manager import JobStatus

    @app.task(
        name="retriva.ingestion_api.tasks.process_document_task",
        bind=True,
        max_retries=settings.celery_task_max_retries,
        acks_late=True,
    )
    def process_document_task(
        self,
        job_id: str,
        source_uri: str,
        content_type: Optional[str],
        user_metadata: Optional[Dict[str, str]],
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
        """Celery task that runs the full v2 ingestion pipeline.

        This is the Celery equivalent of ``BackgroundTasks.add_task(
        process_document_v2, ...)``.
        """
        from retriva.ingestion_api.routers.v2_documents import process_document_v2
        from retriva.indexing.qdrant_store import set_collection_name, _collection_name_ctx, DEFAULT_COLLECTION_NAME

        logger.info(f"Celery task started: job_id={job_id}, source={source_uri}, collection={collection_name}")
        
        col = collection_name or DEFAULT_COLLECTION_NAME
        token = set_collection_name(col)

        # Track OOM-kill re-queues: Celery's task_reject_on_worker_lost=True
        # re-delivers the task after a SIGKILL, but doesn't increment the
        # Celery retry counter.  We use Redis to count attempts and bail out
        # after max_retries+1 tries.
        max_retries = settings.celery_task_max_retries or 3
        attempt = _increment_retry_count(content_hash or job_id)
        if attempt > max_retries + 1:
            logger.error(
                f"Celery task giving up: job_id={job_id}, "
                f"attempts={attempt}, max={max_retries + 1} — "
                f"likely OOM-kill loop"
            )
            _set_job_state(job_id, {
                "job_id": job_id,
                "status": JobStatus.FAILED.value,
                "error": (
                    f"Task failed after {attempt} attempts — "
                    f"likely OOM-kill during processing. "
                    f"Consider increasing Docker memory limits or reducing "
                    f"the document size."
                ),
            })
            return  # Don't re-raise; just stop retrying

        logger.info(f"Celery task attempt {attempt}/{max_retries + 1}: job_id={job_id}")

        # Build a cancel_check that polls Redis instead of the in-memory
        # JobManager singleton.
        def cancel_check():
            return _is_cancel_requested(job_id)

        # Stage-change callback: sync job state to Redis so the API process
        # can report real-time progress to the user.
        def on_stage_change(state: dict):
            _set_job_state(job_id, state)

        try:
            process_document_v2(
                source_uri=source_uri,
                content_type=content_type,
                user_metadata=user_metadata,
                parser_hint=parser_hint,
                job_id=job_id,
                temp_path=temp_path,
                doc_id=doc_id,
                content_hash=content_hash,
                kb_id=kb_id,
                source_paths=source_paths,
                content_size=content_size,
                ingestion_status=ingestion_status,
                created_at=created_at,
                _cancel_check=cancel_check,
                _on_stage_change=on_stage_change,
            )

            # Sync final state to Redis
            from retriva.ingestion_api.job_manager import JobManager
            job = JobManager().get_job(job_id)
            if job:
                _set_job_state(job_id, job.to_dict())
            _clear_cancel_flag(job_id)
            _clear_retry_count(content_hash or job_id)

        except Exception as exc:
            logger.error(f"Celery task failed: job_id={job_id}, error={exc}")
            # Mark job as failed in Redis
            _set_job_state(job_id, {
                "job_id": job_id,
                "status": JobStatus.FAILED.value,
                "error": str(exc),
            })
            # Retry with exponential backoff for transient failures
            raise self.retry(exc=exc, countdown=2 ** self.request.retries)
        finally:
            _collection_name_ctx.reset(token)

    @app.task(
        name="retriva.ingestion_api.tasks.process_mediawiki_task",
        bind=True,
        max_retries=settings.celery_task_max_retries,
        acks_late=True,
    )
    def process_mediawiki_task(
        self,
        job_id: str,
        staged_dir: str,
        user_metadata: Optional[Dict[str, str]],
        kb_id: str,
        collection_name: Optional[str] = None,
    ):
        """Celery task for MediaWiki export ingestion."""
        from retriva.ingestion.mediawiki_v2_parser import process_mediawiki_export
        from retriva.ingestion_api.job_manager import JobManager
        from retriva.indexing.qdrant_store import set_collection_name, _collection_name_ctx, DEFAULT_COLLECTION_NAME

        logger.info(f"Celery MediaWiki task started: job_id={job_id}, dir={staged_dir}, collection={collection_name}")
        
        col = collection_name or DEFAULT_COLLECTION_NAME
        token = set_collection_name(col)

        def cancel_check():
            return _is_cancel_requested(job_id)

        try:
            manager = JobManager()
            manager.start_job(job_id)

            process_mediawiki_export(
                staged_dir,
                user_metadata,
                kb_id,
                cancel_check,
                job_id,
            )

            manager.complete_job(job_id)
            job = manager.get_job(job_id)
            if job:
                _set_job_state(job_id, job.to_dict())
            _clear_cancel_flag(job_id)

        except Exception as exc:
            logger.error(f"Celery MediaWiki task failed: job_id={job_id}, error={exc}")
            from retriva.ingestion_api.job_manager import JobManager
            try:
                JobManager().fail_job(job_id, str(exc))
            except Exception:
                pass
            _set_job_state(job_id, {
                "job_id": job_id,
                "status": JobStatus.FAILED.value,
                "error": str(exc),
            })
            raise self.retry(exc=exc, countdown=2 ** self.request.retries)
        finally:
            _collection_name_ctx.reset(token)


# ── Public dispatch functions (called from the API) ──────────────────────

def dispatch_document_task(payload: dict) -> str:
    """Enqueue a document ingestion task via Celery.

    Returns the Celery task ID (used as the job_id).
    """
    app = _get_celery()
    if app is None:
        raise RuntimeError("Celery is not configured")

    # Ensure tasks are registered
    if not app.tasks.get("retriva.ingestion_api.tasks.process_document_task"):
        _register_tasks(app)

    result = app.tasks["retriva.ingestion_api.tasks.process_document_task"].delay(
        **payload,
    )
    return result.id


def dispatch_mediawiki_task(payload: dict) -> str:
    """Enqueue a MediaWiki ingestion task via Celery.

    Returns the Celery task ID (used as the job_id).
    """
    app = _get_celery()
    if app is None:
        raise RuntimeError("Celery is not configured")

    if not app.tasks.get("retriva.ingestion_api.tasks.process_mediawiki_task"):
        _register_tasks(app)

    result = app.tasks["retriva.ingestion_api.tasks.process_mediawiki_task"].delay(
        **payload,
    )
    return result.id


def request_task_cancellation(job_id: str) -> bool:
    """Request cancellation of a Celery task.

    Returns True if the cancellation flag was set.
    """
    app = _get_celery()
    if app is None:
        return False

    # Revoke the Celery task (terminates if running)
    app.control.revoke(job_id, terminate=False)
    # Set Redis flag for cooperative cancellation
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
