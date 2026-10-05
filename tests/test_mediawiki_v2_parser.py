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
import shutil
from pathlib import Path
from typing import Dict, List, Any
import json
import os

from retriva.ingestion.mediawiki_v2_parser import process_mediawiki_export
from retriva.ingestion.dedup import DeduplicationStore
from retriva.ingestion_api.execution import JobStatus


class InMemoryRecorder:
    """Test double for the durable recorder surface (Spec 025). The
    legacy in-memory JobManager was retired by Spec 027, so parser
    tests use this minimal recorder implementing the same
    start/advance/set/complete/cancel/fail/get/cancel-check surface."""

    def __init__(self):
        self._jobs = {}

    def start_job(self, job_id):
        self._jobs.setdefault(job_id, {"status": "running", "current_stage": None,
                                       "stages_completed": [], "error": None})

    def advance_stage(self, job_id, stage):
        job = self._jobs.setdefault(job_id, {"status": "running", "current_stage": None,
                                             "stages_completed": [], "error": None})
        if job["current_stage"] and job["current_stage"] not in job["stages_completed"]:
            job["stages_completed"].append(job["current_stage"])
        job["current_stage"] = stage

    def set_stage_detail(self, job_id, detail, progress=None):
        pass

    def complete_job(self, job_id):
        job = self._jobs.setdefault(job_id, {"status": "running", "current_stage": None,
                                             "stages_completed": [], "error": None})
        job["status"] = JobStatus.COMPLETED
        if job["current_stage"] and job["current_stage"] not in job["stages_completed"]:
            job["stages_completed"].append(job["current_stage"])
        job["current_stage"] = None

    def mark_cancelled(self, job_id):
        self._jobs.setdefault(job_id, {"status": "cancelled", "current_stage": None,
                                       "stages_completed": [], "error": None})

    def fail_job(self, job_id, error):
        job = self._jobs.setdefault(job_id, {"status": "running", "current_stage": None,
                                             "stages_completed": [], "error": None})
        job["status"] = JobStatus.FAILED
        job["error"] = error

    def get_job(self, job_id):
        from types import SimpleNamespace
        job = self._jobs.get(job_id)
        if job is None:
            return None
        return SimpleNamespace(
            id=job_id, status=job["status"], current_stage=job["current_stage"],
            stages_completed=list(job["stages_completed"]), error=job["error"])

    def is_cancel_requested(self, job_id):
        return False
from retriva.domain.models import DocRecord

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
    job_id = "test-mediawiki-job-1"
    recorder = InMemoryRecorder()

    process_mediawiki_export(
        staged_dir=str(sample_export_dir),
        user_metadata={"project": "test"},
        kb_id="kb_test",
        cancel_check=lambda: False,
        job_id=job_id,
        recorder=recorder,
    )

    job_status = recorder.get_job(job_id)
    assert job_status.status == JobStatus.COMPLETED
    
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
    
    job_id1 = "test-mediawiki-job-1"
    job_id2 = "test-mediawiki-job-2"

    # First run
    process_mediawiki_export(
        staged_dir=str(sample_export_dir),
        user_metadata={"run": "1"},
        kb_id="kb_test",
        cancel_check=lambda: False,
        job_id=job_id1,
        recorder=InMemoryRecorder(),
    )

    # Second run with different metadata
    process_mediawiki_export(
        staged_dir=str(sample_export_dir),
        user_metadata={"run": "2", "new_tag": "true"},
        kb_id="kb_test",
        cancel_check=lambda: False,
        job_id=job_id2,
        recorder=InMemoryRecorder(),
    )
    
    # Records should still be 2 (deduplicated)
    records = store._read_raw()["records"]
    assert len(records) == 2
    for r in records:
        # Metadata should be merged (run=2 overwrites run=1)
        assert r["user_metadata"] == {"run": "2", "new_tag": "true"}
        
    # Updates should have been sent to Qdrant payload
    assert len(mock_qdrant["updates"]) == 2
