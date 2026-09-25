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
Score preservation tests (requirement 5).

Successful reranking preserves the original retrieval score as
``_retrieval_score``, exposes the provider score as ``_rerank_score`` and
keeps the legacy ``_score`` in sync. Fallback and disabled behavior leave
``_score`` untouched and never invent ``_rerank_score``. Internal fields
are stripped from external API payloads (``_score`` is preserved for
backward compatibility).
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from retriva.qa.reranker import DefaultReranker
from retriva.qa.reranking.base import sanitize_chunks_for_api
from retriva.qa.reranking.factory import reset_provider_cache
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


class _StubProvider:
    name = "stub"

    def __init__(self, results):
        self._results = results

    def rank(self, query, documents, top_n):
        return self._results


def _settings(**overrides):
    base = dict(
        enable_retrieval_reranking=True,
        retrieval_rerank_provider="",
        retrieval_rerank_model="m",
        retrieval_rerank_base_url="https://openrouter.ai/api/v1",
        retrieval_rerank_api_key="k",
        retrieval_rerank_aws_region="",
        retrieval_rerank_timeout=5.0,
        retrieval_rerank_max_retries=1,
        retrieval_rerank_retry_base_delay=0.0,
        retrieval_rerank_enforce_eu_region=False,
        retrieval_rerank_allowed_aws_regions="",
        retrieval_rerank_batch_size=100,
        retrieval_rerank_max_length=4096,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _chunks():
    # Chunk 1 originally had the best vector score.
    return [
        {"text": "chunk a", "page_title": "A", "_score": 0.97},
        {"text": "chunk b", "page_title": "B", "_score": 0.91},
        {"text": "chunk c", "page_title": "C", "_score": 0.80},
    ]


class _FailingProvider:
    name = "stub"

    def rank(self, query, documents, top_n):
        raise RuntimeError("boom")


class TestScorePreservation:
    def test_success_preserves_all_three_scores(self, monkeypatch):
        stub = _StubProvider([
            {"index": 1, "relevance_score": 0.99},  # reranker prefers B
            {"index": 0, "relevance_score": 0.42},
        ])
        monkeypatch.setattr("retriva.qa.reranker.get_reranker_provider", lambda: stub)
        monkeypatch.setattr(
            "retriva.qa.reranker.settings", _settings(), raising=False
        )

        chunks = _chunks()
        result = DefaultReranker().rerank("q", chunks, top_n=3)

        b = result[0]
        assert b is chunks[1]
        # _retrieval_score: original vector score, preserved
        assert b["_retrieval_score"] == 0.91
        # _rerank_score: provider score
        assert b["_rerank_score"] == 0.99
        # _score: provider score (legacy compatibility)
        assert b["_score"] == 0.99

        a = result[1]
        assert a["_retrieval_score"] == 0.97
        assert a["_rerank_score"] == 0.42
        assert a["_score"] == 0.42

        # Chunk C was not returned by the provider — its _score untouched.
        assert chunks[2]["_score"] == 0.80
        assert "_rerank_score" not in chunks[2]
        assert "_retrieval_score" not in chunks[2]

    def test_fallback_preserves_original_score_and_no_rerank_score(self, monkeypatch):
        monkeypatch.setattr(
            "retriva.qa.reranker.get_reranker_provider", lambda: _FailingProvider()
        )
        monkeypatch.setattr(
            "retriva.qa.reranker.settings", _settings(), raising=False
        )

        chunks = _chunks()
        result = DefaultReranker().rerank("q", chunks, top_n=2)

        assert result == chunks[:2]
        for c in result:
            assert "_rerank_score" not in c
            assert "_retrieval_score" not in c
        assert chunks[0]["_score"] == 0.97  # original, untouched
        assert chunks[1]["_score"] == 0.91

    def test_disabled_reranking_touches_nothing(self):
        """Disabled reranking returns chunks unchanged (no new fields)."""
        chunks = _chunks()
        from retriva.qa.retriever import _rerank_if_enabled

        with patch("retriva.qa.retriever.settings") as ms:
            ms.enable_retrieval_reranking = False
            result = _rerank_if_enabled("q", chunks, enabled=True)

        assert result == chunks
        assert all("_rerank_score" not in c for c in result)
        assert all("_retrieval_score" not in c for c in result)


class TestExternalExposure:
    def test_sanitizer_strips_internal_fields_keeps_score(self):
        chunks = [
            {"text": "t", "page_title": "A", "_score": 0.5,
             "_rerank_score": 0.9, "_retrieval_score": 0.5},
            {"text": "u", "page_title": "B", "_score": 0.4},
        ]
        clean = sanitize_chunks_for_api(chunks)
        assert clean[0] == {"text": "t", "page_title": "A", "_score": 0.5}
        assert clean[1] == {"text": "u", "page_title": "B", "_score": 0.4}
        # Originals untouched (copies).
        assert chunks[0]["_rerank_score"] == 0.9

    def test_sanitizer_handles_non_dict_entries(self):
        assert sanitize_chunks_for_api(["plain", {"_rerank_score": 1}]) == [
            "plain",
            {},
        ]

    def test_v2_retrieval_router_uses_sanitizer(self):
        """The external v2 retrieval path must route chunks through the
        sanitizer (no _rerank_score/_retrieval_score leak)."""
        import inspect

        import retriva.ingestion_api.routers.v2_retrieval as router_module

        source = inspect.getsource(router_module)
        assert "sanitize_chunks_for_api(" in source
        assert 'RetrievalResponse(chunks=chunks)' not in source

    def test_chat_citations_do_not_include_internal_fields(self):
        """Citations metadata is built from explicit fields only — internal
        underscore keys never reach the chat API."""
        from retriva.openai_api.routers.chat_completions import _build_citations

        chunks = [
            {
                "text": "hello world",
                "page_title": "Doc Title",
                "source_path": "/docs/a.pdf",
                "user_metadata": {"url": "https://example.com/a"},
                "_score": 0.9,
                "_rerank_score": 0.95,
                "_retrieval_score": 0.9,
            }
        ]
        citations = _build_citations(chunks)
        blob = str(citations)
        assert "_rerank_score" not in blob
        assert "_retrieval_score" not in blob