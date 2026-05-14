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

from retriva.config import settings
from retriva.indexing.embeddings import get_embeddings
from retriva.indexing.qdrant_store import get_client, search_chunks
from retriva.logger import get_logger
from retriva.registry import CapabilityRegistry
from typing import List, Dict, Optional, Any

logger = get_logger(__name__)


def retrieve_top_chunks(
    query: str, 
    retriever_top_k: int = 20, 
    metadata_filters: Optional[List[Dict[str, Any]]] = None,
    metadata_filter_mode: str = "soft"
) -> List[Dict]:
    logger.debug(f"Retrieving top_{retriever_top_k} chunks for query (mode={metadata_filter_mode})...")
    embeddings = get_embeddings([query])
    query_vector = embeddings[0]
    
    client = get_client()
    results = search_chunks(
        client=client, 
        query_vector=query_vector, 
        retriever_top_k=retriever_top_k, 
        metadata_filters=metadata_filters,
        metadata_filter_mode=metadata_filter_mode,
        query_text=query
    )
    for i, res in enumerate(results):
        logger.debug(f"  Chunk {i+1}: {res.get('page_title')} (path: {res.get('source_path')})")
    return results


def _rerank_if_enabled(query: str, chunks: List[Dict], enabled: bool = True) -> List[Dict]:
    """Apply two-stage re-ranking."""
    if not enabled or not settings.enable_retrieval_reranking:
        return chunks

    candidates = settings.retrieval_rerank_candidates
    if 0 < candidates < len(chunks):
        chunks = chunks[:candidates]

    registry = CapabilityRegistry()
    reranker = registry.get_instance("reranker")
    return reranker.rerank(query, chunks, settings.retrieval_rerank_top_n)


def _hybrid_select_if_enabled(
    reranked: List[Dict],
    vector_top: List[Dict],
    enabled: bool = True
) -> List[Dict]:
    """Apply hybrid retrieval selection."""
    if not enabled or not settings.enable_retrieval_reranking or not settings.enable_hybrid_retrieval_selection:
        return reranked

    registry = CapabilityRegistry()
    selector = registry.get_instance("hybrid_selector")
    return selector.select(
        reranked,
        vector_top,
        keep_m=settings.hybrid_rerank_keep_top_m,
        keep_l=settings.hybrid_vector_keep_top_l,
    )


def _apply_diversity_filter(chunks: List[Dict], max_per_doc: int) -> List[Dict]:
    """Group chunks by doc_id and keep only top N per document.

    Assumes chunks are already sorted by score (relevance).
    """
    if not chunks or max_per_doc <= 0:
        return chunks

    doc_counts = {}  # doc_id -> count
    filtered = []

    for chunk in chunks:
        # Use doc_id if available, fallback to source_path
        doc_id = chunk.get("doc_id") or chunk.get("source_path")
        if not doc_id:
            filtered.append(chunk)
            continue

        count = doc_counts.get(doc_id, 0)
        if count < max_per_doc:
            filtered.append(chunk)
            doc_counts[doc_id] = count + 1
        else:
            logger.debug(f"Diversity filter: capping {doc_id} at {max_per_doc} chunks")

    return filtered


class DefaultRetriever:
    """OSS default retriever — semantic search via embeddings + Qdrant."""

    def retrieve(
        self,
        query: str,
        top_k: int = 20,
        metadata_filters: Optional[List[Dict[str, Any]]] = None,
        metadata_filter_mode: str = "soft",
        rerank: bool = True,
        hybrid_selection: bool = True
    ) -> List[Dict]:
        """Run the full retrieval pipeline: vector search -> rerank -> diversity -> hybrid select."""

        # 1. Broad retrieval
        # In hard mode, we fetch more candidates than top_k to allow diversity filtering
        if metadata_filter_mode == "hard":
            fetch_k = max(top_k * settings.retrieval_fetch_k_multiplier, 50)
            logger.info(f"Retrieval diversity: hard mode enabled, fetching {fetch_k} candidates for top_{top_k}")
        else:
            fetch_k = top_k

        chunks = retrieve_top_chunks(
            query,
            retriever_top_k=fetch_k,
            metadata_filters=metadata_filters,
            metadata_filter_mode=metadata_filter_mode
        )

        if not chunks:
            return []

        # 2. Pipeline processing
        vector_top = chunks[:]

        # A. Reranking (Cross-Encoder)
        if rerank:
            chunks = _rerank_if_enabled(query, chunks, enabled=rerank)

        # B. Diversity Filtering (Per-document cap)
        # We only apply this in hard mode for now as requested
        if metadata_filter_mode == "hard":
            max_per_doc = settings.retrieval_max_chunks_per_doc
            chunks = _apply_diversity_filter(chunks, max_per_doc)
            logger.info(f"Retrieval diversity: applied cap={max_per_doc}, pool reduced to {len(chunks)}")

        # C. Hybrid Retrieval Selection
        if hybrid_selection:
            chunks = _hybrid_select_if_enabled(chunks, vector_top, enabled=hybrid_selection)

        # 3. Final Sort and Limit to top_k
        # Ensure we return exactly top_k chunks based on the final scores
        # Chunks from qdrant_store and reranker should already have scores.
        chunks.sort(key=lambda x: x.get("_score", 0.0), reverse=True)
        return chunks[:top_k]


# Register as default implementation
CapabilityRegistry().register("retriever", DefaultRetriever, priority=100)
