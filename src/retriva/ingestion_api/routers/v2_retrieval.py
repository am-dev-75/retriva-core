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
v2 retrieval endpoints.
"""

from fastapi import APIRouter, HTTPException, status
from retriva.indexing.qdrant_store import get_client, search_chunks
from retriva.indexing.embeddings import get_embeddings
from retriva.ingestion_api.schemas import UserMetadataValidationError, validate_user_metadata
from retriva.ingestion_api.schemas_v2 import RetrievalRequest, RetrievalResponse
from retriva.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v2/retrieval", tags=["v2-retrieval"])


@router.post("/query", response_model=RetrievalResponse)
async def search_documents(request: RetrievalRequest):
    """Retrieve chunks based on vector similarity, optionally filtered by user_metadata."""
    if request.user_metadata_filter:
        try:
            validate_user_metadata(request.user_metadata_filter)
        except UserMetadataValidationError as e:
            raise HTTPException(status_code=422, detail=e.details)
            
    try:
        query_vector = get_embeddings([request.query])[0]
        client = get_client()
        chunks = search_chunks(
            client=client,
            query_vector=query_vector,
            retriever_top_k=request.top_k,
            metadata_filter=request.user_metadata_filter
        )
        return RetrievalResponse(chunks=chunks)
    except Exception as e:
        logger.error(f"Error during retrieval: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )
