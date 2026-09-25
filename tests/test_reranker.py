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

"""Unit tests for the DefaultReranker capability."""

import pytest
from unittest.mock import patch, MagicMock
from retriva.qa.reranker import DefaultReranker, _call_rerank_api, _truncate_documents, _rerank_batched


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_chunks():
    """Five chunks in vector-similarity order (worst-first for rerank)."""
    return [
        {"text": "Chunk A about power supply.", "page_title": "Power", "source_path": "/power"},
        {"text": "Chunk B about thermal design.", "page_title": "Thermal", "source_path": "/thermal"},
        {"text": "Chunk C about AURA SOM specs.", "page_title": "AURA", "source_path": "/aura"},
        {"text": "Chunk D about GPIO pinout.", "page_title": "GPIO", "source_path": "/gpio"},
        {"text": "Chunk E about memory layout.", "page_title": "Memory", "source_path": "/memory"},
    ]


def _mock_rerank_response(top_n: int, order: list[int]):
    """Build a mock /rerank response with the given index order."""
    results = []
    score = 0.99
    for idx in order[:top_n]:
        results.append({
            "index": idx,
            "relevance_score": round(score, 4),
            "document": {"text": f"chunk {idx}"},
        })
        score -= 0.1
    return results


# ---------------------------------------------------------------------------
# Tests: rerank reorders by score
# ---------------------------------------------------------------------------

class TestDefaultRerankerReorders:
    def test_rerank_reorders_chunks(self, sample_chunks):
        """Reranker should return chunks in the order dictated by the API."""
        reranker = DefaultReranker()
        # API says: index 2 (AURA) is most relevant, then 0 (Power), then 4 (Memory)
        mock_results = _mock_rerank_response(top_n=3, order=[2, 0, 4])

        with patch("retriva.qa.reranker._call_rerank_api", return_value=mock_results):
            result = reranker.rerank("power consumption", sample_chunks, top_n=3)

        assert len(result) == 3
        assert result[0]["page_title"] == "AURA"
        assert result[1]["page_title"] == "Power"
        assert result[2]["page_title"] == "Memory"

    def test_rerank_preserves_metadata(self, sample_chunks):
        """All original chunk metadata must survive the reranking."""
        reranker = DefaultReranker()
        mock_results = _mock_rerank_response(top_n=2, order=[1, 3])

        with patch("retriva.qa.reranker._call_rerank_api", return_value=mock_results):
            result = reranker.rerank("thermal", sample_chunks, top_n=2)

        assert result[0]["source_path"] == "/thermal"
        assert result[1]["source_path"] == "/gpio"


# ---------------------------------------------------------------------------
# Tests: disabled reranker
# ---------------------------------------------------------------------------

class TestRerankerDisabled:
    def test_rerank_disabled_returns_unchanged(self, sample_chunks):
        """When reranker is disabled, chunks should pass through unchanged."""
        from retriva.qa.retriever import _rerank_if_enabled

        with patch("retriva.qa.retriever.settings") as mock_ans_settings:
            mock_ans_settings.enable_retrieval_reranking = False
            result = _rerank_if_enabled("query", sample_chunks)

        assert result == sample_chunks
        assert len(result) == 5


# ---------------------------------------------------------------------------
# Tests: graceful degradation
# ---------------------------------------------------------------------------

class TestRerankerGracefulDegradation:
    def test_rerank_api_failure_returns_truncated(self, sample_chunks):
        """If the API call fails, reranker falls back to vector-search order."""
        reranker = DefaultReranker()

        with patch(
            "retriva.qa.reranker._call_rerank_api",
            side_effect=RuntimeError("connection refused"),
        ):
            result = reranker.rerank("power", sample_chunks, top_n=3)

        # Should return first 3 in original order
        assert len(result) == 3
        assert result[0]["page_title"] == "Power"
        assert result[1]["page_title"] == "Thermal"
        assert result[2]["page_title"] == "AURA"

    def test_rerank_empty_results_returns_truncated(self, sample_chunks):
        """If the API returns empty results, fall back gracefully."""
        reranker = DefaultReranker()

        with patch("retriva.qa.reranker._call_rerank_api", return_value=[]):
            result = reranker.rerank("power", sample_chunks, top_n=3)

        assert len(result) == 3
        assert result[0]["page_title"] == "Power"


# ---------------------------------------------------------------------------
# Tests: top_n clamping
# ---------------------------------------------------------------------------

class TestRerankerTopN:
    def test_top_n_clamps_to_chunk_count(self, sample_chunks):
        """top_n larger than chunk count should be clamped."""
        reranker = DefaultReranker()
        mock_results = _mock_rerank_response(top_n=5, order=[0, 1, 2, 3, 4])

        with patch("retriva.qa.reranker._call_rerank_api", return_value=mock_results):
            result = reranker.rerank("all", sample_chunks, top_n=100)

        assert len(result) == 5

    def test_empty_chunks_returns_empty(self):
        """Reranking an empty list should return an empty list."""
        reranker = DefaultReranker()
        result = reranker.rerank("query", [], top_n=10)
        assert result == []


# ---------------------------------------------------------------------------
# Tests: out-of-bounds index handling
# ---------------------------------------------------------------------------

class TestRerankerBoundsChecking:
    def test_out_of_bounds_index_skipped(self, sample_chunks):
        """Indices outside the chunk list should be skipped safely."""
        reranker = DefaultReranker()
        mock_results = [
            {"index": 0, "relevance_score": 0.9, "document": {"text": ""}},
            {"index": 999, "relevance_score": 0.8, "document": {"text": ""}},  # bad
            {"index": 2, "relevance_score": 0.7, "document": {"text": ""}},
        ]

        with patch("retriva.qa.reranker._call_rerank_api", return_value=mock_results):
            result = reranker.rerank("test", sample_chunks, top_n=3)

        assert len(result) == 2  # index 999 was skipped
        assert result[0]["page_title"] == "Power"
        assert result[1]["page_title"] == "AURA"


# ---------------------------------------------------------------------------
# Tests: text truncation
# ---------------------------------------------------------------------------

class TestTextTruncation:
    def test_truncate_documents_respects_max_length(self):
        """Documents longer than max_length should be truncated."""
        docs = ["a" * 100, "b" * 200, "c" * 50]
        result = _truncate_documents(docs, 80)

        assert len(result[0]) == 80
        assert len(result[1]) == 80
        assert len(result[2]) == 50  # already under limit

    def test_truncate_documents_zero_means_no_limit(self):
        """max_length <= 0 should skip truncation entirely."""
        docs = ["a" * 10000]
        result = _truncate_documents(docs, 0)
        assert len(result[0]) == 10000

    def test_truncate_applied_before_api_call(self, sample_chunks):
        """The reranker should truncate text before sending to the API."""
        reranker = DefaultReranker()
        # Create a chunk with very long text
        long_chunk = {"text": "x" * 10000, "page_title": "Long", "source_path": "/long"}
        chunks = [long_chunk]

        captured_docs = []

        def mock_api(query, documents, top_n):
            captured_docs.extend(documents)
            return [{"index": 0, "relevance_score": 0.9}]

        with patch("retriva.qa.reranker._call_rerank_api", side_effect=mock_api):
            with patch("retriva.qa.reranker.settings") as ms:
                ms.retrieval_rerank_max_length = 500
                ms.retrieval_rerank_batch_size = 100
                reranker.rerank("test", chunks, top_n=1)

        assert len(captured_docs) == 1
        assert len(captured_docs[0]) == 500


# ---------------------------------------------------------------------------
# Tests: batch splitting
# ---------------------------------------------------------------------------

