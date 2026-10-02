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

"""Provider-neutral classifier base: typed errors, canonical provider
names, the closed request/response wire schemas (C2 mirror), and the
immutable startup-built transport target (Spec 001 Phase D).

The Gateway remains the C2/taxonomy owner; this module mirrors the
accepted closed C2 vocabulary for the Core side of the wire and
validates provider responses strictly.  Nothing here selects an
application route, invokes a tool, touches workflow or confirmation
state, or exposes provider/model/endpoint/region/credential fields.
"""

from __future__ import annotations

import enum
import math
import os
import re
from typing import Dict, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)
from typing import Literal


# ---------------------------------------------------------------------------
# Canonical provider names and aliases (Phase D; NOT the reranker's set)
# ---------------------------------------------------------------------------

CANONICAL_PROVIDERS = ("openrouter", "bedrock")

#: Accepted compatibility aliases (normalized at configuration loading;
#: the reranker's ``cohere`` alias is domain-specific and NOT accepted
#: for the intent classifier).
_PROVIDER_ALIASES: Dict[str, str] = {
    "aws_bedrock": "bedrock",
    "aws-bedrock": "bedrock",
}


def canonical_provider_name(value: str) -> str:
    """Normalize a provider name to its canonical value.

    Case/whitespace-insensitive; ``aws_bedrock``/``aws-bedrock`` map to
    ``bedrock``.  Unknown names raise ``IntentClassifierError`` with
    ``invalid_request`` — startup validation surfaces the same error
    when the classifier is enabled.
    """
    raw = (value or "").strip().lower()
    canonical = _PROVIDER_ALIASES.get(raw, raw)
    if canonical not in CANONICAL_PROVIDERS:
        raise IntentClassifierError(
            ClassifierErrorCode.INVALID_REQUEST,
            f"unknown classifier provider {raw!r}; canonical values: "
            + ", ".join(CANONICAL_PROVIDERS))
    return canonical


def effective_aws_region(configured: Optional[str] = None) -> str:
    """Resolve the Bedrock source region with the accepted reranker
    precedence: explicit classifier setting, then AWS_REGION, then
    AWS_DEFAULT_REGION.  Normalized once (lowercase)."""
    region = ((configured or "").strip().lower()
              or (os.environ.get("AWS_REGION") or "").strip().lower()
              or (os.environ.get("AWS_DEFAULT_REGION") or "")
              .strip().lower())
    return region


#: The only accepted OpenRouter base URL under required EU residency.
OPENROUTER_EU_BASE_URL = "https://eu.openrouter.ai/api/v1"

#: Rejected OpenRouter base-URL shapes (fail-closed; tested).
_REJECTED_BASE_URLS = {
    "https://openrouter.ai/api/v1",
    "https://us.openrouter.ai/api/v1",
}

_AWS_REGION_RE = re.compile(r"^[a-z]{2,}-[a-z]+-\d+$")
_ARN_REGION_RE = re.compile(
    r"^arn:aws[a-z0-9-]*:bedrock:([a-z0-9-]+):")

#: EU geographic inference-profile prefix (Bedrock).
EU_PROFILE_PREFIX = "eu."


# ---------------------------------------------------------------------------
# Typed, sanitized error vocabulary (closed)
# ---------------------------------------------------------------------------

class ClassifierErrorCode(str, enum.Enum):
    """Closed classifier error vocabulary (Spec 001 Phase D).

    Sanitized: error messages never include provider bodies, prompts,
    raw messages, credentials, endpoint URLs, signed AWS requests, API
    keys, stack traces, confirmation data, tenant/principal identity, or
    resource IDs.
    """

    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    UNSUPPORTED_PROMPT_VERSION = "unsupported_prompt_version"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    REQUEST_TOO_LARGE = "request_too_large"
    CONCURRENCY_REJECTED = "concurrency_rejected"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    PROVIDER_REJECTED = "provider_rejected"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    TRANSPORT_FAILED = "transport_failed"
    MALFORMED_RESPONSE = "malformed_response"
    RESPONSE_TOO_LARGE = "response_too_large"
    INVALID_RESPONSE = "invalid_response"
    RETRY_EXHAUSTED = "retry_exhausted"
    RECURSION_REJECTED = "recursion_rejected"
    REGIONAL_POLICY_REJECTED = "regional_policy_rejected"
    CLASSIFIER_DISABLED = "classifier_disabled"
    INTERNAL_ERROR = "internal_error"


