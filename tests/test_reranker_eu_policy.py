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
EU region enforcement tests (requirement 8).

When RETRIEVAL_RERANK_ENFORCE_EU_REGION=true (bedrock provider):
- non-allowed regions are rejected;
- model-ARN / configured-region mismatches are rejected;
- unsafe endpoint overrides are rejected;
- no rerouting and no fallback to another provider happens (the standard
  vector-order fallback applies).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from retriva.qa.reranker import DefaultReranker
from retriva.qa.reranking.base import RerankProviderConfig, RerankProviderError
from retriva.qa.reranking.factory import (
    RerankStartupError,
    enforce_rerank_startup,
    reset_provider_cache,
    validate_rerank_config,
)
from retriva.qa.reranking.health import reranker_health
from retriva.qa.reranking.metrics import reranker_metrics
from retriva.qa.reranking.providers.bedrock import BedrockRerankProvider


@pytest.fixture(autouse=True)
def _clean_state():
    from retriva.qa.reranking.factory import reset_provider_cache as _reset

    _reset()
    reranker_health.reset()
    reranker_metrics.reset()
    yield
    _reset()
    reranker_health.reset()
    reranker_metrics.reset()


def _config(**overrides) -> RerankProviderConfig:
    base = dict(
        provider="bedrock",
        model="cohere.rerank-v3-5:0",
        aws_region="eu-central-1",
        timeout=5.0,
        max_retries=1,
        retry_base_delay=0.0,
        enforce_eu_region=True,
        allowed_aws_regions=("eu-central-1",),
    )
    base.update(overrides)
    return RerankProviderConfig(**base)


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        enable_retrieval_reranking=True,
        retrieval_rerank_provider="bedrock",
        retrieval_rerank_model="amazon.rerank-v1:0",
        retrieval_rerank_base_url="",
        retrieval_rerank_api_key="",
        retrieval_rerank_aws_region="eu-central-1",
        retrieval_rerank_timeout=5.0,
        retrieval_rerank_max_retries=2,
        retrieval_rerank_retry_base_delay=1.0,
        retrieval_rerank_strict_startup_validation=False,
        retrieval_rerank_enforce_eu_region=True,
        retrieval_rerank_allowed_aws_regions="eu-central-1",
        retrieval_rerank_batch_size=100,
        retrieval_rerank_max_length=4096,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Rank-time enforcement
# ---------------------------------------------------------------------------

class TestRankTimeEnforcement:
    def test_nonallowed_region_rejected(self):
        client = MagicMock()
        provider = BedrockRerankProvider(_config(aws_region="us-east-1"), client=client)
        with pytest.raises(RerankProviderError, match="EU region policy violation"):
            provider.rank("q", ["doc"], 1)
        client.rerank.assert_not_called()  # no AWS request

    def test_arn_region_mismatch_rejected(self):
        client = MagicMock()
        provider = BedrockRerankProvider(
            _config(model="arn:aws:bedrock:us-east-1::foundation-model/amazon.rerank-v1:0"),
            client=client,
        )
        with pytest.raises(RerankProviderError, match="belongs to region"):
            provider.rank("q", ["doc"], 1)
        client.rerank.assert_not_called()

    def test_endpoint_override_rejected(self, monkeypatch):
        monkeypatch.setenv(
            "AWS_ENDPOINT_URL_BEDROCK_AGENT_RUNTIME", "https://evil.example.com"
        )
        client = MagicMock()
        provider = BedrockRerankProvider(_config(), client=client)
        with pytest.raises(RerankProviderError, match="endpoint override"):
            provider.rank("q", ["doc"], 1)
        client.rerank.assert_not_called()

    def test_no_reroute_no_provider_fallback(self, monkeypatch):
        """A policy violation must NOT switch providers: the pipeline applies
        the standard vector-order fallback with the SAME provider selected."""
        client = MagicMock()
        provider = BedrockRerankProvider(_config(aws_region="us-east-1"), client=client)
        monkeypatch.setattr(
            "retriva.qa.reranker.get_reranker_provider", lambda: provider
        )
        monkeypatch.setattr(
            "retriva.qa.reranker.settings", _settings(), raising=False
        )

        chunks = [
            {"text": f"c{i}", "page_title": f"P{i}", "_score": 0.1 * i}
            for i in range(3)
        ]
        result = DefaultReranker().rerank("q", chunks, top_n=2)

        assert [c["page_title"] for c in result] == ["P0", "P1"]
        snap = reranker_health.snapshot()
        assert snap["status"] == "error"
        assert snap["provider"] == "bedrock"  # same provider remains selected
        assert snap["last_error_category"] == "invalid_request"

    def test_enforcement_disabled_by_default(self):
        """Backward-compatible default: no enforcement, no checks."""
        cfg = _config(enforce_eu_region=False, aws_region="us-east-1")
        client = MagicMock()
        client.rerank.return_value = {"results": [{"index": 0, "relevanceScore": 0.9}]}
        provider = BedrockRerankProvider(cfg, client=client)
        assert provider.rank("q", ["doc"], 1) == [{"index": 0, "relevance_score": 0.9}]


