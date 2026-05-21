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

"""
Content-hash deduplication for v2 ingestion.

Provides:
  - ContentHasher: compute SHA-256 over raw file bytes.
  - DeduplicationStore: JSON-file-based catalog keyed by (kb_id, content_hash, collection_name).

Deduplication key: (kb_id, content_hash, collection_name)
doc_id format:     "doc_" + sha256(kb_id + ":" + hex_digest)[:32]
"""

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from retriva.domain.models import DocRecord
from retriva.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

def compute_content_hash(file_bytes: bytes) -> str:
    """Return 'sha256:<hex>' for the given raw bytes."""
    hex_digest = hashlib.sha256(file_bytes).hexdigest()
    return f"sha256:{hex_digest}"


def derive_doc_id(kb_id: str, hex_digest: str) -> str:
    """Return a deterministic, per-KB doc_id from the content hash.

    Uses sha256(kb_id + ":" + hex_digest) so that the same file in
    different knowledge bases has different doc_ids.
    """
    combined = f"{kb_id}:{hex_digest}"
    scoped_hash = hashlib.sha256(combined.encode("utf-8")).hexdigest()
    return f"doc_{scoped_hash[:32]}"


# ---------------------------------------------------------------------------
# DeduplicationStore — JSON-file-based catalog
# ---------------------------------------------------------------------------

