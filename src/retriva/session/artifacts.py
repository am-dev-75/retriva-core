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
SessionArtifactService — session-scoped generated output artifacts.

Stores downloadable result files scoped to a chat session.  Artifacts:
- belong to the same session as the source attachment;
- are generated under a new filename (never overwrite the input);
- expire after a configurable TTL and are then deleted;
- are inaccessible from other sessions.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from retriva.config import settings
from retriva.logger import get_logger
from retriva.session.models import ArtifactStatus, SessionArtifactRecord
from retriva.session.store import SessionStore

logger = get_logger(__name__)


# MIME types for common output formats
_EXT_TO_MIME = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".json": "application/json",
    ".html": "text/html",
}


class SessionArtifactService:
    """Session-scoped artifact storage and download."""

    def __init__(self, store: Optional[SessionStore] = None) -> None:
        self._store = store or SessionStore()
        self._base_dir = Path(settings.storage_path) / "sessions"
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _session_dir(self, session_id: str) -> Path:
        # Reuse the same sanitization as AttachmentService via the store's
        # session directory layout. Keep it simple and safe.
        import re
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)
        if not safe or safe in (".", ".."):
            raise ValueError(f"Invalid session id: {session_id!r}")
        d = self._base_dir / safe / "artifacts"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _mime_for(filename: str) -> str:
        ext = Path(filename).suffix.lower()
        return _EXT_TO_MIME.get(ext, "application/octet-stream")

    def create(
        self,
        *,
        session_id: str,
        filename: str,
        content: bytes,
        source_attachment_id: Optional[str] = None,
        artifact_kind: str = "report",
        metadata: Optional[dict] = None,
    ) -> SessionArtifactRecord:
        """Store a generated artifact and return its record.

        The filename is forced to a new, unique name to guarantee the input
        attachment is never overwritten.
        """
        artifact_id = uuid.uuid4().hex
        ext = Path(filename).suffix.lower()
        # New filename: <artifact_id>_<safe_original>
        import re
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
        new_filename = f"{artifact_id}_{safe_name}"
        session_dir = self._session_dir(session_id)
        path = session_dir / new_filename
        path.write_bytes(content)
        storage_ref = str(path.relative_to(self._base_dir))
        ttl = getattr(settings, "session_artifact_ttl_seconds", 48 * 3600)
        now = datetime.now(timezone.utc)
        record = SessionArtifactRecord(
            artifact_id=artifact_id,
            session_id=session_id,
            filename=new_filename,
            media_type=self._mime_for(new_filename),
            size=len(content),
            expiration_time=(now + timedelta(seconds=ttl)).isoformat(),
            status=ArtifactStatus.READY,
            storage_ref=storage_ref,
            source_attachment_id=source_attachment_id,
            artifact_kind=artifact_kind,
            metadata=metadata or {},
        )
        self._store.put_artifact(record)
        logger.info(
            f"Session artifact created: id={artifact_id} session={session_id} "
            f"file={new_filename!r} size={len(content)}"
        )
        return record

    def get(self, artifact_id: str, session_id: str) -> Optional[SessionArtifactRecord]:
        return self._store.get_artifact(artifact_id, session_id)

    def list(self, session_id: str) -> List[SessionArtifactRecord]:
        return self._store.list_artifacts(session_id)

    def file_path(self, record: SessionArtifactRecord) -> Path:
        return self._base_dir / record.storage_ref

    def read(self, artifact_id: str, session_id: str) -> Optional[bytes]:
        record = self.get(artifact_id, session_id)
        if record is None or record.status != ArtifactStatus.READY:
            return None
        path = self.file_path(record)
        if not path.exists():
            return None
        return path.read_bytes()

    def delete(self, artifact_id: str, session_id: str) -> bool:
        record = self._store.get_artifact(artifact_id, session_id)
        if record is None:
            return False
        path = self.file_path(record)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        self._store.update_artifact(
            artifact_id, session_id, status=ArtifactStatus.DELETED
        )
        return self._store.delete_artifact(artifact_id, session_id)
