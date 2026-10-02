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

"""Bedrock EU intent-classifier adapter (Spec 001 Phase D).

Credentials: the STANDARD AWS credential chain only (profile, workload
identity, role, or container credentials) — no static access-key or
secret-key settings exist in Retriva.  Region resolution mirrors the
accepted reranker precedence: explicit classifier setting, then
AWS_REGION, then AWS_DEFAULT_REGION, normalized once at startup.

EU residency (when required): geographic inference profiles must use
the ``eu.`` prefix — ``global.``, ``us.``, ``apac.``, ``ca.`` and every
other non-EU prefix are rejected; model-ARN/configured-region
mismatches are rejected; the runtime client is bound to the configured
EU region and direct regional model IDs execute in that region (no
cross-geography routing exists for direct foundation-model calls), so
EU-only inference is proven by the region-bound client plus ARN/model
validation (reranker pattern).  Should that proof not hold for a
configured shape, startup validation rejects it — the adapter does not
weaken residency checks to support a direct model ID.

ZDR (three-tier, no overclaim): (1) Retriva validates configuration
(region/profile/residency); (2) the accepted AWS account/project
zero-retention policy is an OPERATIONAL PREREQUISITE verified at
deployment or by a separately authorized smoke test; (3) provider-side
enforcement blocks incompatible calls.  The runtime never claims
account-policy proof from a normal inference call, never relies on a
model's default retention, and never falls back to a weaker-retention
model or route.

No per-request model or region override exists; no provider or model
fallback exists; every retry uses the exact same region and
model/profile.  Regardless of native structured-output support, Core
strictly parses and validates the C2 response locally.
"""

from __future__ import annotations

import time
from typing import Optional

from ..base import (
    ClassifierErrorCode,
    ClassifierRequest,
    IntentClassifierError,
    IntentClassifierTarget,
    IntentClassification,
    EU_PROFILE_PREFIX,
    _arn_region,
    validate_classification_payload,
)
from ..prompt import classification_system_prompt


