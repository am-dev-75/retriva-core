import pytest
import shutil
from pathlib import Path
from typing import Dict, List, Any
import json
import os

from retriva.ingestion.mediawiki_v2_parser import process_mediawiki_export
from retriva.ingestion.dedup import DeduplicationStore
from retriva.ingestion_api.job_manager import JobManager
from retriva.domain.models import DocRecord
from retriva.indexing.qdrant_store import COLLECTION_NAME

@pytest.fixture
def temp_dedup_store(tmp_path):
    store_path = tmp_path / "dedup.json"
    store = DeduplicationStore(catalog_path=str(store_path))
    return store, store_path

@pytest.fixture
def mock_qdrant(monkeypatch):
    upserted_chunks = []
    def mock_upsert(client, chunks, cancel_check=None):
        upserted_chunks.extend(chunks)
    monkeypatch.setattr("retriva.ingestion.mediawiki_v2_parser.upsert_chunks", mock_upsert)
    
    payload_updates = []
    def mock_update_payload(client, doc_id, payload):
        payload_updates.append((doc_id, payload))
    monkeypatch.setattr("retriva.ingestion.mediawiki_v2_parser.update_payload_by_doc_id", mock_update_payload)
    
    def mock_get_client():
        return "mock_client"
    monkeypatch.setattr("retriva.ingestion.mediawiki_v2_parser.get_client", mock_get_client)
    
    return {"chunks": upserted_chunks, "updates": payload_updates}

@pytest.fixture
def sample_export_dir(tmp_path):
    staged_dir = tmp_path / "staged"
    staged_dir.mkdir()
    
    xml_content = """<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://www.mediawiki.org/xml/export-0.11/ http://www.mediawiki.org/xml/export-0.11.xsd" version="0.11" xml:lang="en">
  <page>
    <title>Page 1</title>
    <ns>0</ns>
    <id>1</id>
    <revision>
      <id>1</id>
      <text bytes="20" xml:space="preserve">This is page 1.</text>
    </revision>
  </page>
  <page>
    <title>Page 2</title>
    <ns>0</ns>
    <id>2</id>
    <revision>
      <id>2</id>
      <text bytes="20" xml:space="preserve">This is page 2.</text>
    </revision>
  </page>
  <page>
    <title>Talk:Page 1</title>
    <ns>1</ns>
    <id>3</id>
    <revision>
      <id>3</id>
      <text bytes="20" xml:space="preserve">Discussion here.</text>
    </revision>
  </page>
</mediawiki>"""
    
    with open(staged_dir / "export.xml", "w") as f:
        f.write(xml_content)
        
    return staged_dir

def test_process_mediawiki_export_basic(sample_export_dir, temp_dedup_store, mock_qdrant, monkeypatch):
    store, store_path = temp_dedup_store
    
    # We need to monkeypatch DeduplicationStore constructor to return our temp one
    def mock_store_init(self, catalog_path=None):
        self._path = store_path
        self._lock = store._lock
        if not self._path.exists():
            self._write_raw({"records": []})
    
    monkeypatch.setattr(DeduplicationStore, "__init__", mock_store_init)
    
    # Run processor
    job_manager = JobManager()
    job = job_manager.create_job(source=str(sample_export_dir), job_type="v2_mediawiki")
    
    process_mediawiki_export(
        staged_dir=str(sample_export_dir),
        user_metadata={"project": "test"},
        kb_id="kb_test",
        cancel_check=lambda: False,
        job_id=job.id,
    )
    
    job_status = job_manager.get_job(job.id)
    assert job_status.status == "completed"
    
    # Check that chunks were created (2 pages in ns=0, Talk page skipped)
    assert len(mock_qdrant["chunks"]) > 0
    doc_ids = set(c.metadata.doc_id for c in mock_qdrant["chunks"])
    assert len(doc_ids) == 2 # 2 separate documents created
    
    # Check deduplication store
    records = store._read_raw()["records"]
    assert len(records) == 2
    for r in records:
        assert r["kb_id"] == "kb_test"
        assert r["user_metadata"] == {"project": "test"}
        assert r["ingestion_status"] == "completed"

def test_process_mediawiki_export_deduplication(sample_export_dir, temp_dedup_store, mock_qdrant, monkeypatch):
    store, store_path = temp_dedup_store
    
    def mock_store_init(self, catalog_path=None):
        self._path = store_path
        self._lock = store._lock
        if not self._path.exists():
            self._write_raw({"records": []})
    
    monkeypatch.setattr(DeduplicationStore, "__init__", mock_store_init)
    
    job_manager = JobManager()
    job1 = job_manager.create_job(source=str(sample_export_dir), job_type="v2_mediawiki")
    
    # First run
    process_mediawiki_export(
        staged_dir=str(sample_export_dir),
        user_metadata={"run": "1"},
        kb_id="kb_test",
        cancel_check=lambda: False,
        job_id=job1.id,
    )
    
    # Second run with different metadata
    job2 = job_manager.create_job(source=str(sample_export_dir), job_type="v2_mediawiki")
    process_mediawiki_export(
        staged_dir=str(sample_export_dir),
        user_metadata={"run": "2", "new_tag": "true"},
        kb_id="kb_test",
        cancel_check=lambda: False,
        job_id=job2.id,
    )
    
    # Records should still be 2 (deduplicated)
    records = store._read_raw()["records"]
    assert len(records) == 2
    for r in records:
        # Metadata should be merged (run=2 overwrites run=1)
        assert r["user_metadata"] == {"run": "2", "new_tag": "true"}
        
    # Updates should have been sent to Qdrant payload
    assert len(mock_qdrant["updates"]) == 2
