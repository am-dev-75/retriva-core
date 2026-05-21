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
from retriva.ingestion_api.schemas_v2 import RetrievalRequest, RetrievalResponse
from retriva.ingestion_api.deps import require_kbs_exist
from retriva.registry import CapabilityRegistry
from retriva.logger import get_logger
from retriva.profiler import Profiler
import time
import retriva.qa.retriever        # noqa: F401 — registers DefaultRetriever
import retriva.qa.reranker         # noqa: F401 — registers DefaultReranker
import retriva.qa.hybrid_selector  # noqa: F401 — registers DefaultHybridSelector

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v2/retrieval", tags=["v2-retrieval"])


@router.post("/query", response_model=RetrievalResponse)
async def search_documents(request: RetrievalRequest):
    """Retrieve chunks based on vector similarity, with advanced metadata filtering and modes."""
    # KB enforcement (SDD): retrieval requires a non-empty, validated kb_ids list.
    require_kbs_exist(request.kb_ids, allow_empty=False)
    start_time = time.time()
    
    # Observability: Log request receipt
    logger.info(
        f"metadata_filter_mode_received: {request.metadata_filter_mode}, "
        f"filters_count={len(request.metadata_filters)}, "
        f"query='{request.query[:50]}...'"
    )
    
    try:
        registry = CapabilityRegistry()
        retriever = registry.get_instance("retriever")
        
        # Map filters to list of dicts for the retriever
        filters = [f.model_dump() for f in request.metadata_filters]
        
        # Support deprecated user_metadata_filter if metadata_filters is empty
        if not filters and request.user_metadata_filter:
            for k, v in request.user_metadata_filter.items():
                filters.append({
                    "field": f"user_metadata.{k}",
                    "operator": "eq",
                    "value": v
                })
        
        # Start profiler for structured logging and request_id propagation
        profiler = Profiler.start_request()
        
        chunks = retriever.retrieve(
            query=request.query,
            top_k=request.top_k,
            metadata_filters=filters,
            metadata_filter_mode=request.metadata_filter_mode.value,
            rerank=request.rerank,
            hybrid_selection=request.hybrid_selection,
            kb_ids=request.kb_ids
        )
        
        duration_ms = int((time.time() - start_time) * 1000)
        profiler.mark_phase("retrieval_finished")
        profiler.finalize()
        
        # Observability: Log completion
        logger.info(
            f"[{profiler.request_id}] filtered_rag_completed: results={len(chunks)}, "
            f"mode={request.metadata_filter_mode}, "
            f"duration_ms={duration_ms}"
        )
        
        return RetrievalResponse(chunks=chunks)
        
    except Exception as e:
        logger.error(f"Error during retrieval: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )
