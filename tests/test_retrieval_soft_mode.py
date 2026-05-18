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

import pytest
from unittest.mock import patch, MagicMock
from retriva.qa.retriever import DefaultRetriever
from retriva.config import settings

@pytest.fixture
def mock_chunks():
    # Helper to create mock chunks
    def _create_chunks(doc_id, count, base_score=0.9):
        chunks = []
        for i in range(count):
            chunks.append({
                "doc_id": doc_id,
                "text": f"Content from {doc_id} chunk {i}",
                "page_title": f"Title {doc_id}",
                "source_path": f"path/{doc_id}",
                "score": base_score - (i * 0.01), # Qdrant score
                "payload": {
                    "doc_id": doc_id,
                    "text": f"Content from {doc_id} chunk {i}"
                }
            })
        return chunks
    return _create_chunks

@patch("retriva.qa.retriever.get_client")
@patch("retriva.indexing.qdrant_store.QdrantClient")
@patch("retriva.qa.retriever.get_embeddings")
def test_soft_mode_semantic_priority(mock_embeddings, mock_qdrant_client, mock_get_client, mock_chunks):
    """Case B & C: Verify that strong semantic relevance beats weak metadata matches in soft mode."""
    from retriva.indexing.qdrant_store import search_chunks
    
    # 1. Setup mock data
    # - Global search finds a very strong match (0.95) with NO metadata
    # - Constrained search finds a metadata match with weak semantic (0.5)
    
    strong_semantic = MagicMock()
    strong_semantic.id = "strong_id"
    strong_semantic.score = 0.95
    strong_semantic.payload = {"doc_id": "global.pdf", "text": "strong semantic match"}
    
    weak_metadata = MagicMock()
    weak_metadata.id = "weak_id"
    weak_metadata.score = 0.5
    weak_metadata.payload = {"doc_id": "meta.pdf", "text": "weak semantic but meta match"}
    
    # Mock Qdrant query_points calls
    # first call is global, second is metadata-constrained
    mock_qdrant_client.query_points.side_effect = [
        MagicMock(points=[strong_semantic]), # global
        MagicMock(points=[weak_metadata])    # constrained
    ]
    
    mock_embeddings.return_value = [[0.1] * 1024]
    
    # We call search_chunks directly to verify the scoring
    results = search_chunks(
        client=mock_qdrant_client,
        query_vector=[0.1]*1024,
        retriever_top_k=10,
        metadata_filters=[{"field": "project", "value": "apollo"}],
        metadata_filter_mode="soft"
    )
    
    # Expected scores:
    # strong: 0.95 (no boost)
    # weak: 0.5 + 0.1 (boost) = 0.6
    # Strong should still be first
    
    assert results[0]["doc_id"] == "global.pdf"
    assert results[0]["_score"] == 0.95
    assert results[1]["doc_id"] == "meta.pdf"
    assert results[1]["_score"] == 0.6
    assert "semantic" in results[0]["_match_reasons"]
    assert "metadata:project" in results[1]["_match_reasons"]

@patch("retriva.qa.retriever.get_client")
@patch("retriva.indexing.qdrant_store.QdrantClient")
@patch("retriva.qa.retriever.get_embeddings")
def test_soft_mode_boost_verified(mock_embeddings, mock_qdrant_client, mock_get_client, mock_chunks):
    """Case A & D: Verify that metadata boost helps when semantic scores are close."""
    from retriva.indexing.qdrant_store import search_chunks
    
    # 1. Setup mock data
    # - Doc A: 0.82 semantic, no meta
    # - Doc B: 0.80 semantic, matches meta
    
    doc_a = MagicMock(id="a", score=0.82, payload={"doc_id": "A.pdf"})
    doc_b = MagicMock(id="b", score=0.80, payload={"doc_id": "B.pdf"})
    
    mock_qdrant_client.query_points.side_effect = [
        MagicMock(points=[doc_a, doc_b]), # global
        MagicMock(points=[doc_b])         # constrained
    ]
    
    mock_embeddings.return_value = [[0.1] * 1024]
    
    results = search_chunks(
        client=mock_qdrant_client,
        query_vector=[0.1]*1024,
        retriever_top_k=10,
        metadata_filters=[{"field": "project", "value": "apollo"}],
        metadata_filter_mode="soft"
    )
    
    # Scores:
    # A: 0.82
    # B: 0.80 + 0.1 = 0.90
    # B should now outrank A because of the metadata boost
    
    assert results[0]["doc_id"] == "B.pdf"
    assert results[1]["doc_id"] == "A.pdf"

@patch("retriva.qa.retriever.get_client")
@patch("retriva.indexing.qdrant_store.QdrantClient")
@patch("retriva.qa.retriever.get_embeddings")
def test_hard_mode_regression(mock_embeddings, mock_qdrant_client, mock_get_client, mock_chunks):
    """Case E: Ensure hard mode behavior is unchanged (strict pre-filtering)."""
    from retriva.indexing.qdrant_store import search_chunks
    
    doc_b = MagicMock(id="b", score=0.80, payload={"doc_id": "B.pdf"})
    
    mock_qdrant_client.query_points.return_value = MagicMock(points=[doc_b])
    mock_embeddings.return_value = [[0.1] * 1024]
    
    results = search_chunks(
        client=mock_qdrant_client,
        query_vector=[0.1]*1024,
        retriever_top_k=10,
        metadata_filters=[{"field": "project", "value": "apollo"}],
        metadata_filter_mode="hard"
    )
    
    # Hard mode only calls query_points once with the filter
    assert mock_qdrant_client.query_points.call_count == 1
    assert len(results) == 1
    assert results[0]["doc_id"] == "B.pdf"
    # No boost applied in hard mode score (returns raw Qdrant score)
    assert results[0]["_score"] == 0.80
