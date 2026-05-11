import asyncio
from fastapi.testclient import TestClient
from retriva.ingestion_api.main import app

client = TestClient(app)

def test():
    # Attempting to verify GET with query params
    res = client.get("/api/v2/documents?metadata.project=apollo")
    print(res.status_code, res.json())

test()
