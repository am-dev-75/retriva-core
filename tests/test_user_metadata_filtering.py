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
import uuid
import asyncio
from unittest.mock import patch
from retriva.config import settings
from retriva.domain.models import Chunk, ChunkMetadata
from retriva.indexing.qdrant_store import init_collection, upsert_chunks, search_chunks, delete_chunks_by_metadata, get_client
from retriva.qa.retriever import DefaultRetriever

@pytest.fixture(scope="module")
def setup_qdrant():
    # Use a specific collection for this test to avoid interfering with others
    test_collection = "test_metadata_filtering_" + uuid.uuid4().hex[:8]
    
    with patch("retriva.indexing.qdrant_store.COLLECTION_NAME", test_collection):
        client = get_client()
        init_collection(client, vector_size=1024) # Ensure dimension is explicit or from settings
        
        # 1. Ingest chunks with different metadata
        chunk1 = Chunk(
            text="Aura SOM uses 2.8W power",
            metadata=ChunkMetadata(
                doc_id="doc1.md",
                chunk_id=uuid.uuid4().hex,
                chunk_index=0,
                source_path="doc1.md",
                section_path="",
                page_title="Aura Specs",
                user_metadata={"board": "aura", "tenant": "acme"}
            )
        )
        
        chunk2 = Chunk(
            text="Bora SOM uses 3.5W power",
            metadata=ChunkMetadata(
                doc_id="doc2.md",
                chunk_id=uuid.uuid4().hex,
                chunk_index=0,
                source_path="doc2.md",
                section_path="",
                page_title="Bora Specs",
                user_metadata={"board": "bora", "tenant": "acme"}
            )
        )
        
        chunk3 = Chunk(
            text="Acme top secret board uses 1.0W power",
            metadata=ChunkMetadata(
                doc_id="doc3.md",
                chunk_id=uuid.uuid4().hex,
                chunk_index=0,
                source_path="doc3.md",
                section_path="",
                page_title="Secret Specs",
                user_metadata={"board": "secret", "tenant": "globex"}
            )
        )
        
        upsert_chunks(client, [chunk1, chunk2, chunk3])
        
        # Sleep briefly to ensure Qdrant indexing is complete
        import time
        time.sleep(1)
        
        yield client
        
        # Teardown
        client.delete_collection(test_collection)

def test_qdrant_metadata_filtering(setup_qdrant):
    client = setup_qdrant
    
    # 2. Test search with no filter
    from retriva.indexing.embeddings import get_embeddings
    query_vector = get_embeddings(["power consumption"])[0]
    
    results = search_chunks(client, query_vector, retriever_top_k=10)
    assert len(results) == 3
    
    # 3. Test search with metadata filter (tenant=acme)
    results_acme = search_chunks(
        client,
        query_vector,
        retriever_top_k=10,
        metadata_filters=[{"field": "user_metadata.tenant", "operator": "eq", "value": "acme"}],
        metadata_filter_mode="hard"
    )
    assert len(results_acme) == 2
    boards = {r["user_metadata"]["board"] for r in results_acme}
    assert boards == {"aura", "bora"}
    
    # 4. Test search with multi-key filter
    results_aura = search_chunks(
        client,
        query_vector,
        retriever_top_k=10,
        metadata_filters=[
            {"field": "user_metadata.tenant", "operator": "eq", "value": "acme"},
            {"field": "user_metadata.board", "operator": "eq", "value": "aura"}
        ],
        metadata_filter_mode="hard"
    )
    assert len(results_aura) == 1
    assert results_aura[0]["user_metadata"]["board"] == "aura"
    
    # 5. Test DefaultRetriever passes filter correctly
    retriever = DefaultRetriever()
    retriever_results = retriever.retrieve(
        "power consumption",
        top_k=10,
        metadata_filters=[{"field": "user_metadata.tenant", "operator": "eq", "value": "globex"}],
        metadata_filter_mode="hard",
        rerank=False,
        hybrid_selection=False
    )
    assert len(retriever_results) == 1
    assert retriever_results[0]["user_metadata"]["board"] == "secret"

def test_qdrant_metadata_deletion(setup_qdrant):
    client = setup_qdrant
    
    query_vector = [0.1] * settings.embedding_dimension  # Dummy vector
    
    # Delete where tenant=acme
    delete_chunks_by_metadata(client, {"tenant": "acme"})
    
    # Sleep to allow delete to propagate
    import time
    time.sleep(1)
    
    # Verify remaining chunks
    results = search_chunks(client, query_vector, retriever_top_k=10)
    assert len(results) == 1
    assert results[0]["user_metadata"]["tenant"] == "globex"
