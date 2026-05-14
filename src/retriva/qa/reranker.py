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
Default re-ranker for Retriva OSS — two-stage retrieval.

Stage 1 (vector search) produces broad recall candidates from Qdrant.
Stage 2 (this module) re-scores those candidates with a cross-encoder
model and returns only the top-N most relevant chunks to the query.

The re-ranker calls the Cohere-compatible ``/rerank`` endpoint via
``httpx``.  This is supported natively by OpenRouter, Cohere, and
any provider exposing the same contract.

Override path for Retriva Pro:
    Register a custom ``reranker`` capability at priority > 100
    via the CapabilityRegistry.
"""

import time
import httpx
from typing import Dict, List

from retriva.config import settings
from retriva.logger import get_logger

logger = get_logger(__name__)

MAX_RETRIES = 2
RETRY_BASE_DELAY = 1.0  # seconds
REQUEST_TIMEOUT = 30.0   # seconds


def _call_rerank_api(
    query: str,
    documents: List[str],
    top_n: int,
) -> List[Dict]:
    """
    Call the ``/rerank`` endpoint and return the ``results`` list.

    Each result dict has the shape::

        {"index": int, "relevance_score": float, "document": {"text": str}}

    Raises on non-transient errors; retries on transient ones.
    """
    url = f"{settings.retrieval_rerank_base_url.rstrip('/')}/rerank"
    headers = {
        "Authorization": f"Bearer {settings.retrieval_rerank_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.retrieval_rerank_model,
        "query": query,
        "documents": documents,
        "top_n": top_n,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
                response = client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
                return data.get("results", [])

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            if attempt < MAX_RETRIES:
                logger.warning(
                    f"Reranker attempt {attempt}/{MAX_RETRIES} failed "
                    f"({type(e).__name__}). Retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
            else:
                raise RuntimeError(
                    f"Reranker failed after {MAX_RETRIES} attempts: {e}"
                ) from e

        except httpx.HTTPStatusError as e:
            # Non-transient HTTP errors (4xx) — no retry
            if 400 <= e.response.status_code < 500:
                raise RuntimeError(
                    f"Reranker returned {e.response.status_code}: "
                    f"{e.response.text[:500]}"
                ) from e
            # Server errors (5xx) — retry
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            if attempt < MAX_RETRIES:
                logger.warning(
                    f"Reranker attempt {attempt}/{MAX_RETRIES} got "
                    f"{e.response.status_code}. Retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
            else:
                raise RuntimeError(
                    f"Reranker failed after {MAX_RETRIES} attempts: {e}"
                ) from e


def _truncate_documents(documents: List[str], max_length: int) -> List[str]:
    """Truncate each document to *max_length* characters."""
    if max_length <= 0:
        return documents
    return [doc[:max_length] for doc in documents]


def _rerank_batched(
    query: str,
    documents: List[str],
    top_n: int,
    batch_size: int,
) -> List[Dict]:
    """
    Score documents in batches of *batch_size*, then merge and sort
    all results by ``relevance_score`` descending, returning *top_n*.

    Each batch is sent as an independent ``/rerank`` call.  The
    ``index`` field in each result is remapped to the global document
    index before merging.
    """
    if batch_size <= 0 or len(documents) <= batch_size:
        # Single batch — no merging needed
        return _call_rerank_api(query, documents, top_n)

    all_results: List[Dict] = []

    for batch_start in range(0, len(documents), batch_size):
        batch_docs = documents[batch_start : batch_start + batch_size]
        # Ask each batch for its full ranking so we can merge globally
        batch_top_n = min(top_n, len(batch_docs))
        batch_results = _call_rerank_api(query, batch_docs, batch_top_n)

        # Remap batch-local indices to global indices
        for r in batch_results:
            r["index"] = r["index"] + batch_start

        all_results.extend(batch_results)

    # Sort all results by relevance_score descending, then take top_n
    all_results.sort(key=lambda r: r.get("relevance_score", 0.0), reverse=True)
    return all_results[:top_n]


class DefaultReranker:
    """OSS default reranker — cross-encoder via Cohere-compatible /rerank API."""

    def rerank(self, query: str, chunks: List[Dict], top_n: int) -> List[Dict]:
        """
        Re-rank *chunks* by relevance to *query* and return the top *top_n*.

        Each chunk must have a ``"text"`` key.  The original chunk dicts
        are returned (not copies), preserving all metadata.

        On failure the original chunks are returned truncated to *top_n*
        in their original (vector-similarity) order.
        """
        if not chunks:
            return chunks

        # Clamp top_n to available chunk count
        effective_top_n = min(top_n, len(chunks))

        # Extract and truncate text for the API call
        documents = [c.get("text", "") for c in chunks]
        documents = _truncate_documents(documents, settings.retrieval_rerank_max_length)

        try:
            results = _rerank_batched(
                query,
                documents,
                effective_top_n,
                settings.retrieval_rerank_batch_size,
            )
        except Exception as e:
            logger.warning(
                f"Reranker failed, falling back to vector-search order: {e}"
            )
            return chunks[:effective_top_n]

        if not results:
            logger.warning("Reranker returned empty results, using vector-search order.")
            return chunks[:effective_top_n]

        # Map results back to original chunk dicts by index
        reranked = []
        for r in results:
            idx = r.get("index")
            if idx is not None and 0 <= idx < len(chunks):
                chunk = chunks[idx]
                # Sync _score so that subsequent sorting/diversity filters use the reranked score
                chunk["_score"] = r.get("relevance_score", 0.0)
                reranked.append(chunk)
            else:
                logger.warning(f"Reranker returned out-of-bounds index {idx}, skipping.")

        logger.info(
            f"Reranker: {len(chunks)} candidates → {len(reranked)} results "
            f"(top score: {results[0].get('relevance_score', 'N/A'):.4f})"
        )
        return reranked


# Register as default implementation
from retriva.registry import CapabilityRegistry
CapabilityRegistry().register("reranker", DefaultReranker, priority=100)
