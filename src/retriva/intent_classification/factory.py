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

"""Classifier factory: selected-provider-only adapter construction
(Spec 001 Phase D; reranker factory pattern, qa/reranking/factory.py).

Startup order (ratified): normalize the provider -> validate the common
configuration -> validate ONLY the selected provider's required
configuration -> construct ONLY the selected provider's adapter ->
bind provider, model, endpoint or region, residency policy, ZDR
policy, timeout, retry policy, and structured-output requirements into
an immutable transport target -> expose no per-request override.

OpenRouter credentials are never required when provider=bedrock; AWS
credentials and Bedrock-specific configuration are never required when
provider=openrouter.  Unused-provider settings never influence runtime
behavior.  The factory caches the built classifier keyed by the
behavioral fingerprint of the validated target (credentials excluded);
tests may inject deterministic fakes.  No cross-provider fallback
exists anywhere in this package.
"""

from __future__ import annotations

import threading
from typing import Dict

from retriva.config import settings

from .base import (
    ClassifierErrorCode,
    ClassifierRequest,
    IntentClassifierError,
    IntentClassifierTarget,
    IntentClassification,
    build_target_from_settings,
    canonical_provider_name,
)
from .prompt import classification_system_prompt

_BUILDER_LOCK = threading.RLock()
_BUILT: Dict[str, object] = {}
_BUILTIN_LOADED = False


class _ClassifierAdapterProtocol:
    """Structural protocol (documentation only — the adapters are
    duck-typed).  classify(request) accepts no transport override."""

    __slots__ = ()

    def classify(self, request: ClassifierRequest,
                 ) -> IntentClassification:
        raise NotImplementedError


def _build_openrouter(target: IntentClassifierTarget):
    from .providers.openrouter import OpenRouterIntentClassifier
    return OpenRouterIntentClassifier(target)


def _build_bedrock(target: IntentClassifierTarget):
    from .providers.bedrock import BedrockIntentClassifier
    return BedrockIntentClassifier(target)


_BUILDERS = {
    "openrouter": _build_openrouter,
    "bedrock": _build_bedrock,
}


def _ensure_builtin_adapters() -> None:
    global _BUILTIN_LOADED
    if _BUILTIN_LOADED:
        return
    with _BUILDER_LOCK:
        if _BUILTIN_LOADED:
            return
        _BUILTIN_LOADED = True


def get_intent_classifier(force: bool = False):
    """Return the process-global classifier transport (or None when the
    classifier is disabled).

    The transport is built ONCE from the immutable validated target;
    the fingerprint (canonical provider, model, endpoint/region,
    policies, timeout, retries — credentials excluded) keys the cache
    exactly like the accepted reranker factory.  ``force=True`` (tests
    only) rebuilds from current settings.
    """
    if not getattr(settings, "intent_classifier_enabled", False):
        return None
    _ensure_builtin_adapters()
    target = build_target_from_settings(settings)
    fingerprint = target.fingerprint()
    with _BUILDER_LOCK:
        if force or fingerprint not in _BUILT:
            provider = canonical_provider_name(
                settings.intent_classifier_provider)
            builder = _BUILDERS.get(provider)
            if builder is None:  # pragma: no cover — validated earlier
                raise IntentClassifierError(
                    ClassifierErrorCode.INVALID_REQUEST,
                    "no adapter registered for provider "
                    f"{provider!r}")
            _BUILT.clear()  # exactly one selected adapter at a time
            _BUILT[fingerprint] = builder(target)
        return _BUILT[fingerprint]


def set_classifier_for_tests(adapter) -> None:
    """Test-only: install a deterministic fake transport (the default
    suite never performs a real provider call)."""
    global _BUILTIN_LOADED
    with _BUILDER_LOCK:
        _BUILT.clear()
        _BUILTIN_LOADED = True
        if adapter is not None:
            _BUILT["test"] = adapter


__all__ = [
    "get_intent_classifier",
    "set_classifier_for_tests",
    "classification_system_prompt",
]
