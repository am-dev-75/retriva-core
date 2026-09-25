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
Unit tests for the Amazon Bedrock rerank provider and its integration with
DefaultReranker (global provider selection through settings).
"""

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from retriva.qa.reranker import DefaultReranker
from retriva.qa.reranking.base import RerankProviderConfig, RerankProviderError
from retriva.qa.reranking.factory import get_reranker_provider, reset_provider_cache
from retriva.qa.reranking.health import reranker_health
from retriva.qa.reranking.metrics import reranker_metrics
from retriva.qa.reranking.providers.bedrock import (
    BedrockRerankProvider,
    effective_aws_region,
    model_arn,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_reranking_state():
    reset_provider_cache()
    reranker_health.reset()
    reranker_metrics.reset()
    yield
    reset_provider_cache()
    reranker_health.reset()
    reranker_metrics.reset()


def _config(**overrides) -> RerankProviderConfig:
    base = dict(
        provider="bedrock",
        model="amazon.rerank-v1:0",
        aws_region="eu-central-1",
        timeout=5.0,
        max_retries=2,
        retry_base_delay=1.0,
    )
    base.update(overrides)
    return RerankProviderConfig(**base)


def _fake_client(results):
    """boto3 client stand-in returning the given raw Bedrock results."""
    client = MagicMock()
    client.rerank.return_value = {"results": results}
    return client


# ---------------------------------------------------------------------------
# Tests: ARN and region resolution
# ---------------------------------------------------------------------------

class TestArnResolution:
    def test_bare_model_id_resolves_to_arn(self):
        arn = model_arn("amazon.rerank-v1:0", "eu-central-1")
        assert arn == "arn:aws:bedrock:eu-central-1::foundation-model/amazon.rerank-v1:0"

    def test_full_arn_passthrough(self):
        arn = "arn:aws:bedrock:us-east-1::foundation-model/amazon.rerank-v1:0"
        assert model_arn(arn, "eu-central-1") == arn


class TestRegionResolution:
    def test_explicit_setting_wins(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        assert effective_aws_region(_config(aws_region="eu-central-1")) == "eu-central-1"

    def test_falls_back_to_aws_region_env(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        assert effective_aws_region(_config(aws_region="")) == "us-east-1"

    def test_falls_back_to_aws_default_region_env(self, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
        assert effective_aws_region(_config(aws_region="")) == "us-west-2"

    def test_none_when_unresolvable(self, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        assert effective_aws_region(_config(aws_region="")) is None


# ---------------------------------------------------------------------------
# Tests: rank() request/response contract
# ---------------------------------------------------------------------------

class TestBedrockRank:
    def test_rank_sends_query_documents_and_model(self):
        client = _fake_client(
            [{"index": 1, "relevanceScore": 0.97}, {"index": 0, "relevanceScore": 0.42}]
        )
        provider = BedrockRerankProvider(_config(), client=client)

        results = provider.rank("power consumption", ["doc a", "doc b"], 2)

        kwargs = client.rerank.call_args.kwargs
        assert kwargs["queries"] == [{"type": "TEXT", "textQuery": {"text": "power consumption"}}]
        assert kwargs["sources"][0]["inlineDocumentSource"]["textDocument"]["text"] == "doc a"
        assert kwargs["sources"][1]["inlineDocumentSource"]["textDocument"]["text"] == "doc b"
        assert all(s["type"] == "INLINE" for s in kwargs["sources"])
        bedrock_cfg = kwargs["rerankingConfiguration"]["bedrockRerankingConfiguration"]
        assert bedrock_cfg["modelConfiguration"]["modelArn"] == (
            "arn:aws:bedrock:eu-central-1::foundation-model/amazon.rerank-v1:0"
        )
        assert bedrock_cfg["numberOfResults"] == 2

        # Normalized result shape (Cohere-compatible currency)
        assert results == [
            {"index": 1, "relevance_score": 0.97},
            {"index": 0, "relevance_score": 0.42},
        ]

    def test_rank_clamps_number_of_results(self):
        client = _fake_client([])
        provider = BedrockRerankProvider(_config(), client=client)
        provider.rank("q", ["a", "b"], 99)
        bedrock_cfg = client.rerank.call_args.kwargs["rerankingConfiguration"][
            "bedrockRerankingConfiguration"
        ]
        assert bedrock_cfg["numberOfResults"] == 2

    def test_rank_wraps_client_errors(self):
        client = MagicMock()
        client.rerank.side_effect = RuntimeError("access denied")
        provider = BedrockRerankProvider(_config(), client=client)
        with pytest.raises(RerankProviderError):
            provider.rank("q", ["a"], 1)

    def test_rank_rejects_malformed_results(self):
        client = _fake_client([{"relevanceScore": 0.5}])  # missing "index"
        provider = BedrockRerankProvider(_config(), client=client)
        with pytest.raises(RerankProviderError):
            provider.rank("q", ["a"], 1)

    def test_missing_region_raises(self, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        provider = BedrockRerankProvider(_config(aws_region=""), client=None)
        with pytest.raises(RerankProviderError):
            provider.rank("q", ["a"], 1)

    def test_lazy_boto3_import(self):
        """Without an injected client, building one requires boto3/region."""
        provider = BedrockRerankProvider(_config(), client=None)
        # boto3 is installed in the test venv; the client must build lazily.
        client = provider._get_client()
        assert client is not None
        # Cached across calls.
        assert provider._get_client() is client


# ---------------------------------------------------------------------------
# Tests: DefaultReranker integration with a bedrock-configured pipeline
# ---------------------------------------------------------------------------

class TestDefaultRerankerWithBedrock:
    def _reranker_settings(self, provider_name: str) -> SimpleNamespace:
        return SimpleNamespace(
            enable_retrieval_reranking=True,
            retrieval_rerank_provider=provider_name,
            retrieval_rerank_model="amazon.rerank-v1:0",
            retrieval_rerank_base_url="https://openrouter.ai/api/v1",
            retrieval_rerank_api_key="",
            retrieval_rerank_aws_region="eu-central-1",
            retrieval_rerank_timeout=5.0,
            retrieval_rerank_max_retries=2,
            retrieval_rerank_retry_base_delay=1.0,
            retrieval_rerank_batch_size=100,
            retrieval_rerank_max_length=4096,
        )

    def test_global_selection_through_settings(self):
        """Selecting 'bedrock' in the GLOBAL settings routes DefaultReranker there."""
        chunks = [
            {"text": f"chunk {i}", "page_title": f"P{i}", "source_path": f"/p{i}", "_score": 0.1 * i}
            for i in range(3)
        ]
        fake_client = _fake_client(
            [{"index": 2, "relevanceScore": 0.99}, {"index": 0, "relevanceScore": 0.5}]
        )
        provider = BedrockRerankProvider(_config(), client=fake_client)

        with patch("retriva.qa.reranker.settings", self._reranker_settings("bedrock")), patch(
            "retriva.qa.reranking.factory.settings", self._reranker_settings("bedrock")
        ), patch(
            "retriva.qa.reranking.providers.bedrock.BedrockRerankProvider._get_client",
            return_value=fake_client,
        ):
            built = get_reranker_provider()
            assert built.name == "bedrock"

            reranker = DefaultReranker()
            result = reranker.rerank("power", chunks, top_n=2)

        # Chunk metadata preserved, order comes from Bedrock, _score synced.
        assert [c["page_title"] for c in result] == ["P2", "P0"]
        assert result[0]["source_path"] == "/p2"
        assert result[0]["_score"] == 0.99
        # Original dicts mutated in place (same objects, like the legacy path).
        assert result[0] is chunks[2]

        # Observability recorded the provider.
        snap = reranker_metrics.snapshot()
        assert snap["counters"]["calls_total"] == 1
        assert snap["counters"]["success_total"] == 1
        assert "bedrock" in snap["per_provider"]
        assert reranker_health.snapshot()["status"] == "ok"

    def test_fallback_to_vector_order_on_provider_error(self, monkeypatch):
        chunks = [
            {"text": f"chunk {i}", "page_title": f"P{i}", "_score": 0.1 * i}
            for i in range(3)
        ]
        failing = BedrockRerankProvider(_config(), client=None)
        failing._get_client = MagicMock(side_effect=RerankProviderError("throttled"))

        with patch("retriva.qa.reranker.settings", self._reranker_settings("bedrock")), patch(
            "retriva.qa.reranking.factory.settings", self._reranker_settings("bedrock")
        ), patch(
            "retriva.qa.reranking.factory.build_rerank_provider", return_value=failing
        ):
            reset_provider_cache()
            result = DefaultReranker().rerank("power", chunks, top_n=2)

        # Error policy: original order, truncated to top_n, no exception.
        assert [c["page_title"] for c in result] == ["P0", "P1"]
        assert reranker_health.snapshot()["status"] == "error"
        assert reranker_metrics.snapshot()["counters"]["fallback_total"] == 1
