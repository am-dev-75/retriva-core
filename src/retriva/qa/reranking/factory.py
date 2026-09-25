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
   — explicit selection. Case/whitespace-insensitive; empty means default;
   aliases (``aws_bedrock``, ``aws-bedrock``, ``cohere``) normalize to the
   canonical names ``bedrock`` / ``openrouter`` BEFORE any snapshot is
   built, so cache fingerprints, instances, logs, metrics and status
   always use canonical values.
2. Default: ``"openrouter"`` (the Cohere-compatible ``/rerank`` transport
   used by every existing deployment — preserves current behavior).
3. Unknown names fail fast with the list of registered providers.

Runtime reload: the factory caches the built provider keyed by a
fingerprint of the effective :class:`RerankProviderConfig` (behavioral,
non-secret settings only — credentials excluded). When the global
settings change, the next ``get_reranker_provider()`` call detects the
new fingerprint and rebuilds the provider under a lock (thread-safe:
exactly one build per config change even under concurrent retrieval).
Environment variables are read once at process start by
pydantic-settings, so env-only changes still require a process restart
(AWS *credential* environment changes are detected separately by the
Bedrock provider, which rebuilds its client without freezing
credentials).
"""

import os
import threading
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from retriva.config import settings
from retriva.logger import get_logger
from retriva.qa.reranking.base import (
    RerankProvider,
    RerankProviderConfig,
    RerankProviderError,
    canonical_provider_name,
    build_config_snapshot,
)

logger = get_logger(__name__)

#: Provider used when nothing is explicitly configured (legacy behavior).
DEFAULT_PROVIDER_NAME = "openrouter"

#: Aliases are applied inside :func:`canonical_provider_name` (base.py) so
#: every snapshot, fingerprint, instance, log and status output carries the
#: canonical name. Kept here only for backward-compatible name resolution
#: of configs built directly (not via ``from_settings``).
_PROVIDER_ALIASES: Dict[str, str] = {
    "cohere": "openrouter",
    "aws_bedrock": "bedrock",
    "aws-bedrock": "bedrock",
}

ProviderFactory = Callable[[RerankProviderConfig], RerankProvider]

_registered: Dict[str, ProviderFactory] = {}
# RLock: builtin registration runs while _ensure_builtin_providers holds the
# lock, and register_rerank_provider re-acquires it.
_registration_lock = threading.RLock()
_builtins_loaded = False


class RerankStartupError(RuntimeError):
    """Raised when strict startup validation rejects the rerank config."""


# -- registration ----------------------------------------------------------


def register_rerank_provider(name: str, factory_fn: ProviderFactory) -> None:
    """Register a provider implementation under *name*.

    Extensions (Retriva Pro) can add transports here without touching the
    pipeline: the globally selected name resolves through this registry.
    """
    normalized = canonical_provider_name(name)
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
    """Canonical names of all registered provider implementations."""
    _ensure_builtin_providers()
    with _registration_lock:
        return sorted(_registered.keys())


# -- resolution ------------------------------------------------------------


def resolve_provider_name(config: RerankProviderConfig) -> str:
    """Resolve the effective provider name with aliases applied."""
    name = canonical_provider_name(config.provider) or DEFAULT_PROVIDER_NAME
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
    is rebuilt only when the effective configuration changed (runtime
    reload); the build happens under the cache lock, so concurrent
    retrievals share exactly one provider instance per configuration.
    """
    global _cached_fingerprint, _cached_provider

    cfg, _ = build_config_snapshot(settings_obj or settings)
    fingerprint = cfg.fingerprint()

    with _cache_lock:
        if _cached_provider is not None and _cached_fingerprint == fingerprint:
            return _cached_provider
        # Built under the lock: fast (no network), and guarantees a single
        # provider instance per fingerprint even under concurrency.
        provider = build_rerank_provider(cfg)
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

    Returns a list of issues: ``{"level": "error"|"warning",
    "strict_fatal": bool, "message": str}``. Startup logs these
    (non-fatal in non-strict mode, matching Retriva's log-and-continue
    convention); :func:`enforce_rerank_startup` raises in strict mode.

    Never performs an AWS request: Bedrock checks are static (region
    resolution, ARN shape) plus a bundled botocore service-model inspection.
    """
    s = settings_obj or settings
    issues: List[Dict] = []

    if not getattr(s, "enable_retrieval_reranking", True):
        return issues

    cfg, notes = build_config_snapshot(s)
    for note in notes:
        issues.append(
            {
                "level": "warning",
                "strict_fatal": True,
                "message": f"Invalid numeric setting defaulted — {note}. "
                f"Fix the value or startup fails in strict mode.",
            }
        )

    try:
        name = resolve_provider_name(cfg)
    except Exception as exc:  # pragma: no cover — defensive
        return [
            {
                "level": "error",
                "strict_fatal": True,
                "message": f"Rerank provider resolution failed: {exc}",
            }
        ]

    _ensure_builtin_providers()
    with _registration_lock:
        known = name in _registered
    if not known:
        issues.append(
            {
                "level": "error",
                "strict_fatal": True,
                "message": (
                    f"Unknown RETRIEVAL_RERANK_PROVIDER '{cfg.provider}'. "
                    f"Registered providers: {', '.join(sorted(_registered.keys()))}."
                ),
            }
        )
        return issues

    if name == "bedrock":
        from retriva.qa.reranking.providers.bedrock import (
            arn_region,
            effective_aws_region,
            verify_rerank_operation_available,
        )

        if not (cfg.model or "").strip():
            issues.append(
                {
                    "level": "error",
                    "strict_fatal": True,
                    "message": "Bedrock reranker requires RETRIEVAL_RERANK_MODEL "
                    "(e.g. 'amazon.rerank-v1:0', 'cohere.rerank-v3-5:0' or a "
                    "full model ARN).",
                }
            )
        region = effective_aws_region(cfg)
        if not region:
            issues.append(
                {
                    "level": "error",
                    "strict_fatal": True,
                    "message": "Bedrock reranker requires an AWS region: set "
                    "RETRIEVAL_RERANK_AWS_REGION, AWS_REGION or "
                    "AWS_DEFAULT_REGION.",
                }
            )
        # SDK operation check (no network: bundled service model only).
        try:
            verify_rerank_operation_available()
        except RerankProviderError as exc:
            issues.append(
                {
                    "level": "error",
                    "strict_fatal": True,
                    "message": str(exc),
                }
            )
        if cfg.api_key:
            issues.append(
                {
                    "level": "warning",
                    "strict_fatal": False,
                    "message": "RETRIEVAL_RERANK_API_KEY is ignored by the bedrock "
                    "provider; credentials come from the standard AWS chain "
                    "(env vars, shared config, or the workload IAM role).",
                }
            )
        # EU region enforcement (opt-in).
        if cfg.enforce_eu_region:
            issues.extend(_validate_eu_policy(cfg, region, name))
    else:
        if not (cfg.model or "").strip():
            issues.append(
                {
                    "level": "error",
                    "strict_fatal": True,
                    "message": f"The '{name}' rerank provider requires "
                    "RETRIEVAL_RERANK_MODEL.",
                }
            )
        problem = _validate_base_url(cfg.base_url)
        if problem:
            issues.append(
                {
                    "level": "error",
                    "strict_fatal": True,
                    "message": f"The '{name}' rerank provider RETRIEVAL_RERANK_BASE_URL "
                    f"{problem}.",
                }
            )
        if not (cfg.api_key or "").strip():
            issues.append(
                {
                    "level": "warning",
                    "strict_fatal": False,
                    "message": f"No API key configured for the '{name}' rerank "
                    "provider (RETRIEVAL_RERANK_API_KEY / OPENROUTER_OPENAI_API_KEY); "
                    "requests will fail until one is set.",
                }
            )

    if cfg.enforce_eu_region and name != "bedrock":
        issues.append(
            {
                "level": "warning",
                "strict_fatal": True,
                "message": f"RETRIEVAL_RERANK_ENFORCE_EU_REGION applies only to "
                f"the bedrock provider; it is ignored for '{name}'.",
            }
        )

    return issues


def _validate_base_url(base_url: str) -> Optional[str]:
    """Return a problem description for an invalid endpoint, else None."""
    try:
        parsed = urlparse((base_url or "").strip())
    except ValueError:
        return "is not a valid URL"
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "is not a valid absolute http(s) URL"
    return None


def _validate_eu_policy(
    cfg: RerankProviderConfig, region: Optional[str], name: str
) -> List[Dict]:
    """Static EU-region-policy validation (opt-in, bedrock provider)."""
    issues: List[Dict] = []
    allowed = tuple(cfg.allowed_aws_regions)
    if not allowed:
        issues.append(
            {
                "level": "error",
                "strict_fatal": True,
                "message": "RETRIEVAL_RERANK_ENFORCE_EU_REGION is enabled but "
                "RETRIEVAL_RERANK_ALLOWED_AWS_REGIONS is empty; configure at "
                "least one allowed region (e.g. eu-central-1).",
            }
        )
        return issues
    if region and region not in allowed:
        issues.append(
            {
                "level": "error",
                "strict_fatal": True,
                "message": f"EU region policy violation: configured region "
                f"'{region}' is not in RETRIEVAL_RERANK_ALLOWED_AWS_REGIONS "
                f"({', '.join(allowed)}).",
            }
        )
    if (cfg.model or "").strip().startswith("arn:aws"):
        from retriva.qa.reranking.providers.bedrock import arn_region

        arn_reg = arn_region(cfg.model)
        if arn_reg and region and arn_reg != region:
            issues.append(
                {
                    "level": "error",
                    "strict_fatal": True,
                    "message": f"EU region policy violation: model ARN belongs "
                    f"to region '{arn_reg}' but the client region is "
                    f"'{region}'.",
                }
            )
    for env in ("AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_BEDROCK_AGENT_RUNTIME"):
        if os.environ.get(env):
            issues.append(
                {
                    "level": "error",
                    "strict_fatal": True,
                    "message": f"EU region policy violation: endpoint override "
                    f"{env} is set while RETRIEVAL_RERANK_ENFORCE_EU_REGION "
                    f"is enabled.",
                }
            )
    return issues


def enforce_rerank_startup(settings_obj: Any = None) -> List[Dict]:
    """Validate the reranking configuration at startup.

    Non-strict mode (default, backward compatible): returns the issues for
    log-and-continue handling; numeric defaulting is surfaced as a degraded
    configuration status.

    Strict mode (``RETRIEVAL_RERANK_STRICT_STARTUP_VALIDATION=true``):
    raises :class:`RerankStartupError` for invalid static settings,
    unsupported providers, missing regions, unsupported SDK operations,
    invalid endpoints, invalid numeric settings, and EU-region-policy
    violations. Never makes an AWS request.
    """
    s = settings_obj or settings
    strict = bool(getattr(s, "retrieval_rerank_strict_startup_validation", False))
    issues = validate_rerank_config(s)

    from retriva.qa.reranking.health import reranker_health

    if not strict:
        for issue in issues:
            if issue.get("strict_fatal"):
                reranker_health.record_config_issue(issue["message"])
        return issues

    fatal = [i for i in issues if i["level"] == "error" or i.get("strict_fatal")]
    if fatal:
        raise RerankStartupError(
            "Retrieval reranking configuration failed strict startup "
            "validation: " + " | ".join(i["message"] for i in fatal)
        )
    return issues