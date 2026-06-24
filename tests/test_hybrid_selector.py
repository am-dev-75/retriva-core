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

"""Unit tests for the DefaultHybridSelector capability."""

import pytest
from unittest.mock import patch
from retriva.qa.hybrid_selector import DefaultHybridSelector


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def reranked_chunks():
    """6 chunks that survived re-ranking (precision set)."""
    return [
        {"text": "AURA SOM power spec: max 2.8W under stress.", "page_title": "AURA Power", "source_path": "/aura/power"},
        {"text": "AURA SOM idle power: 1.5W typical.", "page_title": "AURA Idle", "source_path": "/aura/idle"},
        {"text": "AURA SOM low-power suspend: 0.16W.", "page_title": "AURA Suspend", "source_path": "/aura/suspend"},
        {"text": "AURA SOM processor i.MX93 details.", "page_title": "AURA CPU", "source_path": "/aura/cpu"},
        {"text": "AURA SOM BBSM mode: 1.10mW.", "page_title": "AURA BBSM", "source_path": "/aura/bbsm"},
        {"text": "AURA SOM operational characteristics.", "page_title": "AURA Ops", "source_path": "/aura/ops"},
    ]


@pytest.fixture
def vector_top_chunks():
    """10 chunks from vector search in cosine-similarity order."""
    return [
        {"text": "AURA SOM power spec: max 2.8W under stress.", "page_title": "AURA Power", "source_path": "/aura/power"},  # dup with reranked[0]
        {"text": "AURA SOM thermal management and heat dissipation.", "page_title": "AURA Thermal", "source_path": "/aura/thermal"},
        {"text": "AURA SOM idle power: 1.5W typical.", "page_title": "AURA Idle", "source_path": "/aura/idle"},  # dup with reranked[1]
        {"text": "SBCX thermal test: SOM power consumption max 3.8W.", "page_title": "SBCX Thermal", "source_path": "/sbcx/thermal"},
        {"text": "AURA SOM processor i.MX93 power modes.", "page_title": "AURA Modes", "source_path": "/aura/modes"},
        {"text": "AURA SOM PDF measurements table.", "page_title": "AURA PDF", "source_path": "/aura/pdf"},
        {"text": "BORA SOM power consumption comparison.", "page_title": "BORA Power", "source_path": "/bora/power"},
        {"text": "Comparison table SOM A vs SOM B.", "page_title": "Comparison", "source_path": "/compare"},
        {"text": "AURA SOM pinout table.", "page_title": "AURA Pinout", "source_path": "/aura/pinout"},
        {"text": "DESK-MX9-L carrier board specs.", "page_title": "DESK-MX9", "source_path": "/desk"},
    ]


# ---------------------------------------------------------------------------
# Tests: two-knob merge
# ---------------------------------------------------------------------------

class TestHybridMerge:
    def test_merge_adds_missing_evidence(self, reranked_chunks, vector_top_chunks):
        """Vector-search chunks not in reranked set should be appended."""
        selector = DefaultHybridSelector()
        result = selector.select(reranked_chunks, vector_top_chunks, keep_m=6, keep_l=10)

        # The SBCX thermal test (implicit evidence) should be present
        titles = [c["page_title"] for c in result]
        assert "SBCX Thermal" in titles
        assert "AURA Thermal" in titles

    def test_reranked_order_preserved(self, reranked_chunks, vector_top_chunks):
        """Re-ranked chunks must appear first, in their original order."""
        selector = DefaultHybridSelector()
        result = selector.select(reranked_chunks, vector_top_chunks, keep_m=6, keep_l=10)

        assert result[0]["page_title"] == "AURA Power"
        assert result[1]["page_title"] == "AURA Idle"
        assert result[2]["page_title"] == "AURA Suspend"
        assert result[3]["page_title"] == "AURA CPU"
        assert result[4]["page_title"] == "AURA BBSM"
        assert result[5]["page_title"] == "AURA Ops"

    def test_no_duplicates(self, reranked_chunks, vector_top_chunks):
        """Each unique chunk should appear exactly once."""
        selector = DefaultHybridSelector()
        result = selector.select(reranked_chunks, vector_top_chunks, keep_m=6, keep_l=10)

        texts = [c["text"] for c in result]
        assert len(texts) == len(set(texts))