class DeduplicationStore:
    """Thread-safe, JSON-file-backed catalog of ingested documents.

    The catalog file is a JSON object with a single key ``"records"``, whose
    value is a list of serialised ``DocRecord`` dicts.

    Lookup is O(n) on record count; this is acceptable for catalogs of
    tens-of-thousands of documents. Replace with SQLite if needed later.
    """

    _lock = threading.Lock()

    def __init__(self, catalog_path: Optional[str] = None):
        if catalog_path is None:
            from retriva.config import settings
            storage_dir = getattr(settings, "storage_dir", "storage")
            catalog_path = os.path.join(storage_dir, "dedup_catalog.json")

        self._path = Path(catalog_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

        if not self._path.exists():
            self._write_raw({"records": []})

    # -- Internal I/O -------------------------------------------------------

    def _read_raw(self) -> Dict[str, Any]:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"records": []}

    def _write_raw(self, data: Dict[str, Any]) -> None:
        # Write to a .tmp file then rename for atomicity
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    # -- Public API ---------------------------------------------------------

    def get_by_hash(self, kb_id: str, content_hash: str, collection_name: str = "retriva_chunks") -> Optional[DocRecord]:
        """Look up a document by (kb_id, content_hash, collection_name). Returns None if not found."""
        with self._lock:
            data = self._read_raw()
            for rec in data.get("records", []):
                rec_collection = rec.get("collection_name", "retriva_chunks")
                if (rec.get("kb_id") == kb_id
                        and rec.get("content_hash") == content_hash
                        and rec_collection == collection_name):
                    return DocRecord(**rec)
        return None

    def get_by_doc_id(self, doc_id: str) -> Optional[DocRecord]:
        """Look up a document by its deterministic doc_id."""
        with self._lock:
            data = self._read_raw()
            for rec in data.get("records", []):
                if rec.get("doc_id") == doc_id:
                    return DocRecord(**rec)
        return None

    def create_record(self, record: DocRecord) -> None:
        """Persist a new DocRecord. Raises ValueError if a duplicate key exists."""
        with self._lock:
            data = self._read_raw()
            for rec in data.get("records", []):
                rec_collection = rec.get("collection_name", "retriva_chunks")
                if (rec.get("kb_id") == record.kb_id
                        and rec.get("content_hash") == record.content_hash
                        and rec_collection == record.collection_name):
                    raise ValueError(
                        f"DocRecord already exists: kb_id={record.kb_id}, "
                        f"content_hash={record.content_hash}, "
                        f"collection_name={record.collection_name}"
                    )
            data.setdefault("records", []).append(record.model_dump())
            self._write_raw(data)
        logger.debug(
            f"dedup_record_created: doc_id={record.doc_id}, kb_id={record.kb_id}, "
            f"content_hash={record.content_hash}"
        )

    def update_record(
        self,
        doc_id: str,
        merged_metadata: Optional[Dict[str, Any]],
        merged_source_paths: List[str],
        chunk_count: Optional[int] = None,
        ingestion_status: Optional[str] = None,
    ) -> DocRecord:
        """Update metadata, source_paths (and optionally chunk_count/status) for a record."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            data = self._read_raw()
            for rec in data["records"]:
                if rec.get("doc_id") == doc_id:
                    rec["user_metadata"] = merged_metadata
                    rec["source_paths"] = merged_source_paths
                    rec["updated_at"] = now
                    rec["metadata_updated_at"] = now
                    if chunk_count is not None:
                        rec["chunk_count"] = chunk_count
                    if ingestion_status is not None:
                        rec["ingestion_status"] = ingestion_status
                    self._write_raw(data)
                    return DocRecord(**rec)
        raise KeyError(f"DocRecord not found for doc_id={doc_id}")

    def finalize_record(self, doc_id: str, chunk_count: int) -> None:
        """Mark a record as completed after indexing."""
        with self._lock:
            data = self._read_raw()
            for rec in data["records"]:
                if rec.get("doc_id") == doc_id:
                    rec["chunk_count"] = chunk_count
                    rec["ingestion_status"] = "completed"
                    rec["updated_at"] = datetime.now(timezone.utc).isoformat()
                    self._write_raw(data)
                    return
        raise KeyError(f"DocRecord not found for doc_id={doc_id}")

    # -- KB cascade ---------------------------------------------------------

    def delete_by_kb_id(self, kb_id: str) -> int:
        """Remove every record whose ``kb_id`` matches.

        Returns the number of records removed. Idempotent — calling with a
        ``kb_id`` that has no matching records returns 0 and rewrites the
        catalog unchanged.

        Part of the KB cascade-on-delete (SDD Phase 3). Invoked *after* the
        Qdrant points have been deleted, so the dedup catalog never points
        at non-existent points.
        """
        with self._lock:
            data = self._read_raw()
            records = data.get("records", [])
            kept = [r for r in records if r.get("kb_id") != kb_id]
            removed = len(records) - len(kept)
            if removed:
                data["records"] = kept
                self._write_raw(data)
            return removed

    # -- Testing support ----------------------------------------------------

    def clear_all(self) -> None:
        """Reset catalog — for testing only."""
        with self._lock:
            self._write_raw({"records": []})


# ---------------------------------------------------------------------------
# Metadata merge helpers
# ---------------------------------------------------------------------------

def merge_metadata(
    existing: Optional[Dict[str, Any]],
    incoming: Optional[Dict[str, Any]],
    doc_id: str,
    kb_id: str,
) -> tuple[Optional[Dict[str, Any]], bool]:
    """Merge incoming metadata into existing using overwrite-on-conflict policy.

    Returns (merged_dict, changed: bool).
    """
    if not incoming:
        return existing, False

    merged = dict(existing or {})
    changed = False

    for key, new_val in incoming.items():
        old_val = merged.get(key)
        if old_val != new_val:
            if key in merged:
                logger.info(
                    f"duplicate_document_metadata_merged: doc_id={doc_id}, kb_id={kb_id}, "
                    f"key={key!r}, old_value={old_val!r}, new_value={new_val!r}, "
                    f"action=overwrite"
                )
            merged[key] = new_val
            changed = True

    return merged if merged else None, changed


def merge_source_paths(
    existing: List[str],
    new_path: str,
    doc_id: str,
    kb_id: str,
) -> tuple[List[str], bool]:
    """Add new_path to existing list if not already present.

    Returns (merged_list, changed: bool).
    """
    if new_path in existing:
        return existing, False

    merged = list(existing) + [new_path]
    logger.info(
        f"duplicate_document_source_paths_updated: doc_id={doc_id}, kb_id={kb_id}, "
        f"added_path={new_path!r}, total_paths={len(merged)}"
    )
    return merged, True
