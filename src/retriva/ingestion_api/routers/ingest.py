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

from datetime import datetime, timezone
from fastapi import APIRouter, BackgroundTasks, status
from retriva.ingestion_api.schemas import ChunkIngestRequest, IngestResponse
from retriva.indexing.qdrant_store import get_client, upsert_chunks, init_collection, get_collection_name
from retriva.ingestion_api.job_manager import JobManager, CancellationError
from retriva.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])


def process_chunks_in_background(payload: ChunkIngestRequest, job_id: str):
    manager = JobManager()
    manager.start_job(job_id)
    cancel_check = lambda: manager.is_cancel_requested(job_id)

    try:
        if not payload.chunks:
            manager.complete_job(job_id)
            return

        # Ensure ingestion_timestamp is set for all chunks
        ts = datetime.now(timezone.utc).isoformat()
        for chunk in payload.chunks:
            if not chunk.metadata.ingestion_timestamp:
                chunk.metadata.ingestion_timestamp = ts

        client = get_client()
        upsert_chunks(client, payload.chunks, cancel_check=cancel_check)
        manager.complete_job(job_id)

    except CancellationError:
        manager.mark_cancelled(job_id)
        logger.info(f"Job {job_id} cancelled during chunk processing")
    except Exception as e:
        manager.fail_job(job_id, str(e))
        logger.error(f"Job {job_id} failed: {e}")


@router.post("/chunks", response_model=IngestResponse, status_code=status.HTTP_202_ACCEPTED)
async def ingest_chunks(payload: ChunkIngestRequest, background_tasks: BackgroundTasks):
    logger.debug(f"Processing in background chunks: {payload.chunks}")
    manager = JobManager()
    job = manager.create_job(source="chunks", job_type="chunks")
    background_tasks.add_task(process_chunks_in_background, payload, job.id)
    return IngestResponse(status="accepted", message="Chunks accepted for processing", job_id=job.id)

@router.delete("/collection", response_model=IngestResponse, status_code=status.HTTP_200_OK)
async def clear_collection():
    logger.debug("Clearing collection...")
    client = get_client()
    try:
        if client.collection_exists(get_collection_name()):
            client.delete_collection(get_collection_name())
        init_collection(client)
        return IngestResponse(status="ok", message="Collection cleared and re-initialized")
    except Exception as e:
        logger.error(f"Error clearing collection: {e}")
        return IngestResponse(status="error", message=str(e))