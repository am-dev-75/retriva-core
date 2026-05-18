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
from fastapi.testclient import TestClient
from retriva.config import settings
from retriva.ingestion_api.main import app
from retriva.indexing.qdrant_store import get_client, COLLECTION_NAME
from qdrant_client.models import PointStruct, VectorParams, Distance
import uuid

client = TestClient(app)

@pytest.fixture
def mock_data():
    q_client = get_client()
    # Clean and ensure collection exists for predictable tests
    if q_client.collection_exists(COLLECTION_NAME):
        q_client.delete_collection(COLLECTION_NAME)
    q_client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=settings.embedding_dimension, distance=Distance.COSINE),
    )
    
    # Upsert some test points
    points = [
        PointStruct(
            id=str(uuid.uuid4()),
            vector=[0.9] * settings.embedding_dimension, # Very relevant to "space"
            payload={
                "doc_id": "doc1",
                "source_path": "apollo.md",
                "page_title": "Project Apollo",
                "text": "Project Apollo was the third United States human spaceflight program.",
                "user_metadata": {"project": "apollo", "status": "active"}
            }
        ),
        PointStruct(
            id=str(uuid.uuid4()),
            vector=[0.1] * settings.embedding_dimension, # Irrelevant to "space"
            payload={
                "doc_id": "doc2",
                "source_path": "gemini.md",
                "page_title": "Project Gemini",
                "text": "Project Gemini was the second United States human spaceflight program.",
                "user_metadata": {"project": "gemini", "status": "completed"}
            }
        ),
        PointStruct(
            id=str(uuid.uuid4()),
            vector=[0.05] * settings.embedding_dimension, # Very irrelevant to "space"
            payload={
                "doc_id": "doc3",
                "source_path": "irrelevant.md",
                "page_title": "Not about space",
                "text": "This document has nothing to do with space exploration.",
                "user_metadata": {"project": "apollo", "status": "hidden"}
            }
        )
    ]
    q_client.upsert(collection_name=COLLECTION_NAME, points=points)
    yield
    # Cleanup

def test_hard_filtering(mock_data):
    # Search for project=apollo in hard mode
    response = client.post("/api/v2/retrieval/query", json={
        "query": "space",
        "metadata_filters": [
            {"field": "user_metadata.project", "operator": "eq", "value": "apollo"}
        ],
        "metadata_filter_mode": "hard"
    })
    assert response.status_code == 200
    data = response.json()
    # Should only return doc1 and doc3 (matching metadata), but filtered by semantic similarity?
    # Actually, hard mode strictly excludes doc2.
    for chunk in data["chunks"]:
        assert chunk["user_metadata"]["project"] == "apollo"
        assert chunk["page_title"] != "Project Gemini"

def test_soft_filtering_multi_recall(mock_data):
    # Search for "space" with project=apollo in soft mode
    # "doc3" is semantically irrelevant but matches metadata.
    # It should be recalled in soft mode because of multi-recall.
    response = client.post("/api/v2/retrieval/query", json={
        "query": "space",
        "metadata_filters": [
            {"field": "user_metadata.project", "operator": "eq", "value": "apollo"}
        ],
        "metadata_filter_mode": "soft"
    })
    assert response.status_code == 200
    data = response.json()
    
    chunk_titles = [c["page_title"] for c in data["chunks"]]
    # doc1 (semantic + metadata) should definitely be there
    assert "Project Apollo" in chunk_titles
    # doc3 (metadata match but low semantic) should ALSO be there due to multi-recall
    assert "Not about space" in chunk_titles
    # doc2 (semantic but no metadata match) should ALSO be there because it's soft mode
    assert "Project Gemini" in chunk_titles

def test_default_mode_is_soft(mock_data):
    # Default mode should be soft
    response = client.post("/api/v2/retrieval/query", json={
        "query": "space",
        "metadata_filters": [
            {"field": "user_metadata.project", "operator": "eq", "value": "apollo"}
        ]
    })
    assert response.status_code == 200
    data = response.json()
    chunk_titles = [c["page_title"] for c in data["chunks"]]
    # If it was hard mode, Gemini wouldn't be there. Since it's soft, it should be there.
    assert "Project Gemini" in chunk_titles

def test_document_search_deduplication(mock_data):
    response = client.post("/api/v2/documents/search", json={
        "query": "space",
        "metadata_filters": [
            {"field": "user_metadata.project", "operator": "eq", "value": "apollo"}
        ]
    })
    assert response.status_code == 200
    data = response.json()
    doc_ids = [d["doc_id"] for d in data["documents"]]
    # Should contain doc1 and doc3, but NO duplicates
    assert len(doc_ids) == len(set(doc_ids))
    assert "doc1" in doc_ids
    assert "doc3" in doc_ids
