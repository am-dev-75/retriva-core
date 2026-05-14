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
                "_score": base_score - (i * 0.01),
                "_match_reasons": ["semantic"]
            })
        return chunks
    return _create_chunks

@patch("retriva.qa.retriever.get_client")
@patch("retriva.qa.retriever.search_chunks")
@patch("retriva.qa.retriever.get_embeddings")
def test_retrieval_diversity_cap_respected(mock_embeddings, mock_search, mock_client, mock_chunks):
    """Case B: Verify that max_chunks_per_doc is respected."""
    # 1. Setup mock data: 10 chunks from the same doc
    all_chunks = mock_chunks("large_doc.pdf", 10)
    mock_search.return_value = all_chunks
    mock_embeddings.return_value = [[0.1] * 1024]
    
    retriever = DefaultRetriever()
    
    # Use default settings (max_chunks_per_doc=3)
    top_k = 5
    results = retriever.retrieve(
        query="test query",
        top_k=top_k,
        metadata_filter_mode="hard",
        rerank=False,
        hybrid_selection=False
    )
    
    # Even though top_k=5, we only expect 3 chunks because they all belong to the same doc
    assert len(results) == 3
    for res in results:
        assert res["doc_id"] == "large_doc.pdf"

@patch("retriva.qa.retriever.get_client")
@patch("retriva.qa.retriever.search_chunks")
@patch("retriva.qa.retriever.get_embeddings")
def test_retrieval_diversity_improves_distribution(mock_embeddings, mock_search, mock_client, mock_chunks):
    """Case A & C: Verify that small documents are not drowned by large ones."""
    # 1. Setup mock data: 
    # - 20 chunks from a "noisy" large doc (scores 0.8 to 0.6)
    # - 1 chunk from a very relevant small doc (score 0.85)
    large_doc_chunks = mock_chunks("noisy.pdf", 20, base_score=0.8)
    small_doc_chunk = mock_chunks("relevant.png", 1, base_score=0.85)[0]
    
    # In a real scenario, the small_doc_chunk might be at position 0, 
    # but without diversity filtering, it might be followed by 19 chunks of the same doc.
    # If top_k=5 and the first 20 were from noisy.pdf, the small doc would be lost if it were at index 6+.
    
    # Let's simulate the scenario where the small doc is at index 5 (6th position)
    all_candidates = large_doc_chunks[:5] + [small_doc_chunk] + large_doc_chunks[5:]
    mock_search.return_value = all_candidates
    mock_embeddings.return_value = [[0.1] * 1024]
    
    retriever = DefaultRetriever()
    
    # With top_k=5 and max_per_doc=3
    results = retriever.retrieve(
        query="test query",
        top_k=5,
        metadata_filter_mode="hard",
        rerank=False,
        hybrid_selection=False
    )
    
    # We expect:
    # - 3 chunks from noisy.pdf (indices 0, 1, 2)
    # - 1 chunk from relevant.png (index 5 in candidates, but floats up because noisy.pdf is capped)
    # - 1 more chunk from noisy.pdf? No, noisy.pdf is already capped at 3.
    # Actually, the logic is: keep top 3 per doc, then take top_k.
    
    doc_ids = [r["doc_id"] for r in results]
    assert "relevant.png" in doc_ids
    assert doc_ids.count("noisy.pdf") == 3
    assert len(results) == 4 # Total unique matches capped

@patch("retriva.qa.retriever.get_client")
@patch("retriva.qa.retriever.search_chunks")
@patch("retriva.qa.retriever.get_embeddings")
def test_retrieval_fetch_k_is_increased(mock_embeddings, mock_search, mock_client, mock_chunks):
    """Verify that fetch_k is increased in hard mode."""
    mock_search.return_value = []
    mock_embeddings.return_value = [[0.1] * 1024]
    
    retriever = DefaultRetriever()
    top_k = 10
    retriever.retrieve(
        query="test query",
        top_k=top_k,
        metadata_filter_mode="hard",
        rerank=False,
        hybrid_selection=False
    )
    
    # fetch_k = max(10 * 5, 50) = 50
    assert mock_search.call_args[1]["retriever_top_k"] == 50

@patch("retriva.qa.retriever.get_client")
@patch("retriva.qa.retriever.search_chunks")
@patch("retriva.qa.retriever.get_embeddings")
def test_retrieval_soft_mode_no_diversity(mock_embeddings, mock_search, mock_client, mock_chunks):
    """Verify that soft mode does not apply diversity filtering (per current requirement)."""
    all_chunks = mock_chunks("large_doc.pdf", 10)
    mock_search.return_value = all_chunks
    mock_embeddings.return_value = [[0.1] * 1024]
    
    retriever = DefaultRetriever()
    
    # Soft mode
    top_k = 5
    results = retriever.retrieve(
        query="test query",
        top_k=top_k,
        metadata_filter_mode="soft",
        rerank=False,
        hybrid_selection=False
    )
    
    # In soft mode, diversity is not applied yet, so we get all 5 from the same doc
    assert len(results) == 5
    assert all(r["doc_id"] == "large_doc.pdf" for r in results)