class BedrockIntentClassifier:
    """Bedrock Converse-API transport bound to one immutable EU
    (regional) target."""

    name = "bedrock"

    def __init__(self, target: IntentClassifierTarget) -> None:
        self.target = target
        region = (target.aws_region or "").strip().lower()
        if not region:
            raise IntentClassifierError(
                ClassifierErrorCode.INVALID_REQUEST,
                "the Bedrock classifier adapter requires an AWS source "
                "region")
        if target.require_eu_residency and not region.startswith("eu-"):
            raise IntentClassifierError(
                ClassifierErrorCode.REGIONAL_POLICY_REJECTED,
                "the Bedrock classifier adapter requires an EU source "
                "region under required EU residency")
        model = target.model
        if target.require_eu_residency:
            if model.startswith(("global.", "us.", "apac.", "ca.",
                                 "sa.", "me.", "af.", "il.")):
                raise IntentClassifierError(
                    ClassifierErrorCode.REGIONAL_POLICY_REJECTED,
                    "non-EU geographic inference profiles are rejected")
            if ("." in model and not model.startswith(EU_PROFILE_PREFIX)
                    and not model.startswith("arn:")):
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
        self._client = self._build_client(region)

    def _build_client(self, region: str):
        """Build the region-bound Bedrock runtime client using the
        standard AWS credential chain (boto3 imported lazily so
        openrouter-only deployments never require boto3; no static AWS
        key settings exist)."""
        try:
            import boto3  # noqa: PLC0415 — lazy by design
        except ImportError as exc:  # pragma: no cover — env-dependent
            raise IntentClassifierError(
                ClassifierErrorCode.INVALID_REQUEST,
                "boto3 is required for the Bedrock classifier adapter"
            ) from exc
        return boto3.client("bedrock-runtime", region_name=region)

    # -- invocation -------------------------------------------------------

    def classify(self, request: ClassifierRequest,
                 cancelled: Optional[object] = None
                 ) -> IntentClassification:
        """One classification with the ratified retry policy (one
        initial attempt plus up to ``max_retries`` retries; every retry
        uses the identical region and model/profile; cancellation stops
        retries)."""
        system_prompt = classification_system_prompt(request.message)
        kwargs = {
            "modelId": self.target.model,
            "system": [{"text": system_prompt}],
            "messages": [{
                "role": "user",
                "content": [{"text":
                             "Classify the message inside the "
                             "<user_message> delimiters and answer "
                             "with the JSON object only."}],
            }],
            "inferenceConfig": {"temperature": 0,
                                 "maxTokens": 512},
        }
        attempts = 1 + max(0, int(self.target.max_retries))
        last_error: Optional[IntentClassifierError] = None
        for attempt in range(1, attempts + 1):
            if cancelled is not None and getattr(
                    cancelled, "is_set", lambda: False)():
                raise IntentClassifierError(
                    ClassifierErrorCode.CANCELLED,
                    "classification cancelled before attempt")
            try:
                response = self._client.converse(**kwargs)
            except self._client.exceptions.ModelStreamErrorException:
                last_error = IntentClassifierError(
                    ClassifierErrorCode.PROVIDER_REJECTED,
                    "classification provider rejected the request")
                self._maybe_retry(attempt, attempts)
                continue
            except Exception as exc:  # noqa: BLE001 — sanitized below
                name = type(exc).__name__
                if "Throttling" in name or "TooManyRequests" in name:
                    last_error = IntentClassifierError(
                        ClassifierErrorCode.PROVIDER_RATE_LIMITED,
                        "classification provider rate limit")
                    self._maybe_retry(attempt, attempts)
                    continue
                if "Timeout" in name:
                    last_error = IntentClassifierError(
                        ClassifierErrorCode.TIMEOUT,
                        "classification attempt timed out")
                    self._maybe_retry(attempt, attempts)
                    continue
                if "AccessDenied" in name or "UnrecognizedClient" in name \
                        or "InvalidSignature" in name or "Auth" in name:
                    raise IntentClassifierError(
                        ClassifierErrorCode.PROVIDER_REJECTED,
                        "classification provider authentication failed")
                last_error = IntentClassifierError(
                    ClassifierErrorCode.TRANSPORT_FAILED,
                    "classification transport failed")
                self._maybe_retry(attempt, attempts)
                continue
            return self._parse_response(response)
        raise IntentClassifierError(
            ClassifierErrorCode.RETRY_EXHAUSTED,
            "classification attempts exhausted") \
            if last_error is not None else IntentClassifierError(
            ClassifierErrorCode.INTERNAL_ERROR,
            "classification failed")

    def _maybe_retry(self, attempt: int, attempts: int) -> None:
        if attempt >= attempts:
            raise IntentClassifierError(
                ClassifierErrorCode.RETRY_EXHAUSTED,
                "classification attempts exhausted")
        time.sleep(float(self.target.retry_base_delay)
                   * (2 ** (attempt - 1)))

    def _parse_response(self, response: dict) -> IntentClassification:
        """Extract the model text from the Converse response and run
        the strict local C2 validation (native structured output is
        used where supported, but validation never depends on it)."""
        try:
            output = (response or {}).get("output", {})
            message = output.get("message", {})
            content_blocks = message.get("content", [])
            text = "".join(
                block.get("text", "")
                for block in content_blocks if isinstance(block, dict))
        except (AttributeError, TypeError):
            raise IntentClassifierError(
                ClassifierErrorCode.MALFORMED_RESPONSE,
                "classification provider response was malformed")
        if not text.strip():
            raise IntentClassifierError(
                ClassifierErrorCode.MALFORMED_RESPONSE,
                "classification provider response had no content")
        import json
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise IntentClassifierError(
                ClassifierErrorCode.MALFORMED_RESPONSE,
                "classification provider content was not JSON") \
                from exc
        return validate_classification_payload(payload)
