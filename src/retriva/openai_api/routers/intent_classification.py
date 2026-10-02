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

"""Internal intent-classification endpoint (Spec 001 Phase D; C6).

``POST /v1/intent/classification`` — service-to-service only:

- the Gateway is the only ordinary caller; this is NOT a public
  user-facing classification service;
- dedicated service credential (``X-Service-Token`` =
  ``INTENT_CLASSIFIER_SERVICE_AUTH_TOKEN``, constant-time compare —
  the accepted internal-service pattern; missing/malformed fails
  closed; the token is never reused from another service);
- accepted purpose marker required
  (``X-Retriva-Internal-Purpose: intent-classification`` — absent or
  wrong fails closed; recursion attempts execute nothing);
- ``application/json`` only (415 otherwise); no CORS; no HTML;
  browser access is forbidden and technically enforced — obscurity and
  path naming are NOT access control (exclusion from the public
  OpenAPI is enforced by authentication + internal network placement);
- request-size limit (default 32768 bytes → typed error), input-char
  limit (default 2000, deterministic truncation);
- concurrency semaphore (default 8 → typed busy; no server queue) and
  a per-caller token-bucket rate limit (default 120/min → typed
  429-equivalent);
- timeout applied server-side; cancellation is cooperative (client
  disconnect aborts the upstream provider call; nothing is persisted
  or replayed);
- content-free logging only (safe error category, counters, latency,
  correlation ID) — prompts, messages, and full responses are never
  logged;
- error sanitization: typed codes only — never provider bodies,
  prompts, credentials, secret-bearing URLs, signed requests, or stack
  traces.

Trust-boundary invariant (spec C6): calling this endpoint directly
cannot bypass Gateway routing, trusted-principal resolution, tenant
resolution, workflow-state validation, explicit-intent guards, tool
authorization, or audit.  The endpoint has NO tool registry access,
accepts no tenant/user/session identity, performs no business
mutation, and returns only an advisory classification record the
Gateway re-validates.  Core never selects RAG, the agent loop,
clarification, a workflow, a tool, a confirmation, or an application
route.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import threading
import time
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger

from retriva.config import settings
from retriva.intent_classification.base import (
    ClassifierErrorCode,
    ClassifierRequest,
    IntentClassifierError,
    validate_classification_payload,
)
from retriva.intent_classification.factory import get_intent_classifier
from retriva.intent_classification.prompt import (
    PROMPT_ID,
    PROMPT_VERSION,
)

router = APIRouter(tags=["intent-classification"])

#: Accepted purpose marker (architecture §6c; no-recursion control).
PURPOSE_MARKER = "intent-classification"

_ERROR_STATUS = {
    ClassifierErrorCode.INVALID_REQUEST: 400,
    ClassifierErrorCode.UNSUPPORTED_SCHEMA_VERSION: 400,
    ClassifierErrorCode.UNSUPPORTED_PROMPT_VERSION: 400,
    ClassifierErrorCode.UNAUTHENTICATED: 401,
    ClassifierErrorCode.FORBIDDEN: 403,
    ClassifierErrorCode.RECURSION_REJECTED: 403,
    ClassifierErrorCode.REQUEST_TOO_LARGE: 413,
    ClassifierErrorCode.CONCURRENCY_REJECTED: 429,
    ClassifierErrorCode.RATE_LIMITED: 429,
    ClassifierErrorCode.TIMEOUT: 504,
    ClassifierErrorCode.CANCELLED: 499,
    ClassifierErrorCode.CLASSIFIER_DISABLED: 503,
    ClassifierErrorCode.REGIONAL_POLICY_REJECTED: 503,
}


#: Static, content-free message per closed error code — dynamic
#: exception text never crosses the boundary (no provider bodies,
#: prompts, messages, credentials, endpoints, or stack traces).
_SAFE_MESSAGES = {
    ClassifierErrorCode.INVALID_REQUEST:
        "classification request rejected",
    ClassifierErrorCode.UNSUPPORTED_SCHEMA_VERSION:
        "unsupported classification schema version",
    ClassifierErrorCode.UNSUPPORTED_PROMPT_VERSION:
        "unsupported classification prompt version",
    ClassifierErrorCode.UNAUTHENTICATED:
        "valid service credentials are required",
    ClassifierErrorCode.FORBIDDEN:
        "classification access denied",
    ClassifierErrorCode.RECURSION_REJECTED:
        "the accepted internal purpose marker is required",
    ClassifierErrorCode.REQUEST_TOO_LARGE:
        "classification request exceeded the size limit",
    ClassifierErrorCode.CONCURRENCY_REJECTED:
        "classification concurrency limit reached",
    ClassifierErrorCode.RATE_LIMITED:
        "classification rate limit exceeded",
    ClassifierErrorCode.TIMEOUT:
        "classification timed out",
    ClassifierErrorCode.CANCELLED:
        "classification cancelled",
    ClassifierErrorCode.PROVIDER_REJECTED:
        "classification provider rejected the request",
    ClassifierErrorCode.PROVIDER_RATE_LIMITED:
        "classification provider rate limit",
    ClassifierErrorCode.TRANSPORT_FAILED:
        "classification transport failed",
    ClassifierErrorCode.MALFORMED_RESPONSE:
        "classification provider response was malformed",
    ClassifierErrorCode.RESPONSE_TOO_LARGE:
        "classification provider response exceeded the size limit",
    ClassifierErrorCode.INVALID_RESPONSE:
        "classification response failed strict validation",
    ClassifierErrorCode.RETRY_EXHAUSTED:
        "classification attempts exhausted",
    ClassifierErrorCode.REGIONAL_POLICY_REJECTED:
        "classification regional policy rejected the configuration",
    ClassifierErrorCode.CLASSIFIER_DISABLED:
        "the intent classifier is not enabled",
    ClassifierErrorCode.INTERNAL_ERROR:
        "classification failed",
}


def _typed_error(code: ClassifierErrorCode, message: Optional[str],
                 correlation_id: str) -> JSONResponse:
    """Sanitized typed error: closed code + STATIC content-free
    message (dynamic exception text is never echoed)."""
    status = _ERROR_STATUS.get(code, 500)
    safe = _SAFE_MESSAGES.get(code, "classification failed")
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code.value, "message": safe,
                            "correlation_id": correlation_id}})


class _TokenBucket:
    """Minimal per-caller token bucket (deployment-global capacity,
    refill from the configured rate)."""

    def __init__(self, rate_per_minute: int) -> None:
        self.rate = max(1, int(rate_per_minute))
        self.tokens = float(self.rate)
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def take(self) -> bool:
        now = time.monotonic()
        with self.lock:
            self.tokens = min(
                float(self.rate),
                self.tokens + (now - self.updated) * (self.rate / 60.0))
            self.updated = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False


_rate_buckets: dict = {}
_rate_lock = threading.Lock()

_concurrency_semaphore: Optional[threading.Semaphore] = None
_concurrency_lock = threading.Lock()


def _semaphore() -> threading.Semaphore:
    global _concurrency_semaphore
    with _concurrency_lock:
        if _concurrency_semaphore is None:
            _concurrency_semaphore = threading.Semaphore(
                max(1, int(settings.intent_classifier_max_concurrent_requests)))
        return _concurrency_semaphore


def _bucket_for(caller_key: str) -> _TokenBucket:
    with _rate_lock:
        bucket = _rate_buckets.get(caller_key)
        if bucket is None:
            bucket = _TokenBucket(
                settings.intent_classifier_rate_limit_per_minute)
            _rate_buckets[caller_key] = bucket
        return bucket


@router.post("/v1/intent/classification")
async def classify_intent(request: Request):
    correlation_id = request.headers.get("X-Correlation-ID", "")[:128]
    started = time.monotonic()

    # --- Trust boundary: content type -------------------------------
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" not in content_type:
        return _typed_error(
            ClassifierErrorCode.INVALID_REQUEST,
            "application/json is required", correlation_id)

    # --- Trust boundary: dedicated service credential ---------------
    token = request.headers.get("X-Service-Token") or ""
    expected = settings.intent_classifier_service_auth_token or ""
    if not expected or not hmac.compare_digest(token, expected):
        return _typed_error(
            ClassifierErrorCode.UNAUTHENTICATED,
            "valid service credentials are required", correlation_id)

    # --- Trust boundary: accepted purpose marker ---------------------
    purpose = request.headers.get("X-Retriva-Internal-Purpose") or ""
    if purpose != PURPOSE_MARKER:
        # Missing or wrong purpose fails closed; a recursion attempt
        # executes nothing.
        return _typed_error(
            ClassifierErrorCode.RECURSION_REJECTED,
            "the accepted internal purpose marker is required",
            correlation_id)

    # --- Limits: rate (per caller) and concurrency -------------------
    # Caller key: a non-reversible digest of the credential (in-process
    # dictionary key only — never logged, never returned).
    caller_key = "svc:" + hashlib.sha256(
        token.encode("utf-8")).hexdigest()[:16]
    if not _bucket_for(caller_key).take():
        return _typed_error(
            ClassifierErrorCode.RATE_LIMITED,
            "classification rate limit exceeded", correlation_id)
    semaphore = _semaphore()
    if not semaphore.acquire(blocking=False):
        return _typed_error(
            ClassifierErrorCode.CONCURRENCY_REJECTED,
            "classification concurrency limit reached", correlation_id)
    try:
        # --- Limits: request size --------------------------------------
        body = await request.body()
        if len(body) > int(settings.intent_classifier_max_request_bytes):
            return _typed_error(
                ClassifierErrorCode.REQUEST_TOO_LARGE,
                "classification request exceeded the size limit",
                correlation_id)

        # --- Closed request schema ---------------------------------------
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 — sanitized
            return _typed_error(
                ClassifierErrorCode.INVALID_REQUEST,
                "classification request must be valid JSON",
                correlation_id)
        max_chars = int(settings.intent_classifier_max_input_chars)
        if isinstance(payload, dict) and isinstance(
                payload.get("message"), str):
            # Deterministic truncation to the configured bound.
            payload["message"] = payload["message"][:max_chars]
        if (isinstance(payload, dict)
                and payload.get("prompt_version")
                != settings.intent_classifier_prompt_version):
            return _typed_error(
                ClassifierErrorCode.UNSUPPORTED_PROMPT_VERSION,
                "unsupported classification prompt version",
                correlation_id)
        try:
            classifier_request = ClassifierRequest.model_validate(payload)
        except Exception:  # noqa: BLE001 — sanitized
            return _typed_error(
                ClassifierErrorCode.INVALID_REQUEST,
                "classification request failed strict validation",
                correlation_id)

        # --- Classifier availability -------------------------------------
        classifier = get_intent_classifier()
        if classifier is None:
            return _typed_error(
                ClassifierErrorCode.CLASSIFIER_DISABLED,
                "the intent classifier is not enabled", correlation_id)

        # --- Cooperative cancellation + server-side timeout ------------
        cancelled = threading.Event()

        async def _run():
            return await asyncio.to_thread(
                classifier.classify, classifier_request, cancelled)

        try:
            classification = await asyncio.wait_for(
                _run(),
                timeout=float(settings.intent_classifier_timeout_seconds)
                * (1 + max(0, int(settings.intent_classifier_max_retries)))
                + 2.0)
        except asyncio.TimeoutError:
            return _typed_error(
                ClassifierErrorCode.TIMEOUT,
                "classification timed out", correlation_id)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        except IntentClassifierError as exc:
            return _typed_error(exc.code, exc.message, correlation_id)
        except Exception:  # noqa: BLE001 — sanitized
            return _typed_error(
                ClassifierErrorCode.INTERNAL_ERROR,
                "classification failed", correlation_id)

        # --- Strict response validation (defense in depth) --------------
        try:
            record = validate_classification_payload(
                classification.model_dump())
        except IntentClassifierError as exc:
            return _typed_error(exc.code, exc.message, correlation_id)

        logger.info(
            f"[{correlation_id}] intent_classification ok "
            f"latency={time.monotonic() - started:.2f}s "
            f"prompt={PROMPT_ID}/v{PROMPT_VERSION}")
        return JSONResponse(content=record.model_dump())

    finally:
        semaphore.release()
