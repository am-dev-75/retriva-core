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
OpenRouter / Cohere-compatible rerank provider.

Implements the ``openrouter`` provider name (alias ``"cohere"``) against
any endpoint exposing the Cohere-compatible ``POST /rerank`` contract —
OpenRouter, Cohere, Jina, or self-hosted equivalents — selected via
``RETRIEVAL_RERANK_BASE_URL``.

This is the transport every existing deployment already uses: the HTTP
call, retry policy and batch handling live in ``retriva.qa.reranker``
(``_call_rerank_api`` / ``_rerank_batched``) and are intentionally shared
with the legacy module so behavior — and the patch points used by its
tests — are preserved exactly.
"""

from typing import Dict, List

from retriva.qa import reranker as _legacy_transport
from retriva.qa.reranking.base import RerankProvider, RerankProviderConfig


class OpenRouterRerankProvider(RerankProvider):
    """Cohere-compatible ``/rerank`` transport (OpenRouter default)."""

    name = "openrouter"

    def __init__(self, config: RerankProviderConfig):
        # Kept for parity with other providers; request-time values are read
        # through the legacy transport module so configuration changes made
        # there (and existing test patch points) keep working.
        self.config = config

    def rank(self, query: str, documents: List[str], top_n: int) -> List[Dict]:
        batch_size = _legacy_transport.settings.retrieval_rerank_batch_size
        return _legacy_transport._rerank_batched(query, documents, top_n, batch_size)


def _build(config: RerankProviderConfig) -> OpenRouterRerankProvider:
    return OpenRouterRerankProvider(config)
