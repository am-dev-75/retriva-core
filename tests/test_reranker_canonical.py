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
Canonical provider naming tests (requirement 1).

``bedrock`` is the canonical name. ``aws_bedrock``, ``aws-bedrock`` and
case/whitespace variants must normalize to ``bedrock`` in configuration
snapshots, fingerprints, cache identity, provider instances, and status
output.
"""

import pytest
from unittest.mock import patch

from retriva.qa.reranking.base import (
    CANONICAL_PROVIDER_NAMES,
    RerankProviderConfig,
    canonical_provider_name,
)
from retriva.qa.reranking.factory import (
    DEFAULT_PROVIDER_NAME,
    get_reranker_provider,
    provider_names,
    reset_provider_cache,
)
from retriva.qa.reranking.health import reranker_health
from retriva.qa.reranking.metrics import reranker_metrics


@pytest.fixture(autouse=True)
def _clean_state():
    reset_provider_cache()
    reranker_health.reset()
    reranker_metrics.reset()
    yield
    reset_provider_cache()
    reranker_health.reset()
    reranker_metrics.reset()


def _settings(**overrides):
    base = dict(
        enable_retrieval_reranking=True,
        retrieval_rerank_provider="",
        retrieval_rerank_model="amazon.rerank-v1:0",
        retrieval_rerank_base_url="https://openrouter.ai/api/v1",
        retrieval_rerank_api_key="",
        retrieval_rerank_aws_region="eu-central-1",
        retrieval_rerank_timeout=5.0,
        retrieval_rerank_max_retries=2,
        retrieval_rerank_retry_base_delay=1.0,
        retrieval_rerank_enforce_eu_region=False,
        retrieval_rerank_allowed_aws_regions="",
    )
    base.update(overrides)
    from types import SimpleNamespace

    return SimpleNamespace(**base)


class TestCanonicalNaming:
    @pytest.mark.parametrize("alias", [
        "bedrock",
        "Bedrock",
        "  BEDROCK  ",
        "aws_bedrock",
        "AWS_BEDROCK",
        " aws_bedrock ",
        "aws-bedrock",
        "AWS-Bedrock",
        "  aws - bedrock ".replace(" ", ""),  # whitespace-stripped variant
    ])
    def test_all_variants_normalize_to_canonical(self, alias):
        assert canonical_provider_name(alias) == "bedrock"

    def test_canonical_names_constant(self):
        assert set(CANONICAL_PROVIDER_NAMES) == {"openrouter", "bedrock"}

    def test_default_is_canonical_openrouter(self):
        assert DEFAULT_PROVIDER_NAME == "openrouter"
        assert canonical_provider_name("") == ""
        assert canonical_provider_name("  Cohere ") == "openrouter"

    def test_config_snapshot_uses_canonical_name(self):
        cfg = RerankProviderConfig.from_settings(
            _settings(retrieval_rerank_provider="AWS_BEDROCK")
        )
        assert cfg.provider == "bedrock"
        cfg2 = RerankProviderConfig.from_settings(
            _settings(retrieval_rerank_provider="aws-bedrock")
        )
        assert cfg2.provider == "bedrock"

    def test_aliases_share_one_fingerprint(self):
        cfg1 = RerankProviderConfig.from_settings(_settings(retrieval_rerank_provider="bedrock"))
        cfg2 = RerankProviderConfig.from_settings(_settings(retrieval_rerank_provider="aws_bedrock"))
        cfg3 = RerankProviderConfig.from_settings(_settings(retrieval_rerank_provider="AWS-BEDROCK"))
        assert cfg1.fingerprint() == cfg2.fingerprint() == cfg3.fingerprint()

    def test_aliases_share_one_provider_instance(self):
        p1 = get_reranker_provider(_settings(retrieval_rerank_provider="bedrock"))
        p2 = get_reranker_provider(_settings(retrieval_rerank_provider="AWS_BEDROCK"))
        p3 = get_reranker_provider(_settings(retrieval_rerank_provider=" aws-bedrock "))
        assert p1 is p2 is p3
        assert p1.name == "bedrock"

    def test_registered_names_are_canonical(self):
        names = provider_names()
        assert "bedrock" in names and "openrouter" in names
        # 'aws_bedrock' / 'aws-bedrock' are aliases, not separate registrations.
        assert "aws_bedrock" not in names and "aws-bedrock" not in names

    def test_status_reports_canonical_name(self):
        from retriva.qa.reranking import get_reranker_status

        with patch(
            "retriva.config.settings",
            _settings(retrieval_rerank_provider="AWS_BEDROCK"),
        ):
            status = get_reranker_status()
        assert status["provider"] == "bedrock"
        assert status["config"]["provider"] == "bedrock"

    def test_metrics_and_health_use_canonical_name(self):
        """The instance name surfaced to logs/health/metrics is canonical."""
        provider = get_reranker_provider(_settings(retrieval_rerank_provider="AWS_BEDROCK"))
        assert provider.name == "bedrock"

        reranker_metrics.inc_call(provider.name)
        reranker_health.record_success(provider.name)
        snap = reranker_metrics.snapshot()
        assert "bedrock" in snap["per_provider"]
        assert reranker_health.snapshot()["provider"] == "bedrock"