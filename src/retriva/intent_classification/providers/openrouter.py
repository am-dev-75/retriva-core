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

"""OpenRouter EU intent-classifier adapter (Spec 001 Phase D).

Only the EU regional endpoint is accepted
(``https://eu.openrouter.ai/api/v1`` under required EU residency);
the global and US endpoints, non-HTTPS URLs, URL-embedded credentials,
arbitrary proxies, and request-level URL overrides are rejected.  The
API key comes only from ``INTENT_CLASSIFIER_OPENROUTER_API_KEY`` (no
implicit fallback to any unrelated key setting); it is never logged,
returned, forwarded, or included in a request body beyond the
Authorization header.

Every request carries: the configured deployment-global model; the EU
regional endpoint; ``zdr: true``; ``data_collection: "deny"``;
``require_parameters: true``; strict structured output
(``response_format: json_schema`` with the closed C2 schema and
``additionalProperties: false``); the immutable fallback policy
(endpoint-level fallback within the same model, restricted to EU
in-region, ZDR, data-collection=deny, structured-output-eligible
endpoints — never another model, never outside the EU).

In-region fail-closed: when the selected model has no eligible EU
endpoint, classification fails rather than leaving the EU; there is no
retry against any global endpoint.  Even after provider-side
structured output, Core parses and strictly validates the response
locally (structured output never makes model output authoritative).
"""

from __future__ import annotations

import json
import time
from typing import Dict, Optional

import httpx

from retriva.config import settings

from ..base import (
    ClassifierErrorCode,
    ClassifierRequest,
    IntentClassifierError,
    IntentClassifierTarget,
    IntentClassification,
    OPENROUTER_EU_BASE_URL,
    validate_classification_payload,
)
from ..prompt import classification_system_prompt


