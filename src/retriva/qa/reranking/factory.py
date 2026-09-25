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
Reranking provider factory.

Selection precedence (global, not per-KB):

1. ``RETRIEVAL_RERANK_PROVIDER`` (or ``settings.retrieval_rerank_provider``)
   — explicit selection. Case-insensitive, empty means default.
2. Default: ``"openrouter"`` (the Cohere-compatible ``/rerank`` transport
   used by every existing deployment — preserves current behavior).
3. Unknown names fail fast with the list of registered providers.

Runtime reload: the factory caches the built provider keyed by a
fingerprint of the effective :class:`RerankProviderConfig`. When the global
settings change (e.g. programmatically by extensions/tests, or after an
operator mutates the settings object), the next ``get_reranker_provider()``
call detects the new fingerprint and rebuilds the provider. Environment
variables are read once at process start by pydantic-settings, so env-only
changes still require a process restart.
"""

import threading
from typing import Any, Callable, Dict, List, Optional

from retriva.config import settings
from retriva.logger import get_logger
from retriva.qa.reranking.base import (
    RerankProvider,
    RerankProviderConfig,
    RerankProviderError,
    _normalize_provider_name,
)

logger = get_logger(__name__)

#: Provider used when nothing is explicitly configured (legacy behavior).
DEFAULT_PROVIDER_NAME = "openrouter"

#: Providers registered under additional names (same implementation).
_PROVIDER_ALIASES: Dict[str, str] = {"cohere": "openrouter"}

ProviderFactory = Callable[[RerankProviderConfig], RerankProvider]

_registered: Dict[str, ProviderFactory] = {}
# RLock: builtin registration runs while _ensure_builtin_providers holds the
# lock, and register_rerank_provider re-acquires it.
_registration_lock = threading.RLock()
_builtins_loaded = False


# -- registration ----------------------------------------------------------


def register_rerank_provider(name: str, factory_fn: ProviderFactory) -> None:
    """Register a provider implementation under *name*.

    Extensions (Retriva Pro) can add transports here without touching the
    pipeline: the globally selected name resolves through this registry.
    """
    normalized = _normalize_provider_name(name)
    if not normalized:
        raise ValueError("rerank provider name must not be empty")
    with _registration_lock:
        _registered[normalized] = factory_fn
    logger.debug(f"Registered rerank provider '{normalized}' ← {factory_fn.__name__}")


def _register_builtin_providers() -> None:
    from retriva.qa.reranking.providers import bedrock, openrouter

    register_rerank_provider(DEFAULT_PROVIDER_NAME, openrouter._build)
    # Cohere-compatible endpoints are the same transport under another name.
    register_rerank_provider("cohere", openrouter._build)
    register_rerank_provider("bedrock", bedrock._build)


def _ensure_builtin_providers() -> None:
    """Import builtin provider modules once (registers openrouter/bedrock)."""
    global _builtins_loaded
    if _builtins_loaded:
        return
    with _registration_lock:
        if _builtins_loaded:
            return
        # Deferred imports avoid import cycles with retriva.qa.reranker and
        # keep boto3 optional until a Bedrock provider is actually built.
        _register_builtin_providers()
        _builtins_loaded = True


def provider_names() -> List[str]:
    """Names of all registered provider implementations."""
    _ensure_builtin_providers()
    with _registration_lock:
        return sorted(_registered.keys())


# -- resolution ------------------------------------------------------------


def resolve_provider_name(config: RerankProviderConfig) -> str:
    """Resolve the effective provider name with aliases applied.

    Normalizes case/whitespace so configs built directly (not via
    ``from_settings``) resolve identically.
    """
    name = _normalize_provider_name(config.provider) or DEFAULT_PROVIDER_NAME
    return _PROVIDER_ALIASES.get(name, name)


def build_rerank_provider(config: RerankProviderConfig) -> RerankProvider:
    """Instantiate the provider selected by *config*.

    Raises :class:`RerankProviderError` when the name is unknown.
    """
    _ensure_builtin_providers()
    name = resolve_provider_name(config)
    with _registration_lock:
        factory_fn = _registered.get(name)
    if factory_fn is None:
        raise RerankProviderError(
            f"Unknown rerank provider '{config.provider}' "
            f"(resolved: '{name}'). Registered providers: "
            f"{', '.join(sorted(_registered.keys()))}. Set "
            f"RETRIEVAL_RERANK_PROVIDER to one of them."
        )
    return factory_fn(config)


# -- cached instance with runtime reload ------------------------------------

_cache_lock = threading.Lock()
_cached_fingerprint: Optional[tuple] = None
_cached_provider: Optional[RerankProvider] = None


def get_reranker_provider(settings_obj: Any = None) -> RerankProvider:
    """Return the provider for the current global settings, rebuilding on change.

    Cheap per-call check: a config snapshot + tuple comparison. The instance
    is rebuilt only when the effective configuration changed (runtime reload).
    """
    global _cached_fingerprint, _cached_provider

    cfg = RerankProviderConfig.from_settings(settings_obj or settings)
    fingerprint = cfg.fingerprint()

    with _cache_lock:
        if _cached_provider is not None and _cached_fingerprint == fingerprint:
            return _cached_provider

    provider = build_rerank_provider(cfg)

    with _cache_lock:
        _cached_fingerprint = fingerprint
        _cached_provider = provider
    return provider


def reset_provider_cache() -> None:
    """Drop the cached provider — for testing only."""
    global _cached_fingerprint, _cached_provider
    with _cache_lock:
        _cached_fingerprint = None
        _cached_provider = None


# -- startup validation -----------------------------------------------------


def validate_rerank_config(settings_obj: Any = None) -> List[Dict]:
    """Validate the reranking configuration without contacting any provider.

    Returns a list of issues: ``{"level": "error"|"warning", "message": str}``.
    Startup logs these (non-fatal, matching Retriva's log-and-continue
    convention); the first runtime call applies the standard fallback policy.
    """
    s = settings_obj or settings
    issues: List[Dict] = []

    if not getattr(s, "enable_retrieval_reranking", True):
        return issues

    cfg = RerankProviderConfig.from_settings(s)

    try:
        name = resolve_provider_name(cfg)
    except Exception as exc:  # pragma: no cover — defensive
        return [{"level": "error", "message": f"Rerank provider resolution failed: {exc}"}]

    _ensure_builtin_providers()
    with _registration_lock:
        known = name in _registered
    if not known:
        issues.append(
            {
                "level": "error",
                "message": (
                    f"Unknown RETRIEVAL_RERANK_PROVIDER '{cfg.provider}'. "
                    f"Registered providers: {', '.join(sorted(_registered.keys()))}."
                ),
            }
        )
        return issues

    if name == "bedrock":
        from retriva.qa.reranking.providers.bedrock import effective_aws_region

        if not (cfg.model or "").strip():
            issues.append(
                {
                    "level": "error",
                    "message": "Bedrock reranker requires RETRIEVAL_RERANK_MODEL "
                    "(e.g. 'amazon.rerank-v1:0' or a full model ARN).",
                }
            )
        if not effective_aws_region(cfg):
            issues.append(
                {
                    "level": "error",
                    "message": "Bedrock reranker requires an AWS region: set "
                    "RETRIEVAL_RERANK_AWS_REGION, AWS_REGION or AWS_DEFAULT_REGION.",
                }
            )
        if cfg.api_key:
            issues.append(
                {
                    "level": "warning",
                    "message": "RETRIEVAL_RERANK_API_KEY is ignored by the bedrock "
                    "provider; credentials come from the standard AWS chain "
                    "(env vars, shared config, or the workload IAM role).",
                }
            )
    else:
        if not (cfg.base_url or "").strip():
            issues.append(
                {
                    "level": "error",
                    "message": f"The '{name}' rerank provider requires "
                    "RETRIEVAL_RERANK_BASE_URL.",
                }
            )
        if not (cfg.api_key or "").strip():
            issues.append(
                {
                    "level": "warning",
                    "message": f"No API key configured for the '{name}' rerank "
                    "provider (RETRIEVAL_RERANK_API_KEY / OPENROUTER_OPENAI_API_KEY); "
                    "requests will fail until one is set.",
                }
            )

    return issues
