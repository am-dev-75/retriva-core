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
Stage 2 (this module) re-scores those candidates and returns only the
top-N most relevant chunks to the query.

Provider model
--------------
This module implements the ``reranker`` capability (see the ``Reranker``
protocol in ``retriva.protocols``) as a provider-neutral adapter: the
transport is selected GLOBALLY via ``RETRIEVAL_RERANK_PROVIDER`` in
Retriva's single settings system and applies to every knowledge base,
retrieval operation, user, and customer. Supported providers live in
``retriva.qa.reranking``:

* ``openrouter`` (default) — Cohere-compatible ``/rerank`` endpoint via
  ``httpx``, supported natively by OpenRouter, Cohere, and any provider
  exposing the same contract (legacy behavior, unchanged).
* ``bedrock`` — Amazon Bedrock Rerank via ``boto3``.

Override paths for Retriva Pro:
    * Replace the whole capability: register a custom ``reranker``
      capability at priority > 100 via the CapabilityRegistry.
    * Add a transport: ``register_rerank_provider(name, factory)`` in
      ``retriva.qa.reranking.factory`` and select it globally.

Error and fallback policy (unchanged): on any provider failure — transport
errors, unknown provider names, AND malformed/unusable provider output —
the original chunks are returned truncated to *top_n* in their original
(vector-similarity) order, with a warning and health/metrics updates.
"""

import time
import httpx
from typing import Any, Dict, List, Tuple

from retriva.config import settings
from retriva.logger import get_logger
from retriva.qa.reranking.base import RerankProviderError
from retriva.qa.reranking.factory import get_reranker_provider
from retriva.qa.reranking.health import reranker_health
from retriva.qa.reranking.metrics import reranker_metrics

logger = get_logger(__name__)


def _coerce_result(result: Any) -> Tuple[int, float]:
    """Validate one provider result entry.

    Returns ``(index, relevance_score)`` or ``(None, 0.0)`` when the entry
    is malformed. Hardening: provider output is external input — a broken
    provider (or Pro extension) must degrade to the fallback policy, never
    raise out of :meth:`DefaultReranker.rerank` or poison downstream
    ``_score`` sorting with non-numeric values.
    """
    if not isinstance(result, dict):
        return None, 0.0
    idx = result.get("index")
    # bool is an int subclass — reject it explicitly.
    if isinstance(idx, bool) or not isinstance(idx, int):
        return None, 0.0
    try:
        score = float(result.get("relevance_score", 0.0))
    except (TypeError, ValueError):
        return None, 0.0
    return idx, score


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
    headers = {"Content-Type": "application/json"}
    if settings.retrieval_rerank_api_key:
        headers["Authorization"] = f"Bearer {settings.retrieval_rerank_api_key}"
    payload = {
        "model": settings.retrieval_rerank_model,
        "query": query,
        "documents": documents,
        "top_n": top_n,
    }

    max_retries = max(1, settings.retrieval_rerank_max_retries)
    retry_base_delay = settings.retrieval_rerank_retry_base_delay
    request_timeout = settings.retrieval_rerank_timeout

    for attempt in range(1, max_retries + 1):
        try:
            with httpx.Client(timeout=request_timeout) as client:
                response = client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                try:
                    data = response.json()
                except ValueError as e:
                    raise RuntimeError(
                        f"Reranker returned a non-JSON payload: {e}"
                    ) from e
                if not isinstance(data, dict):
                    raise RuntimeError(
                        f"Reranker returned unexpected payload type "
                        f"{type(data).__name__} (expected an object with "
                        f"a 'results' list)."
                    )
                return data.get("results", [])

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            delay = retry_base_delay * (2 ** (attempt - 1))
            if attempt < max_retries:
                logger.warning(
                    f"Reranker attempt {attempt}/{max_retries} failed "
                    f"({type(e).__name__}). Retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
            else:
                raise RuntimeError(
                    f"Reranker failed after {max_retries} attempts: {e}"
                ) from e

        except httpx.HTTPStatusError as e:
            # Non-transient HTTP errors (4xx) — no retry
            if 400 <= e.response.status_code < 500:
                raise RuntimeError(
                    f"Reranker returned {e.response.status_code}: "
                    f"{e.response.text[:500]}"
                ) from e
            # Server errors (5xx) — retry
            delay = retry_base_delay * (2 ** (attempt - 1))
            if attempt < max_retries:
                logger.warning(
                    f"Reranker attempt {attempt}/{max_retries} got "
                    f"{e.response.status_code}. Retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
            else:
                raise RuntimeError(
                    f"Reranker failed after {max_retries} attempts: {e}"
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
    """
    OSS default reranker — provider-neutral two-stage re-ranking.

    Delegates the transport to the globally configured provider
    (``RETRIEVAL_RERANK_PROVIDER``; default ``openrouter``) resolved
    through the provider factory, which supports runtime reload when the
    global settings change.
    """

    def rerank(self, query: str, chunks: List[Dict], top_n: int) -> List[Dict]:
        """
        Re-rank *chunks* by relevance to *query* and return the top *top_n*.

        Each chunk must have a ``"text"`` key.  The original chunk dicts
        are returned (not copies), preserving all metadata — chunk IDs,
        source IDs, citations and retrieval scores stay attached; only
        ``_score`` is overwritten with the provider's relevance score so
        downstream sorting/diversity filters use the reranked ordering.

        On failure the original chunks are returned truncated to *top_n*
        in their original (vector-similarity) order.
        """
        if not chunks:
            return chunks

        # Clamp top_n to available chunk count; a non-positive top_n means
        # "nothing requested" — return an empty selection without a call.
        effective_top_n = min(top_n, len(chunks))
        if effective_top_n <= 0:
            return []

        # Extract and truncate text for the API call
        documents = [c.get("text", "") for c in chunks]
        documents = _truncate_documents(documents, settings.retrieval_rerank_max_length)

        started = time.perf_counter()
        provider_name = None
        try:
            provider = get_reranker_provider()
            provider_name = provider.name
            reranker_metrics.inc_call(provider.name)
            reranker_metrics.add_documents(len(documents))
            results = provider.rank(query, documents, effective_top_n)
            if not isinstance(results, list):
                raise RerankProviderError(
                    f"Provider returned invalid result type "
                    f"{type(results).__name__} (expected a list)."
                )
        except Exception as e:
            duration_ms = (time.perf_counter() - started) * 1000
            reranker_metrics.observe_latency(duration_ms)
            reranker_metrics.inc_failure(provider_name)
            reranker_metrics.inc_fallback()
            reranker_health.record_failure(
                provider_name, str(e), category=getattr(e, "category", None)
            )
            logger.warning(
                f"Reranker failed, falling back to vector-search order: {e}"
            )
            return chunks[:effective_top_n]

        duration_ms = (time.perf_counter() - started) * 1000
        reranker_metrics.observe_latency(duration_ms)

        if not results:
            reranker_metrics.inc_fallback()
            reranker_health.record_fallback("provider returned empty results")
            logger.warning("Reranker returned empty results, using vector-search order.")
            return chunks[:effective_top_n]

        # Map results back to original chunk dicts by index. Harden: clamp
        # to effective_top_n, skip malformed/out-of-bounds/duplicate entries.
        reranked = []
        seen_indices = set()
        for r in results:
            idx, score = _coerce_result(r)
            if idx is None:
                logger.warning(
                    f"Reranker returned a malformed result {r!r} — skipping."
                )
                continue
            if not 0 <= idx < len(chunks):
                logger.warning(f"Reranker returned out-of-bounds index {idx}, skipping.")
                continue
            if idx in seen_indices:
                logger.warning(f"Reranker returned duplicate index {idx}, skipping.")
                continue
            seen_indices.add(idx)
            chunk = chunks[idx]
            # Score preservation: the original retrieval score is kept in
            # `_retrieval_score`, the provider score lands in both
            # `_rerank_score` (explicit) and `_score` (legacy key used by
            # downstream sorting/diversity filters). Fallback and disabled
            # paths leave `_score` untouched and never set `_rerank_score`.
            original_score = chunk.get("_score")
            if original_score is not None:
                chunk["_retrieval_score"] = original_score
            chunk["_rerank_score"] = score
            chunk["_score"] = score
            reranked.append(chunk)
            if len(reranked) >= effective_top_n:
                break

        if not reranked:
            # Provider "succeeded" but returned nothing usable — this is a
            # fallback situation, not a success.
            reranker_metrics.inc_fallback()
            reranker_health.record_fallback("provider returned no usable results")
            logger.warning(
                "Reranker returned no usable results, using vector-search order."
            )
            return chunks[:effective_top_n]

        reranker_metrics.inc_success(provider.name)
        reranker_health.record_success(provider.name)

        # Top score comes from the first VALID mapped entry (guaranteed
        # numeric) — results[0] itself may be a malformed entry we skipped.
        top_score_str = f"{reranked[0]['_score']:.4f}"
        logger.info(
            f"Reranker[{provider.name}]: {len(chunks)} candidates → {len(reranked)} "
            f"results in {duration_ms:.0f}ms (top score: {top_score_str})"
        )
        return reranked


# Register as default implementation
from retriva.registry import CapabilityRegistry
CapabilityRegistry().register("reranker", DefaultReranker, priority=100)