# ---------------------------------------------------------------------------
# Static validation
# ---------------------------------------------------------------------------

class TestStaticValidation:
    def test_valid_policy_has_no_issues(self, monkeypatch):
        monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
        monkeypatch.delenv("AWS_ENDPOINT_URL_BEDROCK_AGENT_RUNTIME", raising=False)
        assert validate_rerank_config(_settings()) == []

    def test_empty_allowed_regions_is_error(self, monkeypatch):
        monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
        monkeypatch.delenv("AWS_ENDPOINT_URL_BEDROCK_AGENT_RUNTIME", raising=False)
        issues = validate_rerank_config(
            _settings(retrieval_rerank_allowed_aws_regions="")
        )
        assert any(
            i["level"] == "error" and "ALLOWED_AWS_REGIONS" in i["message"]
            for i in issues
        )

    def test_region_violation_is_error(self):
        issues = validate_rerank_config(_settings(retrieval_rerank_aws_region="us-east-1"))
        assert any("EU region policy violation" in i["message"] for i in issues)

    def test_arn_mismatch_is_error(self):
        issues = validate_rerank_config(
            _settings(
                retrieval_rerank_model="arn:aws:bedrock:us-west-2::foundation-model/amazon.rerank-v1:0",
            )
        )
        assert any("model ARN belongs" in i["message"] for i in issues)

    def test_endpoint_override_is_error(self, monkeypatch):
        monkeypatch.setenv("AWS_ENDPOINT_URL", "https://evil.example.com")
        issues = validate_rerank_config(_settings())
        assert any("endpoint override" in i["message"] for i in issues)

    def test_enforcement_on_non_bedrock_provider_warns(self):
        issues = validate_rerank_config(
            _settings(
                retrieval_rerank_provider="openrouter",
                retrieval_rerank_model="cohere/rerank-v3.5",
                retrieval_rerank_base_url="https://openrouter.ai/api/v1",
                retrieval_rerank_api_key="k",
                retrieval_rerank_enforce_eu_region=True,
            )
        )
        assert any("only to the bedrock provider" in i["message"] for i in issues)

    def test_strict_mode_fails_startup_on_policy_violation(self, monkeypatch):
        monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
        monkeypatch.delenv("AWS_ENDPOINT_URL_BEDROCK_AGENT_RUNTIME", raising=False)
        with pytest.raises(RerankStartupError, match="EU region policy"):
            enforce_rerank_startup(
                _settings(
                    retrieval_rerank_aws_region="us-east-1",
                    retrieval_rerank_strict_startup_validation=True,
                )
            )

    def test_no_aws_request_during_validation(self, monkeypatch):
        """Startup validation must never construct an AWS client."""
        import boto3

        def _no_client(*args, **kwargs):
            raise AssertionError(
                "boto3.client must not be called during startup validation"
            )

        monkeypatch.setattr(boto3, "client", _no_client)
        monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
        monkeypatch.delenv("AWS_ENDPOINT_URL_BEDROCK_AGENT_RUNTIME", raising=False)
        assert validate_rerank_config(_settings()) == []
        enforce_rerank_startup(_settings())  # no raise, no request