#: Error categories that are never retried (Spec 001 Phase D).
NON_RETRYABLE = frozenset({
    ClassifierErrorCode.INVALID_REQUEST,
    ClassifierErrorCode.UNSUPPORTED_SCHEMA_VERSION,
    ClassifierErrorCode.UNSUPPORTED_PROMPT_VERSION,
    ClassifierErrorCode.UNAUTHENTICATED,
    ClassifierErrorCode.FORBIDDEN,
    ClassifierErrorCode.REQUEST_TOO_LARGE,
    ClassifierErrorCode.CONCURRENCY_REJECTED,
    ClassifierErrorCode.RATE_LIMITED,
    ClassifierErrorCode.CANCELLED,
    ClassifierErrorCode.MALFORMED_RESPONSE,
    ClassifierErrorCode.RESPONSE_TOO_LARGE,
    ClassifierErrorCode.INVALID_RESPONSE,
    ClassifierErrorCode.RECURSION_REJECTED,
    ClassifierErrorCode.REGIONAL_POLICY_REJECTED,
    ClassifierErrorCode.CLASSIFIER_DISABLED,
})


class IntentClassifierError(Exception):
    """Typed, sanitized classifier transport error."""

    def __init__(self, code: ClassifierErrorCode, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Closed request schema (Gateway -> Core)
# ---------------------------------------------------------------------------

class ClassifierRequest(BaseModel):
    """The closed classification request (Spec 001 §request; C6).

    Privacy boundary: the current normalized message, closed categorical
    context, prompt identity, and a trace correlation id ONLY — never
    history, tool results, model messages, domain payloads, evidence
    bodies, file contents, tenant/principal/session/KB identifiers,
    resource identifiers, confirmation data, transport selection, or
    arbitrary metadata (constitution §29 minimization).
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1"]
    prompt_id: Literal["retriva-intent-classification"]
    prompt_version: Literal["1"]
    message: str = Field(min_length=1, max_length=2000)
    language: Literal["en", "it"]
    ambiguity_class: Literal[
        "WORKFLOW_ADJACENT", "NON_ADJACENT_AMBIGUITY"]
    workflow_family_hint: Optional[
        Literal["ACP", "QUALIFICATION", "COMPANY_IMPORT", "CAMPAIGN"]] \
        = None
    workflow_context_present: bool = False
    pending_confirmation_present: bool = False
    purpose: Literal["intent-classification"]
    correlation_id: str = Field(min_length=1, max_length=128)


# ---------------------------------------------------------------------------
# Closed response schema (C2 mirror; provider output is untrusted)
# ---------------------------------------------------------------------------

_TOPIC_VALUES = ("ACP", "QUALIFICATION", "COMPANY_IMPORT", "CAMPAIGN",
                 "DOCUMENTATION", "GENERAL")
_INTENT_VALUES = (
    "RAG_QUESTION", "WORKFLOW_DOCUMENTATION", "STATUS_EXPLANATION",
    "CAPABILITY_QUESTION",
    "ACP_COHORT_PROPOSAL", "ACP_COHORT_REVIEW", "ACP_COHORT_APPROVAL",
    "ACP_GENERATION", "ACP_REVIEW", "ACP_APPROVAL", "ACP_ACTIVATION",
    "ACP_SUPERSESSION", "ACP_ROLLBACK", "ACP_STATUS", "ACP_LINEAGE",
    "ACP_EVIDENCE_ENRICHMENT", "ACP_EVIDENCE_ENRICHMENT_STATUS",
    "ACP_EVIDENCE_ACCEPTANCE",
    "QUALIFICATION_REQUEST", "QUALIFICATION_STATUS",
    "QUALIFICATION_REVIEW", "QUALIFICATION_APPROVAL",
    "COMPANY_IMPORT_ANALYSIS", "COMPANY_IMPORT_REVIEW",
    "COMPANY_IMPORT_APPROVAL", "COMPANY_IMPORT_COMMIT",
    "COMPANY_IMPORT_STATUS",
    "CAMPAIGN_CREATE", "CAMPAIGN_AUDIENCE_ANALYSIS",
    "CAMPAIGN_AUDIENCE_REVIEW", "CAMPAIGN_AUDIENCE_APPROVAL",
    "CAMPAIGN_AUDIENCE_COMMIT", "CAMPAIGN_HISTORY_IMPORT",
    "CAMPAIGN_MARK_ADDRESSED", "CAMPAIGN_OUTCOME_UPDATE",
    "CAMPAIGN_STATUS",
    "MULTI_INTENT", "CLARIFICATION_REQUIRED", "AMBIGUOUS",
    "UNSUPPORTED",
)
_MODE_VALUES = ("INFORMATIONAL", "ANALYSIS", "MUTATION",
               "DESTRUCTIVE_MUTATION", "UNKNOWN")
_EXPLICITNESS_VALUES = ("EXPLICIT", "IMPLICIT", "AMBIGUOUS",
                        "NEGATED", "HYPOTHETICAL", "QUOTED_EXAMPLE")
_REASON_VALUES = (
    "NO_DETERMINISTIC_MATCH", "DOC_FRAMING", "NEGATION", "HYPOTHETICAL",
    "QUOTED_EXAMPLE", "CODE_BLOCK", "EXPLICIT_ACTION_VERB",
    "CAPABILITY_QUESTION", "STATUS_QUESTION", "MULTI_INTENT",
    "FOLLOWUP_CONTEXT", "UNSUPPORTED_OPERATION", "WORKFLOW_ADJACENT",
    "NON_ADJACENT_AMBIGUITY", "GUARD_RESOURCE_UNRESOLVED",
    "CONSEQUENTIAL_UNAVAILABLE",
)

Topic = Literal[tuple(_TOPIC_VALUES)]
Intent = Literal[tuple(_INTENT_VALUES)]
InteractionMode = Literal[tuple(_MODE_VALUES)]
Explicitness = Literal[tuple(_EXPLICITNESS_VALUES)]
ReasonCode = Literal[tuple(_REASON_VALUES)]


class IntentClassification(BaseModel):
    """The strict C2 classification record (schema_version "1").

    Closed fields ONLY: no provider/model/endpoint/region/credential
    field, no authorization field, no route field, no tool-argument
    field, no execution plan (Spec 001 C2).  Even after provider-side
    structured output, Core re-validates every field here — structured
    output never makes model output authoritative.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1"]
    topic: Topic
    intent: Intent
    mode: InteractionMode
    explicitness: Explicitness
    confidence: float
    requires_clarification: bool = False
    clarification_reason: Optional[str] = Field(default=None,
                                                 max_length=200)
    resource_reference: Optional[str] = Field(default=None,
                                              max_length=128)
    language: Literal["en", "it"] = "en"
    reason_codes: list = Field(default_factory=list, max_length=8)

    @field_validator("confidence")
    @classmethod
    def _confidence_finite_bounded(cls, value: float) -> float:
        """Reject NaN/infinity and out-of-range confidence (TR42-44)."""
        if not isinstance(value, (int, float)) or isinstance(
                value, bool):
            raise ValueError("confidence must be numeric")
        if math.isnan(value) or math.isinf(value):
            raise ValueError("confidence must be finite")
        if not 0.0 <= float(value) <= 1.0:
            raise ValueError("confidence must be within [0.0, 1.0]")
        return float(value)


def validate_classification_payload(payload: object) \
        -> IntentClassification:
    """Parse + strictly validate a provider response payload.

    Any violation — unknown field, unknown schema version, unknown
    enum, missing field, invalid/non-finite/out-of-range confidence —
    raises ``IntentClassifierError(INVALID_RESPONSE)``.
    """
    if not isinstance(payload, dict):
        raise IntentClassifierError(
            ClassifierErrorCode.INVALID_RESPONSE,
            "classification response must be a JSON object")
    if payload.get("schema_version") != "1":
        raise IntentClassifierError(
            ClassifierErrorCode.INVALID_RESPONSE,
            "unsupported classification schema version")
    try:
        return IntentClassification.model_validate(payload)
    except ValidationError as exc:
        raise IntentClassifierError(
            ClassifierErrorCode.INVALID_RESPONSE,
            "classification response failed strict validation") from exc


# ---------------------------------------------------------------------------
# Immutable startup-built transport target
# ---------------------------------------------------------------------------

class IntentClassifierTarget:
    """Immutable transport target bound at startup (Phase D).

    Identifies: canonical provider; model; OpenRouter regional base
    URL or Bedrock source region; permitted inference geography; ZDR
    requirement; structured-output requirement; timeout; retry
    policy.  ``classify`` accepts no transport override; request and
    response schemas carry no provider/model/endpoint/region fields.
    """

    __slots__ = (
        "provider", "model", "base_url", "aws_region",
        "inference_geography", "require_eu_residency", "require_zdr",
        "structured_output", "timeout_seconds", "max_retries",
        "retry_base_delay",
    )

    def __init__(self, *, provider: str, model: str,
                 base_url: str = "", aws_region: str = "",
                 inference_geography: str = "",
                 require_eu_residency: bool = True,
                 require_zdr: bool = True,
                 structured_output: bool = True,
                 timeout_seconds: float = 10.0,
                 max_retries: int = 1,
                 retry_base_delay: float = 0.5) -> None:
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "aws_region", aws_region)
        object.__setattr__(self, "inference_geography",
                           inference_geography)
        object.__setattr__(self, "require_eu_residency",
                           require_eu_residency)
        object.__setattr__(self, "require_zdr", require_zdr)
        object.__setattr__(self, "structured_output", structured_output)
        object.__setattr__(self, "timeout_seconds", timeout_seconds)
        object.__setattr__(self, "max_retries", max_retries)
        object.__setattr__(self, "retry_base_delay", retry_base_delay)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(
            "IntentClassifierTarget is immutable")

    def fingerprint(self) -> str:
        """Behavioral, non-secret fingerprint (canonical values only;
        credentials excluded — reranker snapshot pattern)."""
        return "|".join([
            self.provider, self.model, self.base_url, self.aws_region,
            self.inference_geography,
            str(self.require_eu_residency), str(self.require_zdr),
            str(self.structured_output), str(self.timeout_seconds),
            str(self.max_retries),
        ])


