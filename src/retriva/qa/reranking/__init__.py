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
Provider-neutral reranking primitives.

Layering (top to bottom)::

    Reranker protocol (retriva.protocols)          — domain interface
        DefaultReranker (retriva.qa.reranker)      — "reranker" capability,
                                                     adapter to the pipeline
            RerankProvider (this package)          — provider SPI
                OpenRouterRerankProvider           — Cohere-compatible /rerank
                BedrockRerankProvider              — Amazon Bedrock Rerank

The provider is selected globally through Retriva's single settings system
(``retriva.config.settings``, driven by environment variables). It applies to
every knowledge base, retrieval operation, user and customer — there is no
knowledge-base-specific reranker configuration.
"""

from retriva.qa.reranking.base import (
    RerankProvider,
    RerankProviderConfig,
    RerankProviderError,
)
from retriva.qa.reranking.factory import (
    DEFAULT_PROVIDER_NAME,
    build_rerank_provider,
    get_reranker_provider,
    provider_names,
    register_rerank_provider,
    resolve_provider_name,
    validate_rerank_config,
)
from retriva.qa.reranking.health import RerankerHealth, reranker_health
from retriva.qa.reranking.metrics import RerankerMetrics, reranker_metrics

__all__ = [
    "DEFAULT_PROVIDER_NAME",
    "RerankProvider",
    "RerankProviderConfig",
    "RerankProviderError",
    "RerankerHealth",
    "RerankerMetrics",
    "build_rerank_provider",
    "get_reranker_provider",
    "provider_names",
    "register_rerank_provider",
    "reranker_health",
    "reranker_metrics",
    "resolve_provider_name",
    "validate_rerank_config",
]


def get_reranker_status() -> dict:
    """Aggregate reranker status for health reporting.

    Never includes secret values — ``RerankProviderConfig.public_dict()``
    reports only whether an API key is set.
    """
    from retriva.config import settings

    provider = None
    config = None
    if settings.enable_retrieval_reranking:
        try:
            p = get_reranker_provider()
            provider = getattr(p, "name", None)
        except Exception:
            provider = None
        config = RerankProviderConfig.from_settings(settings).public_dict()
    return {
        "enabled": settings.enable_retrieval_reranking,
        "provider": provider,
        "config": config,
        "health": reranker_health.snapshot(),
        "metrics": reranker_metrics.snapshot(),
        "validation_issues": validate_rerank_config(settings),
    }


def refresh_reranker_health() -> None:
    """Recompute the static parts of reranker health (startup/reload hook)."""
    from retriva.config import settings

    if not settings.enable_retrieval_reranking:
        reranker_health.configure(enabled=False, provider=None)
        return
    try:
        name = resolve_provider_name(RerankProviderConfig.from_settings(settings))
    except Exception:
        name = None
    reranker_health.configure(enabled=True, provider=name)
