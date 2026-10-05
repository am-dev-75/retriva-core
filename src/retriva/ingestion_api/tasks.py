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
Celery task wrappers for the durable v2 ingestion pipeline.

These tasks run inside the Celery worker process (not the FastAPI
process).  Each body executes the DURABLE worker protocol: the
PostgreSQL-backed job lifecycle is authoritative (Spec 025); Redis is
transport only.  The legacy API v1 Redis job-state helpers, the raw
``AsyncResult`` status fallback, and the legacy cancellation surface
were removed by Spec 027 / ADR-032.
"""

from __future__ import annotations

from typing import Dict, Optional

from retriva.config import settings
from retriva.logger import get_logger

logger = get_logger(__name__)

# ── Task definitions ─────────────────────────────────────────────────────
#
# Spec 025 integration: the task bodies run the DURABLE worker
# protocol (PostgreSQL-authoritative attempt lifecycle: atomic claim,
# throttled durable cancellation checks, durable progress, durable
# retry scheduling).  The legacy Redis job-state/cancel keys, the
# legacy retry counter, and the raw ``AsyncResult`` status fallback
# are retired (Spec 027).  No execution happens outside the durable
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
        run_artifact_job,
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

    @app.task(
        name="retriva.ingestion_api.tasks.process_artifact_task",
        bind=True,
        max_retries=0,
        acks_late=True,
    )
    def process_artifact_task(
        self,
        job_id: str,
        attempt_id: str,
        tenant_id: str,
        dispatch_token: str,
        celery_task_id: str,
        artifact_id: str,
        artifact_type: str,
        format: str,
        parameters: Optional[Dict[str, object]] = None,
        user_metadata: Optional[Dict[str, object]] = None,
        collection_context: Optional[str] = None,
    ):
        """Durable v2 artifact generation attempt (Spec 026).

        Handler failures are NON-retryable (deterministic input;
        provider cost never duplicated) — ``max_retries=0`` so Celery
        never re-delivers after a failure; duplicate delivery of a
        RUNNING attempt is classified by the durable claim protocol,
        and redelivery of an already-claimed attempt is a no-op."""
        return run_artifact_job(
            self,
            job_id=job_id,
            attempt_id=attempt_id,
            tenant_id=tenant_id,
            dispatch_token=dispatch_token,
            celery_task_id=celery_task_id,
            payload=dict(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                format=format,
                parameters=parameters or {},
                user_metadata=user_metadata,
                collection_context=collection_context,
            ),
        )
