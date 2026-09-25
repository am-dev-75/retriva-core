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
Hardening tests for the reranking subsystem.

Focus: malformed provider output must degrade to the documented fallback
policy (vector order, truncated to top_n) instead of raising out of
rerank() or poisoning downstream _score sorting; config values must
fail-safe; observability must attribute outcomes to the right provider.
"""

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from retriva.qa.reranker import (
    DefaultReranker,
    _call_rerank_api,
    _coerce_result,
)
from retriva.qa.reranking.base import RerankProviderConfig, RerankProviderError
from retriva.qa.reranking.factory import (
    get_reranker_provider,
    reset_provider_cache,
    resolve_provider_name,
)
from retriva.qa.reranking.health import reranker_health
from retriva.qa.reranking.metrics import reranker_metrics
from retriva.qa.reranking.providers.bedrock import BedrockRerankProvider


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


@pytest.fixture
def sample_chunks():
    return [
        {"text": f"chunk {i}", "page_title": f"P{i}", "source_path": f"/p{i}"}
        for i in range(5)
    ]


def _reranker_settings():
    return SimpleNamespace(
        enable_retrieval_reranking=True,
        retrieval_rerank_provider="",
        retrieval_rerank_model="test-model",
        retrieval_rerank_base_url="https://openrouter.test/api/v1",
        retrieval_rerank_api_key="test-key",
        retrieval_rerank_aws_region="",
        retrieval_rerank_timeout=5.0,
        retrieval_rerank_max_retries=1,
        retrieval_rerank_retry_base_delay=0.0,
        retrieval_rerank_batch_size=100,
        retrieval_rerank_max_length=4096,
    )


class _StubProvider:
    """Provider stand-in returning canned (possibly malformed) results."""

    name = "stub"

    def __init__(self, results=None, error=None):
        self._results = results if results is not None else []
        self._error = error
        self.calls = []

    def rank(self, query, documents, top_n):
        self.calls.append((query, list(documents), top_n))
        if self._error is not None:
            raise self._error
        return self._results


def _install_stub(monkeypatch, stub):
    monkeypatch.setattr(
        "retriva.qa.reranker.get_reranker_provider", lambda: stub
    )
    monkeypatch.setattr(
        "retriva.qa.reranker.settings", _reranker_settings(), raising=False
    )


# ---------------------------------------------------------------------------
# Tests: provider result coercion
# ---------------------------------------------------------------------------

class TestResultCoercion:
    def test_valid_result(self):
        assert _coerce_result({"index": 2, "relevance_score": 0.9}) == (2, 0.9)

    def test_numeric_string_score_is_coerced(self):
        assert _coerce_result({"index": 1, "relevance_score": "0.5"}) == (1, 0.5)

    def test_missing_relevance_score_defaults_to_zero(self):
        assert _coerce_result({"index": 0}) == (0, 0.0)

    @pytest.mark.parametrize("bad", [
        {"relevance_score": 0.9},                    # missing index
        {"index": "1", "relevance_score": 0.9},      # string index
        {"index": True, "relevance_score": 0.9},     # bool index
        {"index": 1.5, "relevance_score": 0.9},      # float index
        {"index": 1, "relevance_score": "high"},     # unconvertible score
        {"index": None, "relevance_score": 0.9},     # null index
        "not a dict",
        42,
        None,
    ])
    def test_malformed_entries_are_rejected(self, bad):
        assert _coerce_result(bad) == (None, 0.0)


# ---------------------------------------------------------------------------
# Tests: DefaultReranker malformed-output hardening
# ---------------------------------------------------------------------------

class TestMalformedProviderOutput:
    def test_malformed_entries_skipped(self, sample_chunks, monkeypatch):
        stub = _StubProvider(results=[
            {"index": 1, "relevance_score": 0.9},
            "garbage",
            {"relevance_score": 0.8},              # no index
            {"index": "2", "relevance_score": 0.7},  # bad index
            {"index": 3, "relevance_score": 0.6},
        ])
        _install_stub(monkeypatch, stub)

        result = DefaultReranker().rerank("q", sample_chunks, top_n=5)

        assert [c["page_title"] for c in result] == ["P1", "P3"]

    def test_duplicate_indices_deduplicated(self, sample_chunks, monkeypatch):
        stub = _StubProvider(results=[
            {"index": 1, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.8},
            {"index": 2, "relevance_score": 0.7},
        ])
        _install_stub(monkeypatch, stub)

        result = DefaultReranker().rerank("q", sample_chunks, top_n=3)

        assert [c["page_title"] for c in result] == ["P1", "P2"]

    def test_results_clamped_to_top_n(self, sample_chunks, monkeypatch):
        """A provider returning more than top_n results must be clamped."""
        stub = _StubProvider(results=[
            {"index": i, "relevance_score": 1.0 - i * 0.1} for i in range(5)
        ])
        _install_stub(monkeypatch, stub)

        result = DefaultReranker().rerank("q", sample_chunks, top_n=2)

        assert len(result) == 2
        assert [c["page_title"] for c in result] == ["P0", "P1"]

    def test_all_invalid_results_fall_back(self, sample_chunks, monkeypatch):
        """Provider 'succeeds' but returns garbage → vector-order fallback."""
        stub = _StubProvider(results=[
            {"index": "bad", "relevance_score": 0.9},
            {"relevance_score": 0.8},
        ])
        _install_stub(monkeypatch, stub)

        result = DefaultReranker().rerank("q", sample_chunks, top_n=3)

        assert [c["page_title"] for c in result] == ["P0", "P1", "P2"]
        assert reranker_health.snapshot()["status"] == "degraded"
        assert reranker_metrics.snapshot()["counters"]["fallback_total"] == 1
        # No success recorded for the unusable response.
        assert reranker_metrics.snapshot()["counters"]["success_total"] == 0

    def test_non_numeric_first_score_does_not_crash_logging(
        self, sample_chunks, monkeypatch
    ):
        """Regression: the old log line raised ValueError on 'N/A' format."""
        stub = _StubProvider(results=[{"index": 0}])  # no relevance_score key
        _install_stub(monkeypatch, stub)

        result = DefaultReranker().rerank("q", sample_chunks, top_n=1)

        assert len(result) == 1
        assert result[0]["_score"] == 0.0

    def test_non_positive_top_n_short_circuits(self, sample_chunks, monkeypatch):
        stub = _StubProvider()
        _install_stub(monkeypatch, stub)

        assert DefaultReranker().rerank("q", sample_chunks, top_n=0) == []
        assert DefaultReranker().rerank("q", sample_chunks, top_n=-3) == []
        assert stub.calls == []  # provider never called

    def test_non_list_provider_output_falls_back(self, sample_chunks, monkeypatch):
        """A provider returning a non-list must trigger the fallback policy,
        not raise out of rerank()."""
        stub = _StubProvider(results={"index": 0, "relevance_score": 0.9})  # dict!
        _install_stub(monkeypatch, stub)

        result = DefaultReranker().rerank("q", sample_chunks, top_n=2)

        assert [c["page_title"] for c in result] == ["P0", "P1"]
        assert reranker_health.snapshot()["status"] == "error"
        assert reranker_metrics.snapshot()["counters"]["fallback_total"] == 1

    def test_malformed_first_result_does_not_crash_logging(
        self, sample_chunks, monkeypatch
    ):
        """Regression: top-score logging read results[0] raw — a malformed
        first entry (valid later ones) crashed the log line."""
        stub = _StubProvider(results=[
            "garbage",
            {"index": 2, "relevance_score": 0.9},
        ])
        _install_stub(monkeypatch, stub)

        result = DefaultReranker().rerank("q", sample_chunks, top_n=2)

        assert [c["page_title"] for c in result] == ["P2"]
        assert result[0]["_score"] == 0.9


# ---------------------------------------------------------------------------
# Tests: _call_rerank_api hardening
# ---------------------------------------------------------------------------

def _fake_http_session(monkeypatch):
    """Install a fake httpx.Client inside retriva.qa.reranker.

    Returns the *inner* session mock — the object `client.post(...)` is
    called on inside the `with` block.
    """
    outer = MagicMock()
    inner = outer.__enter__.return_value
    monkeypatch.setattr(
        "retriva.qa.reranker.httpx.Client", lambda timeout: outer
    )
    return inner


class TestCallRerankApiHardening:
    def test_max_retries_below_one_still_attempts_once(self, monkeypatch):
        s = _reranker_settings()
        s.retrieval_rerank_max_retries = 0
        monkeypatch.setattr("retriva.qa.reranker.settings", s, raising=False)

        inner = _fake_http_session(monkeypatch)
        inner.post.return_value.json.return_value = {"results": []}

        out = _call_rerank_api("q", ["doc"], 1)

        assert out == []
        assert inner.post.call_count == 1  # one attempt, not zero

    def test_non_object_payload_raises_runtime_error(self, monkeypatch):
        s = _reranker_settings()
        monkeypatch.setattr("retriva.qa.reranker.settings", s, raising=False)

        inner = _fake_http_session(monkeypatch)
        inner.post.return_value.json.return_value = ["unexpected", "array"]

        with pytest.raises(RuntimeError, match="unexpected payload type"):
            _call_rerank_api("q", ["doc"], 1)

    def test_non_json_payload_raises_runtime_error(self, monkeypatch):
        s = _reranker_settings()
        monkeypatch.setattr("retriva.qa.reranker.settings", s, raising=False)

        inner = _fake_http_session(monkeypatch)
        inner.post.return_value.json.side_effect = ValueError("bad json")

        with pytest.raises(RuntimeError, match="non-JSON"):
            _call_rerank_api("q", ["doc"], 1)

    def test_authorization_header_only_with_key(self, monkeypatch):
        """Header present with a key, absent without one; the key value is
        never logged."""
        s = _reranker_settings()
        monkeypatch.setattr("retriva.qa.reranker.settings", s, raising=False)
        inner = _fake_http_session(monkeypatch)
        inner.post.return_value.json.return_value = {"results": []}
        _call_rerank_api("q", ["doc"], 1)
        headers = inner.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer test-key"

        s.retrieval_rerank_api_key = None
        inner = _fake_http_session(monkeypatch)
        inner.post.return_value.json.return_value = {"results": []}
        _call_rerank_api("q", ["doc"], 1)
        headers = inner.post.call_args.kwargs["headers"]
        assert "Authorization" not in headers


# ---------------------------------------------------------------------------
# Tests: config fail-safety and name normalization
# ---------------------------------------------------------------------------

class TestConfigFailSafety:
    def test_non_numeric_timeout_falls_back_to_default(self):
        s = SimpleNamespace(
            retrieval_rerank_provider="",
            retrieval_rerank_model="m",
            retrieval_rerank_base_url="https://x",
            retrieval_rerank_api_key="k",
            retrieval_rerank_aws_region="",
            retrieval_rerank_timeout="not-a-number",
            retrieval_rerank_max_retries="two",
            retrieval_rerank_retry_base_delay=[],
        )
        cfg = RerankProviderConfig.from_settings(s)
        assert cfg.timeout == 30.0
        assert cfg.max_retries == 2
        assert cfg.retry_base_delay == 1.0
        # Snapshot must remain buildable (no exception raised above).

    def test_string_numeric_values_are_coerced(self):
        s = SimpleNamespace(
            retrieval_rerank_provider="",
            retrieval_rerank_model="m",
            retrieval_rerank_base_url="https://x",
            retrieval_rerank_api_key="k",
            retrieval_rerank_aws_region="",
            retrieval_rerank_timeout="15",
            retrieval_rerank_max_retries="3",
            retrieval_rerank_retry_base_delay="0.5",
        )
        cfg = RerankProviderConfig.from_settings(s)
        assert cfg.timeout == 15.0
        assert cfg.max_retries == 3
        assert cfg.retry_base_delay == 0.5

    def test_directly_built_config_normalizes_provider_name(self):
        cfg = RerankProviderConfig(provider="  Bedrock  ", model="m")
        assert resolve_provider_name(cfg) == "bedrock"
        cfg_alias = RerankProviderConfig(provider="Cohere", model="m")
        assert resolve_provider_name(cfg_alias) == "openrouter"


# ---------------------------------------------------------------------------
# Tests: per-provider metric attribution
# ---------------------------------------------------------------------------

class TestPerProviderAttribution:
    def test_failure_attributed_to_active_provider(self, sample_chunks, monkeypatch):
        stub = _StubProvider(error=RuntimeError("boom"))
        _install_stub(monkeypatch, stub)

        DefaultReranker().rerank("q", sample_chunks, top_n=2)

        snap = reranker_metrics.snapshot()
        assert snap["counters"]["failure_total"] == 1
        assert snap["per_provider"]["stub"]["failure_total"] == 1
        assert snap["per_provider"]["stub"]["calls_total"] == 1

    def test_failed_provider_build_not_misattributed(self, sample_chunks, monkeypatch):
        """When the provider cannot even be built, no name is known — the
        failure must bump global counters only, never a previous provider."""
        def _boom():
            raise RerankProviderError("unknown provider")

        monkeypatch.setattr("retriva.qa.reranker.get_reranker_provider", _boom)
        monkeypatch.setattr(
            "retriva.qa.reranker.settings", _reranker_settings(), raising=False
        )

        # Seed a prior provider so misattribution would be visible.
        reranker_metrics.inc_call("openrouter")
        DefaultReranker().rerank("q", sample_chunks, top_n=2)

        snap = reranker_metrics.snapshot()
        assert snap["counters"]["failure_total"] == 1
        assert snap["per_provider"]["openrouter"]["failure_total"] == 0

    def test_success_attributed_to_active_provider(self, sample_chunks, monkeypatch):
        stub = _StubProvider(results=[{"index": 0, "relevance_score": 0.9}])
        _install_stub(monkeypatch, stub)

        DefaultReranker().rerank("q", sample_chunks, top_n=1)

        snap = reranker_metrics.snapshot()
        assert snap["per_provider"]["stub"]["success_total"] == 1


# ---------------------------------------------------------------------------
# Tests: Bedrock provider guards
# ---------------------------------------------------------------------------

class TestBedrockGuards:
    def test_empty_documents_short_circuits(self):
        client = MagicMock()
        provider = BedrockRerankProvider(
            RerankProviderConfig(provider="bedrock", model="amazon.rerank-v1:0",
                                 aws_region="eu-central-1"),
            client=client,
        )
        assert provider.rank("q", [], 5) == []
        client.rerank.assert_not_called()

    def test_bool_index_result_rejected(self):
        client = MagicMock()
        client.rerank.return_value = {
            "results": [{"index": True, "relevanceScore": 0.9}]
        }
        provider = BedrockRerankProvider(
            RerankProviderConfig(provider="bedrock", model="amazon.rerank-v1:0",
                                 aws_region="eu-central-1"),
            client=client,
        )
        with pytest.raises(RerankProviderError, match="must be an integer"):
            provider.rank("q", ["doc"], 1)