# ---------------------------------------------------------------------------
# Tests: keep_m trims the reranked set
# ---------------------------------------------------------------------------

class TestKeepM:
    def test_keep_m_trims_reranked(self, reranked_chunks, vector_top_chunks):
        """Only top M reranked chunks should be kept."""
        selector = DefaultHybridSelector()
        result = selector.select(reranked_chunks, vector_top_chunks, keep_m=3, keep_l=0)

        assert len(result) == 3
        assert result[0]["page_title"] == "AURA Power"
        assert result[1]["page_title"] == "AURA Idle"
        assert result[2]["page_title"] == "AURA Suspend"

    def test_keep_m_larger_than_reranked(self, reranked_chunks, vector_top_chunks):
        """M larger than reranked length should just keep all reranked."""
        selector = DefaultHybridSelector()
        result = selector.select(reranked_chunks, vector_top_chunks, keep_m=100, keep_l=0)

        assert len(result) == 6

    def test_keep_m_zero_returns_only_vector(self, reranked_chunks, vector_top_chunks):
        """M=0 means no reranked chunks — only vector recall."""
        selector = DefaultHybridSelector()
        result = selector.select(reranked_chunks, vector_top_chunks, keep_m=0, keep_l=5)

        assert len(result) == 5
        assert result[0]["page_title"] == "AURA Power"  # from vector_top[0]


# ---------------------------------------------------------------------------
# Tests: keep_l controls vector recall
# ---------------------------------------------------------------------------

class TestKeepL:
    def test_keep_l_limits_vector_additions(self, reranked_chunks, vector_top_chunks):
        """Only the first L vector chunks should be considered."""
        selector = DefaultHybridSelector()
        # keep_l=4 → consider vector_top[0:4]
        # vector_top[0] dup, [1] new, [2] dup, [3] new
        result = selector.select(reranked_chunks, vector_top_chunks, keep_m=6, keep_l=4)

        titles = [c["page_title"] for c in result]
        assert "AURA Thermal" in titles   # vector_top[1] — new
        assert "SBCX Thermal" in titles   # vector_top[3] — new
        assert "AURA Modes" not in titles  # vector_top[4] — beyond L=4

    def test_keep_l_zero_returns_reranked_only(self, reranked_chunks, vector_top_chunks):
        """L=0 means no vector recall — reranked set only."""
        selector = DefaultHybridSelector()
        result = selector.select(reranked_chunks, vector_top_chunks, keep_m=6, keep_l=0)

        assert len(result) == 6

    def test_keep_l_larger_than_vector_top(self, reranked_chunks, vector_top_chunks):
        """L larger than vector_top length should not crash."""
        selector = DefaultHybridSelector()
        result = selector.select(reranked_chunks, vector_top_chunks, keep_m=6, keep_l=1000)

        # 6 reranked + 8 new from vector_top (10 total - 2 text dups)
        assert len(result) == 14


# ---------------------------------------------------------------------------
# Tests: disabled modes
# ---------------------------------------------------------------------------

class TestHybridDisabled:
    def test_disabled_returns_reranked(self, reranked_chunks, vector_top_chunks):
        """When hybrid selection is disabled, return reranked unchanged."""
        from retriva.qa.retriever import _hybrid_select_if_enabled

        with patch("retriva.qa.retriever.settings") as ms:
            ms.enable_retrieval_reranking = True
            ms.enable_hybrid_retrieval_selection = False
            result = _hybrid_select_if_enabled(reranked_chunks, vector_top_chunks)

        assert result is reranked_chunks

    def test_disabled_when_reranking_off(self, reranked_chunks, vector_top_chunks):
        """When reranking is off, hybrid selection should also be off."""
        from retriva.qa.retriever import _hybrid_select_if_enabled

        with patch("retriva.qa.retriever.settings") as ms:
            ms.enable_retrieval_reranking = False
            result = _hybrid_select_if_enabled(reranked_chunks, vector_top_chunks)

        assert result is reranked_chunks


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------

