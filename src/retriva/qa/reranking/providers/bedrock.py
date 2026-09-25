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
Amazon Bedrock rerank provider.

Exact AWS API (confirmed against the botocore service model; see
``tests/test_reranker_bedrock_api_model.py``):

* client:   ``boto3.client("bedrock-agent-runtime")``  (service
  ``Bedrock Agent Runtime`` — the ONLY service exposing ``Rerank``)
* operation: ``Rerank``
* request:  ``queries`` (``TEXT`` query), ``sources`` (``INLINE`` /
  ``TEXT`` documents), ``rerankingConfiguration`` with
  ``type='BEDROCK_RERANKING_MODEL'``,
  ``bedrockRerankingConfiguration.modelConfiguration.modelArn`` and an
  explicit ``numberOfResults``
* response: ``results[].index``, ``results[].relevanceScore`` (plus
  ``results[].document``)

``boto3`` is imported lazily so deployments that never select
``RETRIEVAL_RERANK_PROVIDER=bedrock`` do not require it at runtime.
botocore gained ``Rerank`` in 1.35.72 — the minimum enforced by
requirements and the SDK model test.

Secret handling: credentials are NOT part of Retriva settings and are
NEVER passed to ``boto3.client()`` — the standard AWS credential chain
applies (``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` /
``AWS_SESSION_TOKEN`` env vars, shared credentials/config files, or the
workload IAM role). botocore remains fully responsible for resolving and
refreshing temporary credentials; this provider caches only the *client
object* and rebuilds it when the region or the relevant AWS environment
variables change (values are compared in memory only, never logged or
exposed). Region precedence: ``RETRIEVAL_RERANK_AWS_REGION`` >
``AWS_REGION`` > ``AWS_DEFAULT_REGION``.

``RETRIEVAL_RERANK_MODEL`` accepts either a bare reranking model id
(e.g. ``amazon.rerank-v1:0``, ``cohere.rerank-v3-5:0``, resolved to the
region's foundation-model ARN) or a full model ARN (used verbatim).

EU region enforcement (``RETRIEVAL_RERANK_ENFORCE_EU_REGION``): rejects
non-allowed regions, model-ARN/configured-region mismatches, and unsafe
endpoint overrides at rank time with a policy violation error — no
rerouting, no fallback to another provider.
"""

import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from retriva.logger import get_logger
from retriva.qa.reranking.base import (
    RerankProvider,
    RerankProviderConfig,
    RerankProviderError,
)

logger = get_logger(__name__)

#: The ONLY botocore service that exposes the Rerank operation (verified
#: against the bundled service models; see the SDK model CI test).
BEDROCK_SERVICE_NAME = "bedrock-agent-runtime"

#: Minimum botocore/boto3 version exposing Rerank (botocore 1.35.72
#: introduced the operation in bedrock-agent-runtime).
BEDROCK_RERANK_MIN_SDK_VERSION = "1.35.72"

_ARN_REGION_RE = re.compile(r"^arn:aws[a-z0-9-]*:bedrock:([a-z0-9-]+):")

# Safe error categories (never include raw AWS messages in the status API).
CATEGORY_ACCOUNT_VERIFICATION_PENDING = "account_verification_pending"
CATEGORY_AUTHENTICATION_ERROR = "authentication_error"
CATEGORY_AUTHORIZATION_ERROR = "authorization_error"
CATEGORY_CREDENTIALS_EXPIRED = "credentials_expired"
CATEGORY_MODEL_ACCESS_ERROR = "model_access_error"
CATEGORY_MARKETPLACE_ENTITLEMENT_ERROR = "marketplace_entitlement_error"
CATEGORY_MODEL_NOT_FOUND = "model_not_found"
CATEGORY_INVALID_REQUEST = "invalid_request"
CATEGORY_THROTTLED = "throttled"
CATEGORY_TIMEOUT = "timeout"
CATEGORY_SERVICE_UNAVAILABLE = "service_unavailable"
CATEGORY_NETWORK_ERROR = "network_error"
CATEGORY_INVALID_RESPONSE = "invalid_response"

ALL_ERROR_CATEGORIES = (
    CATEGORY_ACCOUNT_VERIFICATION_PENDING,
    CATEGORY_AUTHENTICATION_ERROR,
    CATEGORY_AUTHORIZATION_ERROR,
    CATEGORY_CREDENTIALS_EXPIRED,
    CATEGORY_MODEL_ACCESS_ERROR,
    CATEGORY_MARKETPLACE_ENTITLEMENT_ERROR,
    CATEGORY_MODEL_NOT_FOUND,
    CATEGORY_INVALID_REQUEST,
    CATEGORY_THROTTLED,
    CATEGORY_TIMEOUT,
    CATEGORY_SERVICE_UNAVAILABLE,
    CATEGORY_NETWORK_ERROR,
    CATEGORY_INVALID_RESPONSE,
)

_ACCOUNT_VERIFICATION_MARKER = "your account is currently being verified"

_AUTHENTICATION_CODES = {
    "UnauthorizedException",
    "UnrecognizedClientException",
    "InvalidClientTokenException",
    "InvalidSignatureException",
    "InvalidAccessKeyIdException",
    "InvalidIdentityTokenException",
}
_CREDENTIALS_EXPIRED_CODES = {"ExpiredTokenException", "ExpiredToken"}
_MODEL_ACCESS_CODES = {
    "ModelNotReadyException",
    "ModelNotVisibleException",
    "ModelNotAccessableException",
    "ModelNotManifestedException",
    "ModelStreamErrorException",
}
_THROTTLING_CODES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceQuotaExceededException",
}
_SERVICE_UNAVAILABLE_CODES = {
    "InternalServerException",
    "BadGatewayException",
    "DependencyFailedException",
    "ServiceUnavailable",
    "ServiceUnavailableException",
}
_INVALID_REQUEST_CODES = {"ValidationException", "ConflictException"}


def effective_aws_region(config: RerankProviderConfig) -> Optional[str]:
    """Resolve the AWS region for Bedrock: setting > AWS_REGION > AWS_DEFAULT_REGION."""
    return (
        (config.aws_region or "").strip().lower()
        or os.environ.get("AWS_REGION", "").strip().lower()
        or os.environ.get("AWS_DEFAULT_REGION", "").strip().lower()
        or None
    )


def model_arn(model: str, region: str) -> str:
    """Return *model* as a full Bedrock model ARN (passthrough for ARNs)."""
    model = (model or "").strip()
    if model.startswith("arn:aws"):
        return model
    return f"arn:aws:bedrock:{region}::foundation-model/{model}"


def arn_region(arn: str) -> Optional[str]:
    """Extract the region component from a Bedrock model ARN, if any."""
    match = _ARN_REGION_RE.match((arn or "").strip())
    return match.group(1) if match else None


def verify_rerank_operation_available() -> None:
    """Verify the installed SDK exposes the Rerank operation (no network).

    Inspects the bundled botocore service model — no client is created and
    no request is made. Raises :class:`RerankProviderError` with category
    ``invalid_response`` if the operation or its required shapes are
    missing (SDK too old / incompatible).
    """
    try:
        import botocore
        from botocore.session import get_session
    except ImportError as exc:
        raise RerankProviderError(
            "The bedrock rerank provider requires 'boto3' (>= "
            f"{BEDROCK_RERANK_MIN_SDK_VERSION}). Install it or pick another provider.",
            category=CATEGORY_INVALID_RESPONSE,
        ) from exc

    try:
        model = get_session().get_service_model(BEDROCK_SERVICE_NAME)
        op = model.operation_model("Rerank")
        input_members = op.input_shape.members
        rr = input_members["rerankingConfiguration"]
        brc = rr.members["bedrockRerankingConfiguration"]
        output_members = op.output_shape.members
        result_members = output_members["results"].member.members
    except Exception as exc:
        raise RerankProviderError(
            f"The installed botocore ({botocore.__version__}) does not expose "
            f"the Rerank operation on '{BEDROCK_SERVICE_NAME}'. Minimum "
            f"required: botocore/boto3 >= {BEDROCK_RERANK_MIN_SDK_VERSION}.",
            category=CATEGORY_INVALID_RESPONSE,
        ) from exc

    missing = []
    for name in ("queries", "sources", "rerankingConfiguration"):
        if name not in input_members:
            missing.append(f"request.{name}")
    if "numberOfResults" not in rr.members["bedrockRerankingConfiguration"].members:
        missing.append("request.rerankingConfiguration.bedrockRerankingConfiguration.numberOfResults")
    if "results" not in output_members:
        missing.append("response.results")
    for name in ("index", "relevanceScore"):
        if name not in result_members:
            missing.append(f"response.results[].{name}")
    if missing:
        raise RerankProviderError(
            "The installed botocore Rerank model is missing required shapes: "
            + ", ".join(missing)
            + f" (botocore {botocore.__version__}, minimum "
            f"{BEDROCK_RERANK_MIN_SDK_VERSION}).",
            category=CATEGORY_INVALID_RESPONSE,
        )


def classify_bedrock_error(exc: Exception) -> Tuple[str, str]:
    """Map an AWS SDK exception to a safe (category, message) pair.

    The message contains only the category and the AWS error code — never
    the raw AWS message — so it is safe for logs that may be aggregated and
    for the status API.
    """
    code = getattr(exc, "__class__.__name__", type(exc).__name__)
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error_info = response.get("Error") or {}
        code = error_info.get("Code", code)
    message = str(exc)
    lowered = message.lower()

    if "your account is currently being verified" in lowered:
        return (
            CATEGORY_ACCOUNT_VERIFICATION_PENDING,
            "AWS account verification pending — the Bedrock Rerank API is "
            "unavailable until AWS completes account verification.",
        )
    if code in _CREDENTIALS_EXPIRED_CODES:
        return (
            CATEGORY_CREDENTIALS_EXPIRED,
            f"credentials_expired (aws_error_code={code})",
        )
    if code in _AUTHENTICATION_CODES:
        return (
            CATEGORY_AUTHENTICATION_ERROR,
            f"authentication_error (aws_error_code={code})",
        )
    if code == "AccessDeniedException":
        if "marketplace" in lowered or "entitlement" in lowered:
            return (
                CATEGORY_MARKETPLACE_ENTITLEMENT_ERROR,
                f"marketplace_entitlement_error (aws_error_code={code})",
            )
        if any(
            marker in lowered
            for marker in ("entitlement", "subscription", "offer", "not authorized to access model")
        ):
            return (
                CATEGORY_MODEL_ACCESS_ERROR,
                f"model_access_error (aws_error_code={code})",
            )
        return (
            CATEGORY_AUTHORIZATION_ERROR,
            f"authorization_error (aws_error_code={code})",
        )
    if code in _MODEL_ACCESS_CODES:
        return (
            CATEGORY_MODEL_ACCESS_ERROR,
            f"model_access_error (aws_error_code={code})",
        )
    if code == "ResourceNotFoundException":
        return CATEGORY_MODEL_NOT_FOUND, f"model_not_found (aws_error_code={code})"
    if code in _THROTTLING_CODES:
        return CATEGORY_THROTTLED, f"throttled (aws_error_code={code})"
    if code in _SERVICE_UNAVAILABLE_CODES:
        return (
            CATEGORY_SERVICE_UNAVAILABLE,
            f"service_unavailable (aws_error_code={code})",
        )
    if code in _INVALID_REQUEST_CODES:
        return CATEGORY_INVALID_REQUEST, f"invalid_request (aws_error_code={code})"

    name = type(exc).__name__
    if name in ("NoCredentialsError", "ProfileNotFound", "CredentialRetrievalError"):
        return (
            CATEGORY_AUTHENTICATION_ERROR,
            f"authentication_error (error_type={name}): no AWS credentials "
            "resolved — configure AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, "
            "a shared config file, or the workload IAM role.",
        )
    if name in ("ReadTimeoutError", "ConnectTimeoutError", "InsecureSignatureProblemError") or isinstance(exc, TimeoutError):
        return CATEGORY_TIMEOUT, f"timeout ({name})"
    if name in (
        "EndpointConnectionError",
        "ConnectionError",
        "ConnectionClosedError",
        "ReadTimeoutError",
    ) or isinstance(exc, ConnectionError):
        return CATEGORY_NETWORK_ERROR, f"network_error ({name})"

    return (
        CATEGORY_SERVICE_UNAVAILABLE,
        f"service_unavailable (error_type={name})",
    )


class BedrockRerankProvider(RerankProvider):
    """Amazon Bedrock Rerank transport (``bedrock-agent-runtime`` client)."""

    name = "bedrock"

    #: Environment variables that, when changed, invalidate the cached
    #: client (values are held in memory only — never logged or exposed).
    _CREDENTIAL_ENV_KEYS = (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
        "AWS_ENDPOINT_URL",
        "AWS_ENDPOINT_URL_BEDROCK_AGENT_RUNTIME",
    )

    def __init__(self, config: RerankProviderConfig, client: Optional[Any] = None):
        """
        Args:
            config: Effective provider configuration.
            client: Pre-built boto3 client — testing hook; when ``None``
                a client is created lazily on first use and reused.
        """
        self.config = config
        self._client = client
        self._client_env: Optional[Tuple[Tuple[str, Optional[str]], ...]] = None
        self._arn_cache: Optional[str] = None

    # -- client management ---------------------------------------------------

    def _region(self) -> str:
        region = effective_aws_region(self.config)
        if not region:
            raise RerankProviderError(
                "Bedrock reranker has no AWS region: set "
                "RETRIEVAL_RERANK_AWS_REGION, AWS_REGION or AWS_DEFAULT_REGION.",
                category=CATEGORY_INVALID_REQUEST,
            )
        return region

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:
                raise RerankProviderError(
                    "The bedrock rerank provider requires 'boto3' (>= "
                    f"{BEDROCK_RERANK_MIN_SDK_VERSION}). Install it or pick "
                    "another provider.",
                    category=CATEGORY_INVALID_RESPONSE,
                ) from exc

            region = self._region()
            timeout = max(1.0, float(self.config.timeout))
            # Credentials are NEVER passed here: botocore's standard chain
            # resolves (and refreshes, for temporary credentials) them.
            self._client = boto3.client(
                BEDROCK_SERVICE_NAME,
                region_name=region,
                config=Config(
                    read_timeout=timeout,
                    connect_timeout=min(10.0, timeout),
                    # max_attempts counts the initial attempt, matching the
                    # HTTPX provider's semantics (default 2 total attempts);
                    # no application-level retry loop is wrapped around it,
                    # so retries never multiply.
                    retries={
                        "max_attempts": max(1, self.config.max_retries),
                        "mode": "adaptive",
                    },
                ),
            )
            # Remember the credential-relevant env snapshot so a changed
            # environment (key rotation without restart) triggers a client
            # rebuild. Values live in memory only. (None = externally
            # injected client — never rebuilt.)
            self._client_env = self._env_snapshot()
            logger.debug(
                f"Bedrock rerank client initialized "
                f"(service={BEDROCK_SERVICE_NAME}, region={region})."
            )
        elif (
            self._client_env is not None
            and self._env_snapshot() != self._client_env
        ):
            # Credentials/endpoint config changed in the environment —
            # rebuild the client so botocore re-resolves them. Temporary
            # credential refresh is still botocore's responsibility.
            logger.debug("AWS environment changed — rebuilding Bedrock client.")
            self._client = None
            return self._get_client()
        return self._client

    def _env_snapshot(self) -> Tuple[Tuple[str, Optional[str]], ...]:
        return tuple(
            (key, os.environ.get(key)) for key in self._CREDENTIAL_ENV_KEYS
        )

    # -- EU region policy ------------------------------------------------------

    def _enforce_region_policy(self, region: str) -> None:
        """Reject policy violations at rank time (no rerouting, no
        fallback to another provider)."""
        if not self.config.enforce_eu_region:
            return
        allowed = tuple(self.config.allowed_aws_regions)
        if allowed and region not in allowed:
            raise RerankProviderError(
                f"EU region policy violation: resolved region '{region}' is "
                f"not in RETRIEVAL_RERANK_ALLOWED_AWS_REGIONS.",
                category=CATEGORY_INVALID_REQUEST,
            )
        arn = self._arn_cache or ""
        arn_reg = arn_region(arn)
        if arn_reg and arn_reg != region:
            raise RerankProviderError(
                f"EU region policy violation: model ARN belongs to region "
                f"'{arn_reg}' but the client region is '{region}'.",
                category=CATEGORY_INVALID_REQUEST,
            )
        for env in ("AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_BEDROCK_AGENT_RUNTIME"):
            if os.environ.get(env):
                raise RerankProviderError(
                    f"EU region policy violation: endpoint override "
                    f"{env} is not allowed while enforcement is enabled.",
                    category=CATEGORY_INVALID_REQUEST,
                )

    # -- provider SPI ----------------------------------------------------------

    def rank(self, query: str, documents: List[str], top_n: int) -> List[Dict]:
        if not documents:
            # Deterministic no-op: never send an empty sources list to the API.
            return []

        client = self._get_client()
        region = self._region()

        if self._arn_cache is None:
            self._arn_cache = model_arn(self.config.model, region)
        self._enforce_region_policy(region)

        payload = {
            "queries": [{"type": "TEXT", "textQuery": {"text": query}}],
            "sources": [
                {
                    "type": "INLINE",
                    "inlineDocumentSource": {
                        "type": "TEXT",
                        "textDocument": {"text": doc},
                    },
                }
                for doc in documents
            ],
            "rerankingConfiguration": {
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {"modelArn": self._arn_cache},
                    "numberOfResults": max(1, min(int(top_n), len(documents))),
                },
            },
        }

        started = time.perf_counter()
        try:
            response = client.rerank(**payload)
        except RerankProviderError:
            raise
        except Exception as exc:
            category, safe_message = classify_bedrock_error(exc)
            raise RerankProviderError(safe_message, category=category) from exc

        duration_ms = (time.perf_counter() - started) * 1000
        raw_results = response.get("results", []) if isinstance(response, dict) else []
        results = []
        for r in raw_results:
            if not isinstance(r, dict):
                raise RerankProviderError(
                    f"Bedrock rerank returned malformed result {r!r}: "
                    f"expected an object.",
                    category=CATEGORY_INVALID_RESPONSE,
                )
            idx = r.get("index")
            # bool is an int subclass — reject it explicitly (same policy
            # as DefaultReranker._coerce_result).
            if isinstance(idx, bool) or not isinstance(idx, int):
                raise RerankProviderError(
                    f"Bedrock rerank returned malformed result {r!r}: "
                    f"'index' must be an integer.",
                    category=CATEGORY_INVALID_RESPONSE,
                )
            try:
                relevance = float(r.get("relevanceScore", 0.0))
            except (TypeError, ValueError) as exc:
                raise RerankProviderError(
                    f"Bedrock rerank returned malformed result {r!r}: "
                    f"'relevanceScore' must be numeric.",
                    category=CATEGORY_INVALID_RESPONSE,
                ) from exc
            results.append({"index": idx, "relevance_score": relevance})

        logger.debug(
            f"Bedrock rerank: {len(documents)} docs → {len(results)} results "
            f"in {duration_ms:.0f}ms (model={self._arn_cache})."
        )
        return results


def _build(config: RerankProviderConfig) -> BedrockRerankProvider:
    return BedrockRerankProvider(config)