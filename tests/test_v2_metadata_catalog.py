import pytest
import uuid
import json
from unittest.mock import patch
from fastapi.testclient import TestClient

from retriva.config import settings
from retriva.domain.models import Chunk, ChunkMetadata
from retriva.indexing.qdrant_store import init_collection, upsert_chunks, get_client
from retriva.ingestion_api.main import app

@pytest.fixture(scope="module")
def setup_qdrant_catalog():
    test_collection = "test_catalog_" + uuid.uuid4().hex[:8]
    
    with patch("retriva.indexing.qdrant_store.COLLECTION_NAME", test_collection):
        client = get_client()
        init_collection(client, vector_size=1024)
        
        chunk1 = Chunk(
            text="Apollo project specs for R&D.",
            metadata=ChunkMetadata(
                doc_id="apollo_doc_1",
                chunk_id=uuid.uuid4().hex,
                chunk_index=0,
                source_path="apollo_spec.md",
                section_path="",
                page_title="Apollo Specs",
                user_metadata={"project": "apollo", "department": "r&d"}
            )
        )
        
        chunk2 = Chunk(
            text="Apollo marketing material.",
            metadata=ChunkMetadata(
                doc_id="apollo_doc_2",
                chunk_id=uuid.uuid4().hex,
                chunk_index=0,
                source_path="apollo_marketing.md",
                section_path="",
                page_title="Apollo Marketing",
                user_metadata={"project": "apollo", "department": "marketing"}
            )
        )
        
        chunk3 = Chunk(
            text="Zeus project specs for R&D.",
            metadata=ChunkMetadata(
                doc_id="zeus_doc_1",
                chunk_id=uuid.uuid4().hex,
                chunk_index=0,
                source_path="zeus_spec.md",
                section_path="",
                page_title="Zeus Specs",
                user_metadata={"project": "zeus", "department": "r&d"}
            )
        )
        
        upsert_chunks(client, [chunk1, chunk2, chunk3])
        
        import time
        time.sleep(1)
        
        yield test_collection
        
        client.delete_collection(test_collection)

@pytest.fixture(scope="module")
def test_client():
    return TestClient(app)

def test_catalog_schema_values(setup_qdrant_catalog, test_client):
    test_collection = setup_qdrant_catalog
    with patch("retriva.indexing.qdrant_store.COLLECTION_NAME", test_collection):
        # Test schema
        response = test_client.get("/api/v2/metadata/schema")
        assert response.status_code == 200
        assert "project" in response.json()["keys"]
        assert "department" in response.json()["keys"]
        
        # Test values for project
        response = test_client.get("/api/v2/metadata/values?key=project")
        assert response.status_code == 200
        assert set(response.json()["values"]) == {"apollo", "zeus"}
        
        # Test values for department
        response = test_client.get("/api/v2/metadata/values?key=department")
        assert response.status_code == 200
        assert set(response.json()["values"]) == {"r&d", "marketing"}

def test_catalog_documents_listing(setup_qdrant_catalog, test_client):
    test_collection = setup_qdrant_catalog
    with patch("retriva.indexing.qdrant_store.COLLECTION_NAME", test_collection):
        # All documents
        response = test_client.get("/api/v2/documents")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 3
        
        # Count all
        response = test_client.get("/api/v2/documents/count")
        assert response.status_code == 200
        assert response.json()["count"] == 3
        
        # Filter by project=apollo
        filter_str = json.dumps({"project": "apollo"})
        response = test_client.get(f"/api/v2/documents?user_metadata_filter={filter_str}")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        
        # Count project=apollo
        response = test_client.get(f"/api/v2/documents/count?user_metadata_filter={filter_str}")
        assert response.json()["count"] == 2
        
        # Filter by project=apollo and department=r&d
        filter_str = json.dumps({"project": "apollo", "department": "r&d"})
        response = test_client.get(f"/api/v2/documents?user_metadata_filter={filter_str}")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["documents"][0]["doc_id"] == "apollo_doc_1"

def test_catalog_document_get(setup_qdrant_catalog, test_client):
    test_collection = setup_qdrant_catalog
    with patch("retriva.indexing.qdrant_store.COLLECTION_NAME", test_collection):
        response = test_client.get("/api/v2/documents/apollo_doc_1")
        assert response.status_code == 200
        assert response.json()["page_title"] == "Apollo Specs"

def test_retrieval_query(setup_qdrant_catalog, test_client):
    test_collection = setup_qdrant_catalog
    with patch("retriva.indexing.qdrant_store.COLLECTION_NAME", test_collection):
        payload = {
            "query": "specs",
            "top_k": 5,
            "user_metadata_filter": {"project": "apollo", "department": "r&d"}
        }
        response = test_client.post("/api/v2/retrieval/query", json=payload)
        assert response.status_code == 200
        chunks = response.json()["chunks"]
        assert len(chunks) == 1
        assert chunks[0]["user_metadata"]["project"] == "apollo"