# ---------------------------------------------------------------------------
# Settings validation (selected-provider-only; Phase D)
# ---------------------------------------------------------------------------

def validate_classifier_settings(settings) -> None:
    """Validate the classifier configuration domain.

    When the classifier is DISABLED nothing is required (constitution
    §18: optional capabilities are safe by default; provider-specific
    configuration may be absent).  When ENABLED: the common settings
    validate, the provider canonicalizes, and ONLY the selected
    provider's required configuration validates — OpenRouter
    credentials are not required for bedrock; AWS/Bedrock settings are
    not required for openrouter.  Unused-provider settings never
    influence runtime behavior.
    """
    if not getattr(settings, "intent_classifier_enabled", False):
        return
    provider = canonical_provider_name(
        settings.intent_classifier_provider)
    model = (settings.intent_classifier_model or "").strip()
    if not model:
        raise IntentClassifierError(
            ClassifierErrorCode.INVALID_REQUEST,
            "INTENT_CLASSIFIER_MODEL is mandatory when the classifier "
            "is enabled (no default model exists; it is never derived "
            "from chat/visual/embedding/reranker settings)")
    timeout = float(settings.intent_classifier_timeout_seconds)
    if not 1.0 <= timeout <= 30.0:
        raise IntentClassifierError(
            ClassifierErrorCode.INVALID_REQUEST,
            "INTENT_CLASSIFIER_TIMEOUT_SECONDS must be within 1..30")
    retries = int(settings.intent_classifier_max_retries)
    if not 0 <= retries <= 2:
        raise IntentClassifierError(
            ClassifierErrorCode.INVALID_REQUEST,
            "INTENT_CLASSIFIER_MAX_RETRIES must be within 0..2")
    if int(settings.intent_classifier_max_concurrent_requests) <= 0:
        raise IntentClassifierError(
            ClassifierErrorCode.INVALID_REQUEST,
            "INTENT_CLASSIFIER_MAX_CONCURRENT_REQUESTS must be positive")
    if int(settings.intent_classifier_max_input_chars) <= 0:
        raise IntentClassifierError(
            ClassifierErrorCode.INVALID_REQUEST,
            "INTENT_CLASSIFIER_MAX_INPUT_CHARS must be positive")
    if int(settings.intent_classifier_max_request_bytes) <= 0:
        raise IntentClassifierError(
            ClassifierErrorCode.INVALID_REQUEST,
            "INTENT_CLASSIFIER_MAX_REQUEST_BYTES must be positive")
    require_eu = bool(settings.intent_classifier_require_eu_residency)
    require_zdr = bool(settings.intent_classifier_require_zdr)

    if provider == "openrouter":
        base_url = (settings.intent_classifier_openrouter_base_url
                    or "").strip()
        if require_eu:
            if base_url != OPENROUTER_EU_BASE_URL:
                raise IntentClassifierError(
                    ClassifierErrorCode.REGIONAL_POLICY_REJECTED,
                    "the only accepted OpenRouter base URL under "
                    "required EU residency is "
                    + OPENROUTER_EU_BASE_URL)
        else:
            _validate_openrouter_url(base_url)
        if require_zdr:
            if not settings.intent_classifier_openrouter_zdr:
                raise IntentClassifierError(
                    ClassifierErrorCode.REGIONAL_POLICY_REJECTED,
                    "INTENT_CLASSIFIER_OPENROUTER_ZDR may not be "
                    "disabled while INTENT_CLASSIFIER_REQUIRE_ZDR is "
                    "enabled")
            if (settings.intent_classifier_openrouter_data_collection
                    or "").strip().lower() != "deny":
                raise IntentClassifierError(
                    ClassifierErrorCode.REGIONAL_POLICY_REJECTED,
                    "INTENT_CLASSIFIER_OPENROUTER_DATA_COLLECTION "
                    "must be 'deny' under required ZDR")
        if not (settings.intent_classifier_openrouter_api_key or "").strip():
            raise IntentClassifierError(
                ClassifierErrorCode.INVALID_REQUEST,
                "INTENT_CLASSIFIER_OPENROUTER_API_KEY is required "
                "when provider=openrouter and the classifier is "
                "enabled")
    else:  # bedrock
        region = effective_aws_region(
            settings.intent_classifier_aws_region)
        if not region or not _AWS_REGION_RE.match(region):
            raise IntentClassifierError(
                ClassifierErrorCode.INVALID_REQUEST,
                "INTENT_CLASSIFIER_AWS_REGION (or AWS_REGION/"
                "AWS_DEFAULT_REGION) must be a valid AWS region")
        geography = (
            settings.intent_classifier_bedrock_inference_geography
            or "").strip().lower()
        if require_eu and geography != "eu":
            raise IntentClassifierError(
                ClassifierErrorCode.REGIONAL_POLICY_REJECTED,
                "INTENT_CLASSIFIER_BEDROCK_INFERENCE_GEOGRAPHY must "
                "be 'eu' under required EU residency")
        if require_eu and not region.startswith("eu-"):
            raise IntentClassifierError(
                ClassifierErrorCode.REGIONAL_POLICY_REJECTED,
                "the Bedrock source region must be an EU region under "
                "required EU residency")
        if require_eu:
            if model.startswith(("global.", "us.", "apac.", "ca.",
                                 "sa.", "me.", "af.", "il.")):
                raise IntentClassifierError(
                    ClassifierErrorCode.REGIONAL_POLICY_REJECTED,
                    "non-EU geographic inference profiles are rejected")
            if "." in model and not model.startswith(EU_PROFILE_PREFIX):
                raise IntentClassifierError(
                    ClassifierErrorCode.REGIONAL_POLICY_REJECTED,
                    "geographic inference profiles must use the 'eu.' "
                    "prefix under required EU residency")
            arn_region = _arn_region(model)
            if arn_region is not None and arn_region != region:
                raise IntentClassifierError(
                    ClassifierErrorCode.REGIONAL_POLICY_REJECTED,
                    "model ARN region does not match the configured "
                    "Bedrock source region")