class TestBatchSplitting:
    def test_single_batch_no_splitting(self):
        """When docs fit in one batch, no splitting should occur."""
        docs = ["doc1", "doc2", "doc3"]
        mock_results = [
            {"index": 2, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.8},
        ]

        with patch("retriva.qa.reranker._call_rerank_api", return_value=mock_results) as mock_api:
            result = _rerank_batched("query", docs, top_n=2, batch_size=10)

        # Should call the API exactly once
        mock_api.assert_called_once()
        assert len(result) == 2

    def test_multi_batch_merges_and_sorts(self):
        """When docs exceed batch_size, results should be merged by score."""
        # 6 docs, batch_size=3 → two batches
        docs = [f"doc{i}" for i in range(6)]

        # Batch 1 (indices 0-2): doc1 is best
        batch1_results = [
            {"index": 1, "relevance_score": 0.95},
            {"index": 0, "relevance_score": 0.70},
        ]
        # Batch 2 (indices 3-5): doc5 is best
        batch2_results = [
            {"index": 2, "relevance_score": 0.90},  # global index will be 5
            {"index": 0, "relevance_score": 0.60},   # global index will be 3
        ]

        call_count = [0]

        def mock_api(query, documents, top_n):
            call_count[0] += 1
            if call_count[0] == 1:
                return batch1_results
            return batch2_results

        with patch("retriva.qa.reranker._call_rerank_api", side_effect=mock_api):
            result = _rerank_batched("query", docs, top_n=3, batch_size=3)

        # Should call API twice
        assert call_count[0] == 2

        # Should return top 3 by global score: 0.95, 0.90, 0.70
        assert len(result) == 3
        assert result[0]["relevance_score"] == 0.95
        assert result[0]["index"] == 1    # global: batch1 index 1
        assert result[1]["relevance_score"] == 0.90
        assert result[1]["index"] == 5    # global: batch2 index 2 + offset 3
        assert result[2]["relevance_score"] == 0.70
        assert result[2]["index"] == 0    # global: batch1 index 0


# ---------------------------------------------------------------------------
# Tests: candidate selection
# ---------------------------------------------------------------------------

class TestCandidateSelection:
    def test_candidate_slicing_in_answerer(self):
        """_rerank_if_enabled should slice chunks to retrieval_rerank_candidates."""
        from retriva.qa.retriever import _rerank_if_enabled

        chunks = [{"text": f"chunk{i}", "page_title": f"P{i}"} for i in range(50)]
        captured_chunks = []

        class MockReranker:
            def rerank(self, query, chunks, top_n):
                captured_chunks.extend(chunks)
                return chunks[:top_n]

        with patch("retriva.qa.retriever.settings") as ms:
            ms.enable_retrieval_reranking = True
            ms.retrieval_rerank_candidates = 20
            ms.retrieval_rerank_top_n = 10
            with patch("retriva.qa.retriever.CapabilityRegistry") as mock_reg:
                mock_reg.return_value.get_instance.return_value = MockReranker()
                result = _rerank_if_enabled("query", chunks)

        # Reranker should have received only 20 candidates (not all 50)
        assert len(captured_chunks) == 20
        assert len(result) == 10

    def test_candidate_slicing_skipped_when_zero(self):
        """retrieval_rerank_candidates <= 0 should skip slicing."""
        from retriva.qa.retriever import _rerank_if_enabled

        chunks = [{"text": f"chunk{i}", "page_title": f"P{i}"} for i in range(50)]
        captured_chunks = []

        class MockReranker:
            def rerank(self, query, chunks, top_n):
                captured_chunks.extend(chunks)
                return chunks[:top_n]

        with patch("retriva.qa.retriever.settings") as ms:
            ms.enable_retrieval_reranking = True
            ms.retrieval_rerank_candidates = 0  # disabled
            ms.retrieval_rerank_top_n = 10
            with patch("retriva.qa.retriever.CapabilityRegistry") as mock_reg:
                mock_reg.return_value.get_instance.return_value = MockReranker()
                result = _rerank_if_enabled("query", chunks)

        # Reranker should have received all 50 candidates
        assert len(captured_chunks) == 50


# ---------------------------------------------------------------------------
# Tests: registry integration
# ---------------------------------------------------------------------------

class TestRerankerRegistry:
    def test_reranker_registered_in_registry(self):
        """DefaultReranker should be discoverable via the CapabilityRegistry."""
        from retriva.registry import CapabilityRegistry
        import importlib
        import retriva.qa.reranker
        importlib.reload(retriva.qa.reranker)
        registry = CapabilityRegistry()
        cls = registry.get("reranker")
        assert cls.__name__ == "DefaultReranker"

    def test_reranker_instance_has_rerank_method(self):
        """The resolved instance must expose a rerank() method."""
        from retriva.registry import CapabilityRegistry
        import importlib
        import retriva.qa.reranker
        importlib.reload(retriva.qa.reranker)
        registry = CapabilityRegistry()
        instance = registry.get_instance("reranker")
        assert hasattr(instance, "rerank")
        assert callable(instance.rerank)