class OpenRouterIntentClassifier:
    """OpenAI-compatible chat-completions transport against the
    OpenRouter EU regional endpoint (strict structured output)."""

    name = "openrouter"

    def __init__(self, target: IntentClassifierTarget) -> None:
        self.target = target
        base = (target.base_url or "").strip()
        if base != OPENROUTER_EU_BASE_URL and target.require_eu_residency:
            raise IntentClassifierError(
                ClassifierErrorCode.REGIONAL_POLICY_REJECTED,
                "the OpenRouter classifier adapter requires the EU "
                "regional endpoint " + OPENROUTER_EU_BASE_URL)
        if base in ("https://openrouter.ai/api/v1",
                    "https://us.openrouter.ai/api/v1"):
            raise IntentClassifierError(
                ClassifierErrorCode.REGIONAL_POLICY_REJECTED,
                "the global and US OpenRouter endpoints are rejected "
                "for the classifier")
        if not base.startswith("https://") or "@" in base:
            raise IntentClassifierError(
                ClassifierErrorCode.REGIONAL_POLICY_REJECTED,
                "the classifier endpoint must be HTTPS without "
                "embedded credentials")

    # -- request construction (immutable target only) ------------------

    def _build_payload(self, request: ClassifierRequest) -> Dict:
        """The provider request body: the versioned prompt, the
        untrusted message (embedded by the prompt builder), strict
        structured output against the closed C2 schema, and the
        immutable privacy/routing controls.  No per-request provider,
        model, endpoint, region, or routing-preference field exists."""
        system_prompt = classification_system_prompt(request.message)
        return {
            "model": self.target.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",
                 "content": "Classify the message inside the "
                            "<user_message> delimiters and answer with "
                            "the JSON object only."},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "retriva_intent_classification",
                    "strict": True,
                    "schema": _C2_JSON_SCHEMA,
                },
            },
            "provider": {
                # Immutable deployment-global routing preferences
                # (Spec 001 Phase D): endpoint-level fallback within the
                # SAME model only, restricted to EU in-region, ZDR,
                # data-collection=deny, structured-output-eligible
                # endpoints.  Fallback to another model is not
                # possible; OpenRouter EU in-region routing fails
                # closed rather than leaving the EU.
                "allow_fallbacks": bool(
                    settings.intent_classifier_openrouter_allow_fallbacks),
                "require_parameters": bool(
                    settings.intent_classifier_openrouter_require_parameters),
                "data_collection": (
                    settings.intent_classifier_openrouter_data_collection
                    or "deny"),
                "zdr": bool(
                    settings.intent_classifier_openrouter_zdr),
            },
            "temperature": 0,
            "max_tokens": 512,
        }

    def _headers(self) -> Dict[str, str]:
        """Authorization header only — the key is never logged, never
        returned, never placed in a request body or error."""
        key = (settings.intent_classifier_openrouter_api_key or "").strip()
        if not key:
            raise IntentClassifierError(
                ClassifierErrorCode.INVALID_REQUEST,
                "the OpenRouter classifier API key is not configured")
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    # -- invocation -------------------------------------------------------

    def classify(self, request: ClassifierRequest,
                 cancelled: Optional[object] = None
                 ) -> IntentClassification:
        """One classification with the ratified retry policy: one
        initial attempt plus up to ``max_retries`` retries (at most
        three attempts); every retry uses the exact same immutable
        provider, model, endpoint, residency policy, ZDR policy,
        credential source, and structured-output contract; cancellation
        stops retries.  Retryable categories only (transport failure,
        timeout, 5xx, provider rate limit); configuration, schema,
        prompt-version, malformed-output, validation, and policy errors
        are non-retryable."""
        url = f"{self.target.base_url.rstrip('/')}/chat/completions"
        payload = self._build_payload(request)
        headers = self._headers()
        attempts = 1 + max(0, int(self.target.max_retries))
        last_error: Optional[IntentClassifierError] = None
        with httpx.Client(timeout=float(self.target.timeout_seconds)) \
                as client:
            for attempt in range(1, attempts + 1):
                if cancelled is not None and getattr(
                        cancelled, "is_set", lambda: False)():
                    raise IntentClassifierError(
                        ClassifierErrorCode.CANCELLED,
                        "classification cancelled before attempt")
                try:
                    response = client.post(url, json=payload,
                                            headers=headers)
                except httpx.TimeoutException as exc:
                    last_error = IntentClassifierError(
                        ClassifierErrorCode.TIMEOUT,
                        "classification attempt timed out")
                    self._maybe_retry(
                        attempt, attempts, last_error, exc)
                    continue
                except httpx.ConnectError as exc:
                    last_error = IntentClassifierError(
                        ClassifierErrorCode.TRANSPORT_FAILED,
                        "classification transport failed")
                    self._maybe_retry(
                        attempt, attempts, last_error, exc)
                    continue
                except httpx.HTTPError as exc:
                    last_error = IntentClassifierError(
                        ClassifierErrorCode.TRANSPORT_FAILED,
                        "classification transport failed")
                    self._maybe_retry(
                        attempt, attempts, last_error, exc)
                    continue
                if response.status_code == 429:
                    last_error = IntentClassifierError(
                        ClassifierErrorCode.PROVIDER_RATE_LIMITED,
                        "classification provider rate limit")
                    self._maybe_retry(
                        attempt, attempts, last_error, None)
                    continue
                if 500 <= response.status_code < 600:
                    last_error = IntentClassifierError(
                        ClassifierErrorCode.PROVIDER_REJECTED,
                        "classification provider rejected the request")
                    self._maybe_retry(
                        attempt, attempts, last_error, None)
                    continue
                if response.status_code >= 400:
                    # Client errors are non-retryable and sanitized —
                    # the provider body never crosses the boundary.
                    raise IntentClassifierError(
                        ClassifierErrorCode.PROVIDER_REJECTED,
                        "classification provider rejected the request")
                return self._parse_response(response)
        if last_error is not None:
            raise IntentClassifierError(
                ClassifierErrorCode.RETRY_EXHAUSTED,
                "classification attempts exhausted")
        raise IntentClassifierError(
            ClassifierErrorCode.INTERNAL_ERROR,
            "classification failed")  # pragma: no cover

    def _maybe_retry(self, attempt: int, attempts: int,
                     error: IntentClassifierError,
                     exc: Optional[BaseException]) -> None:
        """Exponential backoff between retryable attempts (reranker
        pattern); the last attempt raises the retry-exhausted error."""
        if attempt >= attempts:
            raise IntentClassifierError(
                ClassifierErrorCode.RETRY_EXHAUSTED,
                "classification attempts exhausted")
        time.sleep(float(self.target.retry_base_delay)
                   * (2 ** (attempt - 1)))

    def _parse_response(self, response: httpx.Response
                        ) -> IntentClassification:
        """Strict local validation regardless of provider-side
        structured output (unknown fields, unknown schema version,
        unknown enums, missing fields, string/NaN/infinity confidence,
        contradictory combinations — all rejected)."""
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise IntentClassifierError(
                ClassifierErrorCode.MALFORMED_RESPONSE,
                "classification provider response was not JSON") \
                from exc
        if len(response.content or b"") > _MAX_RESPONSE_BYTES:
            raise IntentClassifierError(
                ClassifierErrorCode.RESPONSE_TOO_LARGE,
                "classification provider response exceeded the size "
                "limit")
        choices = (body or {}).get("choices") or []
        if not choices:
            raise IntentClassifierError(
                ClassifierErrorCode.MALFORMED_RESPONSE,
                "classification provider response had no choices")
        content = ((choices[0] or {}).get("message") or {}) \
            .get("content")
        if not isinstance(content, str) or not content.strip():
            raise IntentClassifierError(
                ClassifierErrorCode.MALFORMED_RESPONSE,
                "classification provider response had no content")
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, ValueError) as exc:
            raise IntentClassifierError(
                ClassifierErrorCode.MALFORMED_RESPONSE,
                "classification provider content was not JSON") \
                from exc
        return validate_classification_payload(payload)