def _validate_openrouter_url(base_url: str) -> None:
    """Fail-closed OpenRouter base-URL validation (every deployment,
    even with EU residency not required)."""
    if not base_url:
        raise IntentClassifierError(
            ClassifierErrorCode.INVALID_REQUEST,
            "INTENT_CLASSIFIER_OPENROUTER_BASE_URL is required for "
            "provider=openrouter")
    if base_url in _REJECTED_BASE_URLS:
        raise IntentClassifierError(
            ClassifierErrorCode.REGIONAL_POLICY_REJECTED,
            "the global and US OpenRouter endpoints are rejected for "
            "the classifier")
    if not base_url.startswith("https://"):
        raise IntentClassifierError(
            ClassifierErrorCode.REGIONAL_POLICY_REJECTED,
            "the classifier base URL must be HTTPS")
    if "@" in base_url:
        raise IntentClassifierError(
            ClassifierErrorCode.INVALID_REQUEST,
            "URL-embedded credentials are rejected")
    if base_url.rstrip("/") != base_url:
        base_url = base_url.rstrip("/")


def _arn_region(model: str) -> Optional[str]:
    """Extract the region of a Bedrock model ARN, if any (reranker
    pattern: bedrock.py:156-166)."""
    match = _ARN_REGION_RE.match((model or "").strip())
    return match.group(1) if match else None