class TestHybridEdgeCases:
    def test_empty_reranked(self, vector_top_chunks):
        """Empty reranked set should get vector chunks appended."""
        selector = DefaultHybridSelector()
        result = selector.select([], vector_top_chunks, keep_m=6, keep_l=5)

        assert len(result) == 5

    def test_empty_vector_top(self, reranked_chunks):
        """Empty vector_top should return reranked[:M]."""
        selector = DefaultHybridSelector()
        result = selector.select(reranked_chunks, [], keep_m=6, keep_l=10)

        assert len(result) == 6

    def test_both_empty(self):
        """Both empty should return empty list."""
        selector = DefaultHybridSelector()
        result = selector.select([], [], keep_m=6, keep_l=10)

        assert result == []

    def test_complete_overlap(self):
        """When all vector chunks are already in precision set, no additions."""
        chunks = [
            {"text": "A", "page_title": "A"},
            {"text": "B", "page_title": "B"},
        ]
        selector = DefaultHybridSelector()
        result = selector.select(chunks, chunks, keep_m=10, keep_l=10)

        assert len(result) == 2


# ---------------------------------------------------------------------------
# Tests: answerer integration
# ---------------------------------------------------------------------------

class TestHybridAnswererIntegration:
    def test_answerer_passes_both_knobs(self, reranked_chunks, vector_top_chunks):
        """_hybrid_select_if_enabled passes keep_m and keep_l from settings."""
        from retriva.qa.retriever import _hybrid_select_if_enabled

        captured = {}

        class SpySelector:
            def select(self, reranked, vector_top, keep_m, keep_l):
                captured["keep_m"] = keep_m
                captured["keep_l"] = keep_l
                return reranked[:keep_m]

        with patch("retriva.qa.retriever.settings") as ms:
            ms.enable_retrieval_reranking = True
            ms.enable_hybrid_retrieval_selection = True
            ms.hybrid_rerank_keep_top_m = 4
            ms.hybrid_vector_keep_top_l = 8
            with patch("retriva.qa.retriever.CapabilityRegistry") as mr:
                mr.return_value.get_instance.return_value = SpySelector()
                _hybrid_select_if_enabled(reranked_chunks, vector_top_chunks)

        assert captured["keep_m"] == 4
        assert captured["keep_l"] == 8


# ---------------------------------------------------------------------------
# Tests: registry integration
# ---------------------------------------------------------------------------

class TestHybridSelectorRegistry:
    def test_registered_in_registry(self):
        """DefaultHybridSelector should be discoverable via the CapabilityRegistry."""
        from retriva.registry import CapabilityRegistry
        import importlib
        import retriva.qa.hybrid_selector
        importlib.reload(retriva.qa.hybrid_selector)
        registry = CapabilityRegistry()
        cls = registry.get("hybrid_selector")
        assert cls.__name__ == "DefaultHybridSelector"

    def test_override_at_higher_priority(self):
        """A Pro hybrid selector at priority > 100 should win."""
        from retriva.registry import CapabilityRegistry
        import importlib
        import retriva.qa.hybrid_selector
        importlib.reload(retriva.qa.hybrid_selector)

        registry = CapabilityRegistry()

        class ProSelector:
            def select(self, reranked, vector_top, keep_m, keep_l):
                return [{"text": "pro", "page_title": "Pro"}]

        registry.register("hybrid_selector", ProSelector, priority=200)
        assert registry.get("hybrid_selector") is ProSelector

        # Clean up
        importlib.reload(retriva.qa.hybrid_selector)
