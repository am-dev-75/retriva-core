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
Reranking provider SPI (service-provider interface).

A :class:`RerankProvider` turns ``(query, documents, top_n)`` into raw
relevance results in the Cohere ``/rerank`` result shape::

    {"index": int, "relevance_score": float, ...}

where ``index`` refers to the position of the document in the submitted
list. This is the neutral internal currency shared by every provider;
``DefaultReranker`` maps results back onto the original chunk dicts.

Secret handling: API keys live only inside :class:`RerankProviderConfig`
and are never logged or returned by status/health endpoints
(``public_dict()`` reports only whether a key is set).
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


class RerankProviderError(RuntimeError):
    """Raised by providers when reranking fails (transient or fatal)."""


class RerankProvider:
    """Interface every reranking transport must implement."""

    #: Short identifier stored on instances and used in logs/health ("openrouter").
    name: str = "abstract"

    def rank(self, query: str, documents: List[str], top_n: int) -> List[Dict]:
        """Return relevance results for *documents* against *query*.

        Implementations MUST:

        * return at most ``top_n`` results;
        * use 0-based indices referring to the *documents* list positions;
        * raise :class:`RerankProviderError` on failure instead of
          returning partial/garbage results — fallback to vector order is
          decided by the caller, never by the provider.
        """
        raise NotImplementedError


def _normalize_provider_name(raw: Any) -> str:
    """Normalize a provider selection value ('', ' Bedrock ' → 'bedrock')."""
    return str(raw or "").strip().lower()


def _coerce_float(value: Any, default: float) -> float:
    """Coerce *value* to float, falling back to *default* on bad input.

    Hardening: a non-numeric env override (e.g. RETRIEVAL_RERANK_TIMEOUT=abc)
    must fail-safe to the default instead of raising on every provider
    snapshot (which would silently disable reranking).
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int) -> int:
    """Coerce *value* to int, falling back to *default* on bad input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class RerankProviderConfig:
    """Immutable snapshot of the globally configured reranking provider.

    Built from Retriva's single global settings object; the fingerprint is
    used by the factory to detect configuration changes at runtime and
    rebuild the provider.
    """

    provider: str
    model: str
    base_url: str = ""
    api_key: Optional[str] = None
    aws_region: str = ""
    timeout: float = 30.0
    max_retries: int = 2
    retry_base_delay: float = 1.0

    @classmethod
    def from_settings(cls, settings: Any) -> "RerankProviderConfig":
        """Build a config snapshot from a global Settings object."""
        return cls(
            provider=_normalize_provider_name(
                getattr(settings, "retrieval_rerank_provider", "") or ""
            ),
            model=getattr(settings, "retrieval_rerank_model", "") or "",
            base_url=getattr(settings, "retrieval_rerank_base_url", "") or "",
            api_key=getattr(settings, "retrieval_rerank_api_key", None),
            aws_region=getattr(settings, "retrieval_rerank_aws_region", "") or "",
            timeout=_coerce_float(
                getattr(settings, "retrieval_rerank_timeout", 30.0), 30.0
            ),
            max_retries=_coerce_int(
                getattr(settings, "retrieval_rerank_max_retries", 2), 2
            ),
            retry_base_delay=_coerce_float(
                getattr(settings, "retrieval_rerank_retry_base_delay", 1.0), 1.0
            ),
        )

    def fingerprint(self) -> tuple:
        """Hashable identity of the effective configuration."""
        return (
            self.provider,
            self.model,
            self.base_url,
            self.api_key,
            self.aws_region,
            self.timeout,
            self.max_retries,
            self.retry_base_delay,
        )

    def public_dict(self) -> Dict:
        """Redacted view safe for status endpoints (no secret values)."""
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url or None,
            "aws_region": self.aws_region or None,
            "api_key_set": bool(self.api_key),
            "timeout_seconds": self.timeout,
            "max_retries": self.max_retries,
            "retry_base_delay_seconds": self.retry_base_delay,
        }