def build_target_from_settings(settings) -> IntentClassifierTarget:
    """Build the immutable transport target from validated settings
    (selected provider only; no per-request override exists)."""
    validate_classifier_settings(settings)
    provider = canonical_provider_name(
        settings.intent_classifier_provider)
    model = (settings.intent_classifier_model or "").strip()
    if provider == "openrouter":
        return IntentClassifierTarget(
            provider=provider, model=model,
            base_url=(settings.intent_classifier_openrouter_base_url
                      or "").strip(),
            require_eu_residency=bool(
                settings.intent_classifier_require_eu_residency),
            require_zdr=bool(settings.intent_classifier_require_zdr),
            structured_output=True,
            timeout_seconds=float(
                settings.intent_classifier_timeout_seconds),
            max_retries=int(settings.intent_classifier_max_retries))
    return IntentClassifierTarget(
        provider=provider, model=model,
        aws_region=effective_aws_region(
            settings.intent_classifier_aws_region),
        inference_geography=(
            settings.intent_classifier_bedrock_inference_geography
            or "").strip().lower(),
        require_eu_residency=bool(
            settings.intent_classifier_require_eu_residency),
        require_zdr=bool(settings.intent_classifier_require_zdr),
        structured_output=True,
        timeout_seconds=float(settings.intent_classifier_timeout_seconds),
        max_retries=int(settings.intent_classifier_max_retries))
