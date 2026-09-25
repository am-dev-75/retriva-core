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
Strict startup validation tests (requirement 4).

Non-strict mode (default): log-and-continue, numeric defaulting surfaces a
sanitized warning and a degraded configuration status.
Strict mode (RETRIEVAL_RERANK_STRICT_STARTUP_VALIDATION=true): startup
fails for invalid static settings, unsupported providers, missing regions,
unsupported SDK operations, invalid endpoints, invalid numeric settings,
and EU-region-policy violations — without making any AWS request.
"""

from types import SimpleNamespace

import pytest

from retriva.qa.reranking.factory import (
    RerankStartupError,
    enforce_rerank_startup,
    reset_provider_cache,
    validate_rerank_config,
)
from retriva.qa.reranking.health import reranker_health
from retriva.qa.reranking.metrics import reranker_metrics


@pytest.fixture(autouse=True)
def _clean_state():
    reset_provider_cache()
    reranker_health.reset()
    yield
    reset_provider_cache()
    reranker_health.reset()


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        enable_retrieval_reranking=True,
        retrieval_rerank_provider="openrouter",
        retrieval_rerank_model="cohere/rerank-v3.5",
        retrieval_rerank_base_url="https://openrouter.ai/api/v1",
        retrieval_rerank_api_key="test-key",
        retrieval_rerank_aws_region="",
        retrieval_rerank_timeout=30.0,
        retrieval_rerank_max_retries=2,
        retrieval_rerank_retry_base_delay=1.0,
        retrieval_rerank_strict_startup_validation=False,
        retrieval_rerank_enforce_eu_region=False,
        retrieval_rerank_allowed_aws_regions="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestNonStrictBackwardCompatibility:
    def test_valid_config_no_issues(self):
        assert enforce_rerank_startup(_settings()) == []

    def test_numeric_defaulting_stays_nonfatal_with_sanitized_warning(self):
        s = _settings(retrieval_rerank_timeout="not-a-number")
        issues = enforce_rerank_startup(s)
        assert issues, "numeric defaulting must be surfaced"
        issue = next(i for i in issues if "RETRIEVAL_RERANK_TIMEOUT" in i["message"])
        assert issue["level"] == "warning"
        assert issue["strict_fatal"] is True  # fatal only under strict mode
        # Sanitized: truncated repr, no exotic characters.
        assert len(issue["message"]) < 300

    def test_numeric_defaulting_sets_degraded_status(self):
        reranker_health.configure(enabled=True, provider="openrouter")
        enforce_rerank_startup(_settings(retrieval_rerank_max_retries="two"))
        assert reranker_health.snapshot()["status"] == "degraded"
        assert "RETRIEVAL_RERANK_MAX_RETRIES" in (
            reranker_health.snapshot()["last_fallback_reason"]
        )

    def test_missing_api_key_is_warning_not_fatal(self):
        issues = enforce_rerank_startup(_settings(retrieval_rerank_api_key=""))
        assert issues and all(i["level"] == "warning" for i in issues)
        assert not any(i.get("strict_fatal") for i in issues)


class TestStrictStartupFailures:
    def test_unknown_provider_fails(self):
        with pytest.raises(RerankStartupError, match="Unknown RETRIEVAL_RERANK_PROVIDER"):
            enforce_rerank_startup(_settings(retrieval_rerank_provider="does-not-exist", retrieval_rerank_strict_startup_validation=True))

    def test_invalid_numeric_settings_fail(self):
        with pytest.raises(RerankStartupError, match="RETRIEVAL_RERANK_TIMEOUT"):
            enforce_rerank_startup(_settings(retrieval_rerank_timeout="abc", retrieval_rerank_strict_startup_validation=True))

    def test_invalid_endpoint_fails(self):
        with pytest.raises(RerankStartupError, match="RETRIEVAL_RERANK_BASE_URL"):
            enforce_rerank_startup(_settings(retrieval_rerank_base_url="not-a-url", retrieval_rerank_strict_startup_validation=True))

    def test_missing_model_fails(self):
        with pytest.raises(RerankStartupError, match="RETRIEVAL_RERANK_MODEL"):
            enforce_rerank_startup(_settings(retrieval_rerank_model="", retrieval_rerank_strict_startup_validation=True))

    @pytest.mark.parametrize("field,value", [
        ("retrieval_rerank_aws_region", ""),   # and env cleared in test
        ("retrieval_rerank_model", ""),
    ])
    def test_bedrock_static_failures(self, field, value, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        overrides = {"retrieval_rerank_provider": "bedrock", "retrieval_rerank_model": "amazon.rerank-v1:0"}
        overrides[field] = value
        overrides["retrieval_rerank_strict_startup_validation"] = True
        with pytest.raises(RerankStartupError):
            enforce_rerank_startup(_settings(**overrides))

    def test_unsupported_sdk_operation_fails_strict(self, monkeypatch):
        """The strict check uses the SDK service model (no network) — a SDK
        without the Rerank operation must fail startup in strict mode."""
        from retriva.qa.reranking.base import RerankProviderError

        def _no_rerank():
            raise RerankProviderError(
                "The installed botocore does not expose the Rerank operation "
                "on 'bedrock-agent-runtime'. Minimum required: botocore/boto3 "
                ">= 1.35.72.",
                category="invalid_response",
            )

        monkeypatch.setattr(
            "retriva.qa.reranking.providers.bedrock.verify_rerank_operation_available",
            _no_rerank,
        )
        s = _settings(
            retrieval_rerank_provider="bedrock",
            retrieval_rerank_model="amazon.rerank-v1:0",
        )
        s.retrieval_rerank_aws_region = "eu-central-1"
        s.retrieval_rerank_strict_startup_validation = True
        with pytest.raises(RerankStartupError, match="Rerank operation"):
            enforce_rerank_startup(s)

    def test_eu_violation_fails_strict(self):
        s = _settings(retrieval_rerank_provider="bedrock", retrieval_rerank_model="amazon.rerank-v1:0")
        s.retrieval_rerank_aws_region = "us-east-1"
        s.retrieval_rerank_enforce_eu_region = True
        s.retrieval_rerank_allowed_aws_regions = "eu-central-1"
        s.retrieval_rerank_strict_startup_validation = True
        with pytest.raises(RerankStartupError, match="EU region policy"):
            enforce_rerank_startup(s)

    def test_strict_valid_config_starts(self, monkeypatch):
        monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
        monkeypatch.delenv("AWS_ENDPOINT_URL_BEDROCK_AGENT_RUNTIME", raising=False)
        s = _settings()
        s.retrieval_rerank_strict_startup_validation = True
        assert enforce_rerank_startup(s) == []

    def test_no_aws_request_during_strict_validation(self, monkeypatch):
        import boto3

        def _no_client(*args, **kwargs):
            raise AssertionError("boto3.client called during startup validation")

        monkeypatch.setattr(boto3, "client", _no_client)
        s = _settings(
            retrieval_rerank_provider="bedrock",
            retrieval_rerank_model="amazon.rerank-v1:0",
            retrieval_rerank_aws_region="eu-central-1",
        )
        s.retrieval_rerank_strict_startup_validation = True
        enforce_rerank_startup(s)

    def test_disabled_reranking_skips_validation(self):
        s = _settings(enable_retrieval_reranking=False, retrieval_rerank_timeout="garbage")
        assert enforce_rerank_startup(s) == []
        s.retrieval_rerank_strict_startup_validation = True
        assert enforce_rerank_startup(s) == []  # no failure while disabled