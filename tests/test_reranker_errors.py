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
Bedrock error normalization tests (requirement 9).

AWS errors are mapped to safe categories; the 403 ValidationException
containing "Your account is currently being verified" is classified as
``account_verification_pending``. Raw AWS messages never reach the status
API — health stores sanitized, bounded, categorized messages only.
"""

from unittest.mock import MagicMock

import pytest

from retriva.qa.reranking.base import RerankProviderConfig, RerankProviderError
from retriva.qa.reranking.factory import reset_provider_cache
from retriva.qa.reranking.health import reranker_health, sanitize_message
from retriva.qa.reranking.metrics import reranker_metrics
from retriva.qa.reranking.providers.bedrock import (
    ALL_ERROR_CATEGORIES,
    BedrockRerankProvider,
    classify_bedrock_error,
)


@pytest.fixture(autouse=True)
def _clean_state():
    reset_provider_cache()
    reranker_health.reset()
    reranker_metrics.reset()
    yield
    reset_provider_cache()
    reranker_health.reset()
    reranker_metrics.reset()


class _AwsError(Exception):
    """Stand-in for botocore ClientError."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


def _provider(config, exc):
    client = MagicMock()
    client.rerank.side_effect = exc
    return BedrockRerankProvider(config, client=client)


def _cfg(**kw) -> RerankProviderConfig:
    base = dict(provider="bedrock", model="amazon.rerank-v1:0", aws_region="eu-central-1")
    base.update(kw)
    return RerankProviderConfig(**base)


_VERIFICATION_MESSAGE = (
    "An error occurred (ValidationException) when calling the Rerank "
    "operation: Your account is currently being verified. Once the "
    "verification process is complete, you can begin using this service."
)


class TestErrorClassification:
    def test_account_verification_pending(self):
        """403 ValidationException 'Your account is currently being verified'
        must classify as account_verification_pending."""
        category, message = classify_bedrock_error(_AwsError("ValidationException", _VERIFICATION_MESSAGE))
        assert category == "account_verification_pending"
        # The raw AWS message must NOT leak into the safe message.
        assert "currently being verified" not in message
        assert "verification pending" in message.lower()

    def test_declared_categories_all_exist(self):
        expected = {
            "account_verification_pending", "authentication_error",
            "authorization_error", "credentials_expired",
            "model_access_error", "marketplace_entitlement_error",
            "model_not_found", "invalid_request", "throttled", "timeout",
            "service_unavailable", "network_error", "invalid_response",
        }
        assert set(ALL_ERROR_CATEGORIES) == expected

    @pytest.mark.parametrize("code,expected", [
        ("AccessDeniedException", "authorization_error"),
        ("UnauthorizedException", "authentication_error"),
        ("UnrecognizedClientException", "authentication_error"),
        ("InvalidSignatureException", "authentication_error"),
        ("ExpiredTokenException", "credentials_expired"),
        ("ResourceNotFoundException", "model_not_found"),
        ("ModelNotReadyException", "model_access_error"),
        ("ThrottlingException", "throttled"),
        ("TooManyRequestsException", "throttled"),
        ("InternalServerException", "service_unavailable"),
        ("BadGatewayException", "service_unavailable"),
        ("DependencyFailedException", "service_unavailable"),
        ("ServiceQuotaExceededException", "throttled"),
        ("ValidationException", "invalid_request"),
        ("ConflictException", "invalid_request"),
    ])
    def test_code_mapping(self, code, expected):
        exc = _AwsError(
            code,
            f"An error occurred ({code}) when calling the Rerank operation: ...",
        )
        category, _ = classify_bedrock_error(exc)
        assert category == expected

    def test_marketplace_entitlement_via_message(self):
        exc = _AwsError(
            "AccessDeniedException",
            "Access denied: a marketplace offer for this model is required.",
        )
        category, _ = classify_bedrock_error(exc)
        assert category == "marketplace_entitlement_error"

    def test_network_error(self):
        assert classify_bedrock_error(ConnectionError("connection reset"))[0] == "network_error"

    def test_missing_credentials_classified_as_authentication_error(self):
        """NoCredentialsError must map to authentication_error with an
        actionable, secret-free message."""
        import botocore

        exc = botocore.exceptions.NoCredentialsError()
        category, message = classify_bedrock_error(exc)
        assert category == "authentication_error"
        assert "AWS_ACCESS_KEY_ID" in message or "IAM role" in message

    def test_timeout_error(self):
        assert classify_bedrock_error(TimeoutError("read timed out"))[0] == "timeout"

    def test_safe_message_never_contains_raw_text(self):
        secret_ish = "arn:aws:sts::123456789012:assumed-role/secret-role/xyz session=verysecretvalue"
        exc = _AwsError("AccessDeniedException", f"Access denied: {secret_ish}")
        _, message = classify_bedrock_error(exc)
        assert "secret-role" not in message
        assert message.startswith("authorization_error")


class TestProviderWrapsWithCategory:
    def test_client_error_gets_category(self):
        exc = _AwsError("ThrottlingException", "Rate exceeded for the rerank API")
        provider = _provider(_cfg(), exc)
        with pytest.raises(RerankProviderError) as info:
            provider.rank("q", ["doc"], 1)
        assert info.value.category == "throttled"
        assert "Rate exceeded" not in str(info.value)

    def test_malformed_response_category(self):
        client = MagicMock()
        client.rerank.return_value = {"results": [{"index": "bad", "relevanceScore": 1.0}]}
        provider = BedrockRerankProvider(_cfg(), client=client)
        with pytest.raises(RerankProviderError) as info:
            provider.rank("q", ["doc"], 1)
        assert info.value.category == "invalid_response"


class TestHealthBoundedAndCategorized:
    def test_account_verification_flows_to_health_with_category(self):
        """The full path: provider → DefaultReranker → health (category,
        sanitized message; raw AWS text absent)."""
        from retriva.qa.reranker import DefaultReranker

        provider = _provider(_cfg(), _AwsError("ValidationException", _VERIFICATION_MESSAGE))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("retriva.qa.reranker.get_reranker_provider", lambda: provider)
            chunks = [{"text": "doc", "page_title": "P"}]
            DefaultReranker().rerank("q", chunks, top_n=1)
        snap = reranker_health.snapshot()
        assert snap["status"] == "error"
        assert snap["last_error_category"] == "account_verification_pending"
        assert "currently being verified" not in (snap["last_error"] or "")

    def test_record_failure_sanitizes_and_bounds(self):
        long_raw = ("x" * 5000) + "\nstack trace line"
        reranker_health.record_failure("bedrock", long_raw, category="throttled")
        snap = reranker_health.snapshot()
        assert snap["last_error_category"] == "throttled"
        assert len(snap["last_error"]) <= 200
        assert "\n" not in snap["last_error"]
        assert "stack trace line" not in snap["last_error"]

    def test_sanitize_message_basics(self):
        assert sanitize_message("a\nb\tc  d") == "a b c d"
        assert len(sanitize_message("y" * 1000)) == 200

    def test_no_query_or_document_text_in_health(self):
        """Failure messages derive from provider errors, not request payloads."""
        from retriva.qa.reranker import DefaultReranker

        provider = _provider(_cfg(), RuntimeError("boom"))
        chunks = [{"text": "SENSITIVE-DOCUMENT-TEXT", "page_title": "P"}]
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("retriva.qa.reranker.get_reranker_provider", lambda: provider)
            DefaultReranker().rerank("SENSITIVE-QUERY-TEXT", chunks, top_n=1)
        snap = reranker_health.snapshot()
        assert "SENSITIVE" not in str(snap)