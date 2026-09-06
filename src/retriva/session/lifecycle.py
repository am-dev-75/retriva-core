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
Expiration sweeper for session attachments and artifacts.

Marks expired records and deletes their backing files.  Intended to be
run periodically (e.g. via a FastAPI BackgroundTasks tick on startup and
on a timer).  Failures are logged and never raise — expiration is
best-effort and must not destabilize the API.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from retriva.config import settings
from retriva.logger import get_logger
from retriva.session.artifacts import SessionArtifactService
from retriva.session.attachments import AttachmentService
from retriva.session.models import AttachmentStatus, ArtifactStatus
from retriva.session.store import SessionStore

logger = get_logger(__name__)


def sweep_expired() -> dict:
    """Delete expired attachments and artifacts. Returns a summary dict.

    Safe to call repeatedly.  Idempotent.
    """
    store = SessionStore()
    now_iso = datetime.now(timezone.utc).isoformat()
    summary = {"attachments_deleted": 0, "artifacts_deleted": 0, "errors": 0}

    # Attachments
    try:
        expired = store.list_expired_attachments(now_iso)
        att_service = AttachmentService(store=store)
        for rec in expired:
            try:
                path = att_service.file_path(rec)
                if path.exists():
                    path.unlink()
                store.update_attachment(
                    rec.attachment_id, rec.session_id,
                    status=AttachmentStatus.EXPIRED,
                )
                store.delete_attachment(rec.attachment_id, rec.session_id)
                summary["attachments_deleted"] += 1
            except Exception as e:
                summary["errors"] += 1
                logger.warning(
                    f"Failed to expire attachment {rec.attachment_id}: {e}"
                )
    except Exception as e:
        summary["errors"] += 1
        logger.error(f"Attachment expiration sweep failed: {e}")

    # Artifacts
    try:
        expired = store.list_expired_artifacts(now_iso)
        art_service = SessionArtifactService(store=store)
        for rec in expired:
            try:
                path = art_service.file_path(rec)
                if path.exists():
                    path.unlink()
                store.update_artifact(
                    rec.artifact_id, rec.session_id,
                    status=ArtifactStatus.EXPIRED,
                )
                store.delete_artifact(rec.artifact_id, rec.session_id)
                summary["artifacts_deleted"] += 1
            except Exception as e:
                summary["errors"] += 1
                logger.warning(
                    f"Failed to expire artifact {rec.artifact_id}: {e}"
                )
    except Exception as e:
        summary["errors"] += 1
        logger.error(f"Artifact expiration sweep failed: {e}")

    if summary["attachments_deleted"] or summary["artifacts_deleted"]:
        logger.info(
            f"Session expiration sweep: "
            f"attachments={summary['attachments_deleted']} "
            f"artifacts={summary['artifacts_deleted']} "
            f"errors={summary['errors']}"
        )
    return summary
