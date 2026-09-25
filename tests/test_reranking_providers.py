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
Unit tests for the reranking provider subsystem (factory, health, metrics,
settings-driven selection, runtime reload, secret redaction).
"""

import pytest
from types import SimpleNamespace
from unittest.mock import patch

from retriva.qa.reranking.base import (
    RerankProvider,
    RerankProviderConfig,
    RerankProviderError,
)
from retriva.qa.reranking.factory import (
    DEFAULT_PROVIDER_NAME,
    build_rerank_provider,
    get_reranker_provider,
    provider_names,
    register_rerank_provider,
    reset_provider_cache,
    resolve_provider_name,
    validate_rerank_config,
)
from retriva.qa.reranking.health import reranker_health
from retriva.qa.reranking.metrics import reranker_metrics


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_reranking_state():
    """Isolate provider cache, health and metrics between tests."""
    reset_provider_cache()
    reranker_health.reset()
    reranker_metrics.reset()
    yield
    reset_provider_cache()
    reranker_health.reset()
    reranker_metrics.reset()


def _settings(**overrides) -> SimpleNamespace:
    """Settings stand-in exposing only what the factory reads."""
    base = dict(
        enable_retrieval_reranking=True,
        retrieval_rerank_provider="",
        retrieval_rerank_model="cohere/rerank-v3.5",
        retrieval_rerank_base_url="https://openrouter.ai/api/v1",
        retrieval_rerank_api_key="test-key",
        retrieval_rerank_aws_region="",
        retrieval_rerank_timeout=30.0,
        retrieval_rerank_max_retries=2,
        retrieval_rerank_retry_base_delay=1.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Tests: provider resolution and precedence
# ---------------------------------------------------------------------------

class TestProviderResolution:
    def test_default_provider_is_openrouter(self):
        """Empty selection must resolve to the legacy openrouter transport."""
        cfg = RerankProviderConfig.from_settings(_settings(retrieval_rerank_provider=""))
        assert resolve_provider_name(cfg) == DEFAULT_PROVIDER_NAME == "openrouter"

    def test_selection_is_case_insensitive(self):
        cfg = RerankProviderConfig.from_settings(_settings(retrieval_rerank_provider="  Bedrock "))
        assert resolve_provider_name(cfg) == "bedrock"

    def test_cohere_alias_maps_to_openrouter_transport(self):
        cfg = RerankProviderConfig.from_settings(_settings(retrieval_rerank_provider="cohere"))
        assert resolve_provider_name(cfg) == "openrouter"

    def test_builtin_providers_registered(self):
        names = provider_names()
        assert "openrouter" in names
        assert "bedrock" in names

    def test_build_unknown_provider_fails_fast(self):
        cfg = RerankProviderConfig.from_settings(_settings(retrieval_rerank_provider="does-not-exist"))
        with pytest.raises(RerankProviderError) as exc:
            build_rerank_provider(cfg)
        assert "does-not-exist" in str(exc.value)
        assert "openrouter" in str(exc.value)

    def test_custom_provider_registration_and_selection(self):
        class StubProvider(RerankProvider):
            name = "teststub"

            def rank(self, query, documents, top_n):
                return [{"index": 0, "relevance_score": 1.0}]

        register_rerank_provider("teststub", lambda cfg: StubProvider())
        provider = get_reranker_provider(
            _settings(retrieval_rerank_provider="teststub")
        )
        assert isinstance(provider, StubProvider)
        assert provider.rank("q", ["d"], 1) == [{"index": 0, "relevance_score": 1.0}]

    def test_default_provider_instance_is_openrouter(self):
        assert get_reranker_provider(_settings()).name == "openrouter"


# ---------------------------------------------------------------------------
# Tests: runtime reload via settings fingerprint
# ---------------------------------------------------------------------------

class TestRuntimeReload:
    def test_same_settings_return_cached_instance(self):
        s = _settings()
        p1 = get_reranker_provider(s)
        p2 = get_reranker_provider(s)
        assert p1 is p2

    def test_model_change_rebuilds_provider(self):
        p1 = get_reranker_provider(_settings(retrieval_rerank_model="model-a"))
        p2 = get_reranker_provider(_settings(retrieval_rerank_model="model-b"))
        assert p1 is not p2

    def test_provider_change_rebuilds_provider(self):
        p1 = get_reranker_provider(_settings(retrieval_rerank_provider=""))
        p2 = get_reranker_provider(_settings(retrieval_rerank_provider="bedrock"))
        assert p1.name == "openrouter"
        assert p2.name == "bedrock"

    def test_credential_change_does_not_rebuild_provider(self):
        """Fingerprint policy: credentials are excluded from the fingerprint.

        The OpenRouter transport reads the key at request time and the
        Bedrock transport delegates to botocore's credential chain, so key
        rotation must not force provider reconstruction.
        """
        p1 = get_reranker_provider(_settings(retrieval_rerank_api_key="key-1"))
        p2 = get_reranker_provider(_settings(retrieval_rerank_api_key="key-2"))
        assert p1 is p2  # reused, not rebuilt

        cfg1 = RerankProviderConfig.from_settings(_settings(retrieval_rerank_api_key="key-1"))
        cfg2 = RerankProviderConfig.from_settings(_settings(retrieval_rerank_api_key="key-2"))
        assert cfg1.fingerprint() == cfg2.fingerprint()

    def test_aws_region_change_rebuilds_provider(self):
        p1 = get_reranker_provider(_settings(retrieval_rerank_provider="bedrock", retrieval_rerank_aws_region="eu-central-1"))
        p2 = get_reranker_provider(_settings(retrieval_rerank_provider="bedrock", retrieval_rerank_aws_region="us-east-1"))
        assert p1 is not p2

    def test_eu_policy_settings_are_in_fingerprint(self):
        cfg1 = RerankProviderConfig.from_settings(_settings())
        cfg2 = RerankProviderConfig.from_settings(
            _settings(retrieval_rerank_enforce_eu_region=True)
        )
        cfg3 = RerankProviderConfig.from_settings(
            _settings(retrieval_rerank_allowed_aws_regions="eu-central-1")
        )
        assert cfg1.fingerprint() != cfg2.fingerprint()
        assert cfg1.fingerprint() != cfg3.fingerprint()


# ---------------------------------------------------------------------------
# Tests: startup validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_disabled_reranking_has_no_issues(self):
        assert validate_rerank_config(_settings(enable_retrieval_reranking=False)) == []

    def test_valid_openrouter_config_has_no_issues(self):
        assert validate_rerank_config(_settings()) == []

    def test_openrouter_without_api_key_warns(self):
        issues = validate_rerank_config(_settings(retrieval_rerank_api_key=""))
        assert len(issues) == 1
        assert issues[0]["level"] == "warning"
        assert "RETRIEVAL_RERANK_API_KEY" in issues[0]["message"]

    def test_unknown_provider_is_error(self):
        issues = validate_rerank_config(_settings(retrieval_rerank_provider="nope"))
        assert issues and issues[0]["level"] == "error"

    def test_bedrock_requires_region(self, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        issues = validate_rerank_config(_settings(retrieval_rerank_provider="bedrock"))
        levels = {i["level"] for i in issues}
        assert "error" in levels
        assert any("region" in i["message"].lower() for i in issues)

    def test_bedrock_requires_model(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "eu-central-1")
        issues = validate_rerank_config(_settings(retrieval_rerank_provider="bedrock", retrieval_rerank_model=""))
        assert any("RETRIEVAL_RERANK_MODEL" in i["message"] for i in issues)

    def test_valid_bedrock_config_has_no_issues(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "eu-central-1")
        issues = validate_rerank_config(
            _settings(
                retrieval_rerank_provider="bedrock",
                retrieval_rerank_model="amazon.rerank-v1:0",
                retrieval_rerank_api_key="",
            )
        )
        assert issues == []

    def test_bedrock_with_api_key_warns_about_ignored_secret(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "eu-central-1")
        issues = validate_rerank_config(_settings(retrieval_rerank_provider="bedrock"))
        assert any("ignored" in i["message"] for i in issues if i["level"] == "warning")


# ---------------------------------------------------------------------------
# Tests: health and metrics
# ---------------------------------------------------------------------------

class TestHealth:
    def test_success_updates_health(self):
        reranker_health.configure(enabled=True, provider="openrouter")
        reranker_health.record_success("openrouter")
        snap = reranker_health.snapshot()
        assert snap["status"] == "ok"
        assert snap["provider"] == "openrouter"
        assert snap["total_successes"] == 1
        assert snap["consecutive_failures"] == 0
        assert snap["last_success_at"] is not None

    def test_failure_tracks_consecutive_errors(self):
        reranker_health.record_failure("bedrock", "boom")
        reranker_health.record_failure("bedrock", "boom again")
        snap = reranker_health.snapshot()
        assert snap["status"] == "error"
        assert snap["consecutive_failures"] == 2
        assert snap["total_failures"] == 2
        assert snap["last_error"] == "boom again"

    def test_success_resets_consecutive_failures(self):
        reranker_health.record_failure("openrouter", "boom")
        reranker_health.record_success("openrouter")
        assert reranker_health.snapshot()["consecutive_failures"] == 0
        assert reranker_health.snapshot()["status"] == "ok"

    def test_fallback_marks_degraded_not_error(self):
        reranker_health.record_fallback("empty results")
        snap = reranker_health.snapshot()
        assert snap["status"] == "degraded"
        assert snap["last_fallback_reason"] == "empty results"

    def test_disabled_status(self):
        reranker_health.configure(enabled=False, provider=None)
        assert reranker_health.snapshot()["status"] == "disabled"


class TestMetrics:
    def test_counters_and_latency(self):
        reranker_metrics.inc_call("openrouter")
        reranker_metrics.add_documents(5)
        reranker_metrics.observe_latency(12.5)
        reranker_metrics.observe_latency(27.5)
        reranker_metrics.inc_success()
        snap = reranker_metrics.snapshot()
        assert snap["counters"]["calls_total"] == 1
        assert snap["counters"]["success_total"] == 1
        assert snap["counters"]["documents_scored_total"] == 5
        assert snap["latency_ms"]["last"] == 27.5
        assert snap["latency_ms"]["avg"] == 20.0
        assert snap["latency_ms"]["max"] == 27.5
        assert snap["per_provider"]["openrouter"]["calls_total"] == 1

    def test_failure_counters(self):
        reranker_metrics.inc_failure()
        reranker_metrics.inc_fallback()
        snap = reranker_metrics.snapshot()
        assert snap["counters"]["failure_total"] == 1
        assert snap["counters"]["fallback_total"] == 1


# ---------------------------------------------------------------------------
# Tests: secret redaction
# ---------------------------------------------------------------------------

class TestSecretRedaction:
    def test_public_dict_never_contains_api_key_value(self):
        cfg = RerankProviderConfig.from_settings(_settings(retrieval_rerank_api_key="sk-super-secret"))
        public = cfg.public_dict()
        assert "sk-super-secret" not in str(public)
        assert public["api_key_set"] is True

    def test_status_payload_is_redacted(self):
        from retriva.qa.reranking import get_reranker_status

        with patch("retriva.config.settings", _settings(retrieval_rerank_api_key="sk-super-secret")):
            status = get_reranker_status()
        blob = str(status)
        assert "sk-super-secret" not in blob
        assert status["config"]["api_key_set"] is True
        assert status["enabled"] is True
        assert status["provider"] == "openrouter"


# ---------------------------------------------------------------------------
# Tests: config snapshot
# ---------------------------------------------------------------------------

class TestProviderConfig:
    def test_from_settings_reads_all_fields(self):
        cfg = RerankProviderConfig.from_settings(_settings())
        assert cfg.provider == ""
        assert cfg.model == "cohere/rerank-v3.5"
        assert cfg.api_key == "test-key"
        assert cfg.timeout == 30.0
        assert cfg.max_retries == 2

    def test_fingerprint_is_stable_and_sensitive(self):
        cfg1 = RerankProviderConfig.from_settings(_settings())
        cfg2 = RerankProviderConfig.from_settings(_settings())
        cfg3 = RerankProviderConfig.from_settings(_settings(retrieval_rerank_model="other"))
        assert cfg1.fingerprint() == cfg2.fingerprint()
        assert cfg1.fingerprint() != cfg3.fingerprint()