#: Closed C2 JSON schema for strict structured output
#: (``additionalProperties: false``; provider ``require_parameters:
#: true``).  The response is STILL locally re-validated — structured
#: output never makes model output authoritative.
_C2_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version", "topic", "intent", "mode", "explicitness",
        "confidence", "requires_clarification", "language",
        "reason_codes"],
    "properties": {
        "schema_version": {"type": "string",
                           "enum": ["1"]},
        "topic": {"type": "string",
                  "enum": ["ACP", "QUALIFICATION", "COMPANY_IMPORT",
                           "CAMPAIGN", "DOCUMENTATION", "GENERAL"]},
        "intent": {"type": "string",
                   "enum": ["RAG_QUESTION", "WORKFLOW_DOCUMENTATION",
                            "STATUS_EXPLANATION", "CAPABILITY_QUESTION",
                            "ACP_COHORT_PROPOSAL", "ACP_COHORT_REVIEW",
                            "ACP_COHORT_APPROVAL", "ACP_GENERATION",
                            "ACP_REVIEW", "ACP_APPROVAL",
                            "ACP_ACTIVATION", "ACP_SUPERSESSION",
                            "ACP_ROLLBACK", "ACP_STATUS", "ACP_LINEAGE",
                            "ACP_EVIDENCE_ENRICHMENT",
                            "ACP_EVIDENCE_ENRICHMENT_STATUS",
                            "ACP_EVIDENCE_ACCEPTANCE",
                            "QUALIFICATION_REQUEST",
                            "QUALIFICATION_STATUS",
                            "QUALIFICATION_REVIEW",
                            "QUALIFICATION_APPROVAL",
                            "COMPANY_IMPORT_ANALYSIS",
                            "COMPANY_IMPORT_REVIEW",
                            "COMPANY_IMPORT_APPROVAL",
                            "COMPANY_IMPORT_COMMIT",
                            "COMPANY_IMPORT_STATUS",
                            "CAMPAIGN_CREATE",
                            "CAMPAIGN_AUDIENCE_ANALYSIS",
                            "CAMPAIGN_AUDIENCE_REVIEW",
                            "CAMPAIGN_AUDIENCE_APPROVAL",
                            "CAMPAIGN_AUDIENCE_COMMIT",
                            "CAMPAIGN_HISTORY_IMPORT",
                            "CAMPAIGN_MARK_ADDRESSED",
                            "CAMPAIGN_OUTCOME_UPDATE",
                            "CAMPAIGN_STATUS", "MULTI_INTENT",
                            "CLARIFICATION_REQUIRED", "AMBIGUOUS",
                            "UNSUPPORTED"]},
        "mode": {"type": "string",
                 "enum": ["INFORMATIONAL", "ANALYSIS", "MUTATION",
                          "DESTRUCTIVE_MUTATION", "UNKNOWN"]},
        "explicitness": {"type": "string",
                         "enum": ["EXPLICIT", "IMPLICIT", "AMBIGUOUS",
                                  "NEGATED", "HYPOTHETICAL",
                                  "QUOTED_EXAMPLE"]},
        "confidence": {"type": "number",
                       "minimum": 0.0, "maximum": 1.0},
        "requires_clarification": {"type": "boolean"},
        "clarification_reason": {"type": ["string", "null"],
                                 "maxLength": 200},
        "resource_reference": {"type": ["string", "null"],
                               "maxLength": 128},
        "language": {"type": "string", "enum": ["en", "it"]},
        "reason_codes": {"type": "array", "maxItems": 8},
    },
}

_MAX_RESPONSE_BYTES = 262144
