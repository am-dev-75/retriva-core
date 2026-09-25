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

Canonical naming
----------------
``bedrock`` and ``openrouter`` are the canonical provider names. Friendly
aliases (``aws_bedrock``, ``aws-bedrock``, ``cohere``) normalize to the
canonical name BEFORE configuration snapshots are built, so cache
fingerprints, provider instances, logs, metrics, and status output always
carry the canonical value.

Secret handling: API keys live only inside :class:`RerankProviderConfig`
and are never logged or returned by status/health endpoints
(``public_dict()`` reports only whether a key is set). Credentials are
also excluded from cache fingerprints: they are never part of provider
construction (the OpenRouter transport reads the key at request time and
the Bedrock transport delegates to botocore's credential chain).
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

#: Canonical provider names (what the registry, cache, logs, and status use).
CANONICAL_PROVIDER_NAMES = ("openrouter", "bedrock")

#: Friendly aliases → canonical names. Normalized case/whitespace-insensitively.
PROVIDER_ALIASES = {
    "cohere": "openrouter",
    "aws_bedrock": "bedrock",
    "aws-bedrock": "bedrock",
}


class RerankProviderError(RuntimeError):
    """Raised by providers when reranking fails (transient or fatal).

    ``category`` is a stable, safe error category (see the Bedrock
    provider's normalization table) suitable for health reporting — raw
    provider messages never reach the status API.
    """

    def __init__(self, message: str, category: Optional[str] = None):
        super().__init__(message)
        self.category = category


class RerankProvider:
    """Interface every reranking transport must implement."""

    #: Short identifier stored on instances and used in logs/health.
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


def canonical_provider_name(raw: Any) -> str:
    """Normalize *raw* and map aliases to the canonical provider name.

    Empty input returns ``""`` (the factory applies the default); accepted
    aliases are normalized case/whitespace-insensitively:
    ``" AWS_Bedrock "`` → ``"bedrock"``.
    """
    name = _normalize_provider_name(raw)
    return PROVIDER_ALIASES.get(name, name)


def _coerce_float(value: Any, default: float):
    """Coerce *value* to float, falling back to *default* on bad input.

    Returns ``(coerced, note)``: ``note`` is ``None`` for clean values and
    a sanitized description when defaulting was applied (for startup
    warnings; never echoes secrets — these settings are not secrets).
    """
    try:
        return float(value), None
    except (TypeError, ValueError):
        raw = repr(value)
        if len(raw) > 40:
            raw = raw[:37] + "..."
        return default, f"invalid numeric value {raw}; using default {default}"


def _coerce_int(value: Any, default: int):
    """Coerce *value* to int, falling back to *default* on bad input."""
    try:
        return int(value), None
    except (TypeError, ValueError):
        raw = repr(value)
        if len(raw) > 40:
            raw = raw[:37] + "..."
        return default, f"invalid numeric value {raw}; using default {default}"


def _parse_regions(raw: Any) -> Tuple[str, ...]:
    """Parse a comma-separated region list into a lowercase tuple."""
    if not raw:
        return ()
    if isinstance(raw, str):
        parts = raw.split(",")
    else:
        parts = list(raw)
    return tuple(r.strip().lower() for r in parts if str(r).strip())


@dataclass(frozen=True)
class RerankProviderConfig:
    """Immutable snapshot of the globally configured reranking provider.

    Built from Retriva's single global settings object; the fingerprint is
    used by the factory to detect configuration changes at runtime and
    rebuild the provider.

    Fingerprint policy: every behavioral non-secret setting is included;
    credentials (``api_key``) are EXCLUDED — the OpenRouter transport reads
    the key at request time and the Bedrock transport delegates to
    botocore's credential chain, so key rotation never requires provider
    reconstruction.
    """

    provider: str
    model: str
    base_url: str = ""
    api_key: Optional[str] = None
    aws_region: str = ""
    timeout: float = 30.0
    max_retries: int = 2
    retry_base_delay: float = 1.0
    enforce_eu_region: bool = False
    allowed_aws_regions: Tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, settings: Any) -> "RerankProviderConfig":
        """Build a config snapshot from a global Settings object."""
        cfg, _ = build_config_snapshot(settings)
        return cfg

    def fingerprint(self) -> tuple:
        """Hashable identity of the effective configuration (no secrets)."""
        return (
            self.provider,
            self.model,
            self.base_url,
            self.aws_region,
            self.enforce_eu_region,
            self.allowed_aws_regions,
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
            "enforce_eu_region": self.enforce_eu_region,
            "allowed_aws_regions": list(self.allowed_aws_regions) or None,
        }


def build_config_snapshot(settings: Any) -> Tuple[RerankProviderConfig, List[str]]:
    """Build a config snapshot plus sanitized coercion notes.

    Numeric settings that fail to parse are defaulted (backward-compatible
    non-strict behavior) and produce a note naming the setting, the
    sanitized raw value (truncated) and the applied default — for the
    startup warning and degraded-configuration status.
    """
    notes: List[str] = []

    timeout, note = _coerce_float(
        getattr(settings, "retrieval_rerank_timeout", 30.0), 30.0
    )
    if note:
        notes.append(f"RETRIEVAL_RERANK_TIMEOUT: {note}")
    max_retries, note = _coerce_int(
        getattr(settings, "retrieval_rerank_max_retries", 2), 2
    )
    if note:
        notes.append(f"RETRIEVAL_RERANK_MAX_RETRIES: {note}")
    retry_base_delay, note = _coerce_float(
        getattr(settings, "retrieval_rerank_retry_base_delay", 1.0), 1.0
    )
    if note:
        notes.append(f"RETRIEVAL_RERANK_RETRY_BASE_DELAY: {note}")

    cfg = RerankProviderConfig(
        provider=canonical_provider_name(
            getattr(settings, "retrieval_rerank_provider", "") or ""
        ),
        model=getattr(settings, "retrieval_rerank_model", "") or "",
        base_url=getattr(settings, "retrieval_rerank_base_url", "") or "",
        api_key=getattr(settings, "retrieval_rerank_api_key", None),
        aws_region=(getattr(settings, "retrieval_rerank_aws_region", "") or "").strip().lower(),
        timeout=timeout,
        max_retries=max_retries,
        retry_base_delay=retry_base_delay,
        enforce_eu_region=bool(
            getattr(settings, "retrieval_rerank_enforce_eu_region", False)
        ),
        allowed_aws_regions=_parse_regions(
            getattr(settings, "retrieval_rerank_allowed_aws_regions", "")
        ),
    )
    return cfg, notes


def sanitize_chunks_for_api(chunks: List[Dict]) -> List[Dict]:
    """Return copies of *chunks* with internal reranking fields removed.

    Internal fields ``_rerank_score`` and ``_retrieval_score`` never leave
    the process through public APIs. ``_score`` is preserved: it was part
    of external retrieval responses before the reranking subsystem existed
    (backward compatibility).
    """
    _INTERNAL_KEYS = ("_rerank_score", "_retrieval_score")
    out = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            out.append(chunk)
            continue
        if any(k in chunk for k in _INTERNAL_KEYS):
            chunk = {k: v for k, v in chunk.items() if k not in _INTERNAL_KEYS}
        out.append(chunk)
    return out