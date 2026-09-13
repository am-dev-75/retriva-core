# Copyright (C) 2026 Andrea Marson (am-dev-75@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Session-scoped metadata store for attachments and artifacts.

SQLite-backed, thread-safe (single writer).  Stores only metadata; file
bytes live in the session storage directory managed by
:class:`AttachmentService` / :class:`SessionArtifactService`.

Tables:
- ``session_attachments``  — one row per uploaded attachment.
- ``session_artifacts``    — one row per generated output artifact.

Both are scoped by ``session_id``.  No cross-session access is possible
through this store's API.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from retriva.config import settings
from retriva.logger import get_logger
from retriva.session.models import (
    AttachmentRecord,
    AttachmentStatus,
    SessionArtifactRecord,
    ArtifactStatus,
)

logger = get_logger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_attachments (
    attachment_id     TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    media_type        TEXT NOT NULL,
    size              INTEGER NOT NULL,
    checksum          TEXT NOT NULL,
    upload_time       TEXT NOT NULL,
    expiration_time   TEXT NOT NULL,
    status            TEXT NOT NULL,
    selected_parser   TEXT,
    owner             TEXT,
    storage_ref       TEXT NOT NULL,
    failure_info      TEXT,
    parsed_element_count INTEGER NOT NULL DEFAULT 0,
    parse_warnings    TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_attachments_session ON session_attachments(session_id);
CREATE INDEX IF NOT EXISTS idx_attachments_expiration ON session_attachments(expiration_time);

CREATE TABLE IF NOT EXISTS session_artifacts (
    artifact_id        TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL,
    filename           TEXT NOT NULL,
    media_type         TEXT NOT NULL,
    size               INTEGER NOT NULL,
    created_at         TEXT NOT NULL,
    expiration_time    TEXT NOT NULL,
    status             TEXT NOT NULL,
    storage_ref        TEXT NOT NULL,
    source_attachment_id TEXT,
    artifact_kind      TEXT NOT NULL DEFAULT 'report',
    metadata           TEXT NOT NULL DEFAULT '{}',
    failure_info       TEXT
);
CREATE INDEX IF NOT EXISTS idx_artifacts_session ON session_artifacts(session_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_expiration ON session_artifacts(expiration_time);
"""


class SessionStore:
    """SQLite metadata store for session attachments and artifacts.

    Thread-safe via a write lock.  The database file lives under the
    configured storage path so it is shared across Core processes that
    mount the same volume (e.g. ingestion API + openai API in containers).
    """

    _instance: Optional["SessionStore"] = None
    _init_lock = threading.Lock()

    def __new__(cls) -> "SessionStore":
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._lock = threading.Lock()
        self._db_path = self._resolve_db_path()
        self._init_db()
        self._initialized = True
        logger.debug(f"SessionStore initialized at {self._db_path}")

    # ------------------------------------------------------------------ setup

    @staticmethod
    def _resolve_db_path() -> Path:
        base = Path(settings.storage_path)
        base.mkdir(parents=True, exist_ok=True)
        return base / "sessions.db"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.executescript(_SCHEMA)

    # ----------------------------------------------------------- attachments

    def put_attachment(self, record: AttachmentRecord) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO session_attachments
                    (attachment_id, session_id, original_filename, media_type, size,
                     checksum, upload_time, expiration_time, status, selected_parser,
                     owner, storage_ref, failure_info, parsed_element_count,
                     parse_warnings)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.attachment_id,
                        record.session_id,
                        record.original_filename,
                        record.media_type,
                        record.size,
                        record.checksum,
                        record.upload_time,
                        record.expiration_time,
                        record.status.value,
                        record.selected_parser,
                        record.owner,
                        record.storage_ref,
                        record.failure_info,
                        record.parsed_element_count,
                        json.dumps(record.parse_warnings),
                    ),
                )

    def get_attachment(
        self, attachment_id: str, session_id: str
    ) -> Optional[AttachmentRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM session_attachments "
                "WHERE attachment_id = ? AND session_id = ?",
                (attachment_id, session_id),
            ).fetchone()
        return self._row_to_attachment(row) if row else None

    def list_attachments(self, session_id: str) -> List[AttachmentRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM session_attachments WHERE session_id = ? "
                "ORDER BY upload_time ASC",
                (session_id,),
            ).fetchall()
        return [self._row_to_attachment(r) for r in rows]

    def update_attachment(
        self,
        attachment_id: str,
        session_id: str,
        *,
        status: Optional[AttachmentStatus] = None,
        selected_parser: Optional[str] = None,
        failure_info: Optional[str] = None,
        parsed_element_count: Optional[int] = None,
        parse_warnings: Optional[List[str]] = None,
    ) -> Optional[AttachmentRecord]:
        sets: List[str] = []
        vals: list = []
        if status is not None:
            sets.append("status = ?")
            vals.append(status.value)
        if selected_parser is not None:
            sets.append("selected_parser = ?")
            vals.append(selected_parser)
        if failure_info is not None:
            sets.append("failure_info = ?")
            vals.append(failure_info)
        if parsed_element_count is not None:
            sets.append("parsed_element_count = ?")
            vals.append(parsed_element_count)
        if parse_warnings is not None:
            sets.append("parse_warnings = ?")
            vals.append(json.dumps(parse_warnings))
        if not sets:
            return self.get_attachment(attachment_id, session_id)
        vals.extend([attachment_id, session_id])
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"UPDATE session_attachments SET {', '.join(sets)} "
                    "WHERE attachment_id = ? AND session_id = ?",
                    vals,
                )
        return self.get_attachment(attachment_id, session_id)

    def delete_attachment(self, attachment_id: str, session_id: str) -> bool:
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM session_attachments "
                    "WHERE attachment_id = ? AND session_id = ?",
                    (attachment_id, session_id),
                )
                return cur.rowcount > 0

    def list_expired_attachments(self, now_iso: str) -> List[AttachmentRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM session_attachments "
                "WHERE expiration_time <= ? AND status NOT IN (?, ?)",
                (now_iso, AttachmentStatus.EXPIRED.value, AttachmentStatus.DELETED.value),
            ).fetchall()
        return [self._row_to_attachment(r) for r in rows]

    @staticmethod
    def _row_to_attachment(row: sqlite3.Row) -> AttachmentRecord:
        return AttachmentRecord(
            attachment_id=row["attachment_id"],
            session_id=row["session_id"],
            original_filename=row["original_filename"],
            media_type=row["media_type"],
            size=row["size"],
            checksum=row["checksum"],
            upload_time=row["upload_time"],
            expiration_time=row["expiration_time"],
            status=AttachmentStatus(row["status"]),
            selected_parser=row["selected_parser"],
            owner=row["owner"],
            storage_ref=row["storage_ref"],
            failure_info=row["failure_info"],
            parsed_element_count=row["parsed_element_count"],
            parse_warnings=json.loads(row["parse_warnings"] or "[]"),
        )

    # ------------------------------------------------------------ artifacts

    def put_artifact(self, record: SessionArtifactRecord) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO session_artifacts
                    (artifact_id, session_id, filename, media_type, size,
                     created_at, expiration_time, status, storage_ref,
                     source_attachment_id, artifact_kind, metadata, failure_info)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.artifact_id,
                        record.session_id,
                        record.filename,
                        record.media_type,
                        record.size,
                        record.created_at,
                        record.expiration_time,
                        record.status.value,
                        record.storage_ref,
                        record.source_attachment_id,
                        record.artifact_kind,
                        json.dumps(record.metadata),
                        record.failure_info,
                    ),
                )

    def get_artifact(
        self, artifact_id: str, session_id: str
    ) -> Optional[SessionArtifactRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM session_artifacts "
                "WHERE artifact_id = ? AND session_id = ?",
                (artifact_id, session_id),
            ).fetchone()
        return self._row_to_artifact(row) if row else None

    def list_artifacts(self, session_id: str) -> List[SessionArtifactRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM session_artifacts WHERE session_id = ? "
                "ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
        return [self._row_to_artifact(r) for r in rows]

    def update_artifact(
        self,
        artifact_id: str,
        session_id: str,
        *,
        status: Optional[ArtifactStatus] = None,
        size: Optional[int] = None,
        failure_info: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Optional[SessionArtifactRecord]:
        sets: List[str] = []
        vals: list = []
        if status is not None:
            sets.append("status = ?")
            vals.append(status.value)
        if size is not None:
            sets.append("size = ?")
            vals.append(size)
        if failure_info is not None:
            sets.append("failure_info = ?")
            vals.append(failure_info)
        if metadata is not None:
            sets.append("metadata = ?")
            vals.append(json.dumps(metadata))
        if not sets:
            return self.get_artifact(artifact_id, session_id)
        vals.extend([artifact_id, session_id])
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"UPDATE session_artifacts SET {', '.join(sets)} "
                    "WHERE artifact_id = ? AND session_id = ?",
                    vals,
                )
        return self.get_artifact(artifact_id, session_id)

    def delete_artifact(self, artifact_id: str, session_id: str) -> bool:
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM session_artifacts "
                    "WHERE artifact_id = ? AND session_id = ?",
                    (artifact_id, session_id),
                )
                return cur.rowcount > 0

    def list_all_artifacts(
        self,
        *,
        include_expired: bool = False,
        limit: int = 500,
    ) -> List[SessionArtifactRecord]:
        """List artifacts across ALL sessions (most recent first).

        Powers the artifact index page.  Expired/deleted artifacts are
        excluded unless ``include_expired`` is set.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        query = "SELECT * FROM session_artifacts"
        if not include_expired:
            query += " WHERE status NOT IN (?, ?) AND expiration_time > ?"
        query += " ORDER BY created_at DESC LIMIT ?"
        with self._connect() as conn:
            if include_expired:
                rows = conn.execute(query, (limit,)).fetchall()
            else:
                rows = conn.execute(
                    query,
                    (
                        ArtifactStatus.EXPIRED.value,
                        ArtifactStatus.DELETED.value,
                        now_iso,
                        limit,
                    ),
                ).fetchall()
        return [self._row_to_artifact(r) for r in rows]

    def list_expired_artifacts(self, now_iso: str) -> List[SessionArtifactRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM session_artifacts "
                "WHERE expiration_time <= ? AND status NOT IN (?, ?)",
                (now_iso, ArtifactStatus.EXPIRED.value, ArtifactStatus.DELETED.value),
            ).fetchall()
        return [self._row_to_artifact(r) for r in rows]

    @staticmethod
    def _row_to_artifact(row: sqlite3.Row) -> SessionArtifactRecord:
        return SessionArtifactRecord(
            artifact_id=row["artifact_id"],
            session_id=row["session_id"],
            filename=row["filename"],
            media_type=row["media_type"],
            size=row["size"],
            created_at=row["created_at"],
            expiration_time=row["expiration_time"],
            status=ArtifactStatus(row["status"]),
            storage_ref=row["storage_ref"],
            source_attachment_id=row["source_attachment_id"],
            artifact_kind=row["artifact_kind"],
            metadata=json.loads(row["metadata"] or "{}"),
            failure_info=row["failure_info"],
        )

    # ------------------------------------------------------------- testing

    @classmethod
    def _reset(cls) -> None:
        """Reset the singleton — for testing only."""
        with cls._init_lock:
            cls._instance = None


def get_session_store() -> SessionStore:
    """Return the process-wide :class:`SessionStore` singleton."""
    return SessionStore()
