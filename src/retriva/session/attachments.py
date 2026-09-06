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
AttachmentService — session-scoped attachment upload, validation,
storage, and parse-without-ingest.

Reuses the existing Retriva parser registry (``parser:docling``,
``parser:default``) to produce a :class:`ParsedAttachment` **without**
running the INDEXING stage.  Attachments are never ingested into the
persistent knowledge base (Qdrant) or GraphRAG storage.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from retriva.config import settings
from retriva.logger import get_logger
from retriva.registry import CapabilityRegistry
from retriva.session.malware import MalwareScanner
from retriva.session.models import (
    AttachmentRecord,
    AttachmentStatus,
    ParsedAttachment,
    ParsedElement,
)
from retriva.session.store import SessionStore

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Validation configuration
# ---------------------------------------------------------------------------

# Allowed MIME types for session attachments. Mirrors the formats Retriva
# can already parse for normal ingestion.
_ALLOWED_MIME: dict[str, str] = {
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/html": ".html",
    "application/pdf": ".pdf",
    # Office formats (parsed by Docling)
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    # Tabular
    "text/csv": ".csv",
    "application/csv": ".csv",
    # Fallback
    "application/octet-stream": "",
}

# Extensions considered dangerous regardless of MIME claim.
_BLOCKED_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".sh", ".ps1", ".vbs", ".js", ".jar",
    ".msi", ".dll", ".so", ".dylib", ".app", ".com", ".scr", ".wsf",
    ".lnk", ".hta", ".cpl", ".inf", ".reg", ".msp", ".mst",
}

# Macro-enabled / executable content
_BLOCKED_MACRO_EXTENSIONS = {
    ".docm", ".xlsm", ".pptm", ".xlsb", ".xla", ".xlam",
}

# Compressed-file expansion limit (bytes) — we do not auto-expand archives
# in v1; we reject them to avoid zip-bombs. Documented limitation.
_BLOCKED_ARCHIVE_EXTENSIONS = {
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".ar",
}

_RE_PATH_TRAVERSAL = re.compile(r"(^|/)\.\.($|/)")


class AttachmentValidationError(ValueError):
    """Raised when an attachment fails validation."""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class AttachmentService:
    """Session-scoped attachment handling.

    Responsibilities:
    - validate filename, size, MIME, extension consistency, parser availability;
    - malware-scan (via registered ``malware_scanner``);
    - store bytes under a session-scoped directory;
    - parse via the existing parser registry WITHOUT ingestion;
    - track expiration.
    """

    def __init__(self, store: Optional[SessionStore] = None) -> None:
        self._store = store or SessionStore()
        self._base_dir = self._resolve_base_dir()
        self._base_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- storage

    @staticmethod
    def _resolve_base_dir() -> Path:
        base = Path(settings.storage_path) / "sessions"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _session_dir(self, session_id: str) -> Path:
        safe_sid = self._sanitize_component(session_id)
        d = self._base_dir / safe_sid
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _sanitize_component(name: str) -> str:
        """Sanitize a single path component to prevent traversal."""
        if not name or not isinstance(name, str):
            raise AttachmentValidationError("Invalid session/attachment id")
        # Strip path separators and traversal sequences.
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        if _RE_PATH_TRAVERSAL.search(cleaned) or cleaned in (".", ".."):
            raise AttachmentValidationError(f"Unsafe path component: {name!r}")
        return cleaned

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    # ---------------------------------------------------------- validation

    def validate(
        self,
        *,
        filename: str,
        media_type: str,
        size: int,
        content_path: Path,
    ) -> Tuple[str, str]:
        """Validate an upload and return ``(selected_parser, normalized_mime)``.

        Raises :class:`AttachmentValidationError` on any failure.
        """
        # Filename safety
        if not filename or not isinstance(filename, str):
            raise AttachmentValidationError("filename is required")
        if _RE_PATH_TRAVERSAL.search(filename) or "\x00" in filename:
            raise AttachmentValidationError(f"Unsafe filename: {filename!r}")
        base = os.path.basename(filename)
        if base != filename:
            raise AttachmentValidationError(
                f"filename must not contain path separators: {filename!r}"
            )

        ext = Path(filename).suffix.lower()

        # Blocked extensions
        if ext in _BLOCKED_EXTENSIONS:
            raise AttachmentValidationError(
                f"Blocked executable extension: {ext}"
            )
        if ext in _BLOCKED_MACRO_EXTENSIONS:
            raise AttachmentValidationError(
                f"Macro-enabled documents are not allowed: {ext}"
            )
        if ext in _BLOCKED_ARCHIVE_EXTENSIONS:
            raise AttachmentValidationError(
                f"Compressed archives are not supported in v1: {ext}"
            )

        # Size limit
        max_size = getattr(settings, "session_attachment_max_size", 50 * 1024 * 1024)
        if size <= 0:
            raise AttachmentValidationError("size must be positive")
        if size > max_size:
            raise AttachmentValidationError(
                f"size {size} exceeds maximum {max_size}"
            )

        # MIME / extension consistency
        normalized_mime = media_type or "application/octet-stream"
        expected_ext = _ALLOWED_MIME.get(normalized_mime)
        if expected_ext is not None and ext and expected_ext and ext != expected_ext:
            # Allow common aliases (e.g. .markdown for .md)
            if not self._ext_alias_ok(ext, expected_ext):
                raise AttachmentValidationError(
                    f"extension {ext} does not match MIME {normalized_mime}"
                )

        # Parser availability — pick the parser that can handle this MIME.
        selected_parser = self._select_parser(normalized_mime, ext)
        if selected_parser is None:
            raise AttachmentValidationError(
                f"No parser available for MIME {normalized_mime} (ext {ext})"
            )

        # Password-protected / malformed detection is delegated to the parser
        # at parse time; here we only enforce cheap, pre-parse checks.

        return selected_parser, normalized_mime

    @staticmethod
    def _ext_alias_ok(ext: str, expected: str) -> bool:
        aliases = {
            ".md": {".markdown"},
            ".markdown": {".md"},
            ".htm": {".html"},
            ".html": {".htm"},
            ".text": {".txt"},
            ".txt": {".text"},
        }
        return ext in aliases.get(expected, set())

    @staticmethod
    def _select_parser(media_type: str, ext: str) -> Optional[str]:
        """Return the capability name of a parser that can handle the MIME.

        v1 uses the Docling parser (``parser:docling``) for everything Docling
        supports, and falls back to ``parser:default`` for plain text/HTML/MD.
        """
        registry = CapabilityRegistry()
        # Docling handles PDF/DOCX/XLSX/PPTX/HTML/MD/CSV/images.
        docling_types = {
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.oasis.opendocument.text",
            "application/vnd.oasis.opendocument.spreadsheet",
            "application/vnd.oasis.opendocument.presentation",
            "text/csv",
            "application/csv",
            "text/html",
            "text/markdown",
        }
        try:
            if media_type in docling_types:
                registry.get("parser:docling")
                return "parser:docling"
        except KeyError:
            pass
        try:
            registry.get("parser:default")
            return "parser:default"
        except KeyError:
            return None

    # ------------------------------------------------------------- malware

    def _get_scanner(self) -> MalwareScanner:
        registry = CapabilityRegistry()
        try:
            return registry.get_instance("malware_scanner")
        except KeyError:
            from retriva.session.malware import NoopMalwareScanner
            return NoopMalwareScanner()

    # -------------------------------------------------------------- upload

    def upload(
        self,
        *,
        session_id: str,
        filename: str,
        media_type: str,
        content: bytes,
        owner: Optional[str] = None,
    ) -> AttachmentRecord:
        """Validate, scan, and store an uploaded attachment.

        Returns the persisted :class:`AttachmentRecord`.  The attachment is
        NOT parsed yet; call :meth:`parse` to obtain a :class:`ParsedAttachment`.
        """
        size = len(content)
        # Write to a temp file first for validation + scanning.
        session_dir = self._session_dir(session_id)
        tmp_path = session_dir / f".upload_{uuid.uuid4().hex}"
        try:
            tmp_path.write_bytes(content)
            selected_parser, normalized_mime = self.validate(
                filename=filename,
                media_type=media_type,
                size=size,
                content_path=tmp_path,
            )
            # Malware scan
            scanner = self._get_scanner()
            scan = scanner.scan(str(tmp_path))
            if not scan.clean:
                raise AttachmentValidationError(
                    f"Malware scan failed: {scan.signature or scan.message}"
                )

            # Persist under a stable, sanitized name.
            attachment_id = uuid.uuid4().hex
            safe_name = self._sanitize_component(filename)
            stored_path = session_dir / f"{attachment_id}_{safe_name}"
            os.replace(tmp_path, stored_path)
            checksum = self._sha256(stored_path)
            storage_ref = str(stored_path.relative_to(self._base_dir))

            ttl = getattr(settings, "session_attachment_ttl_seconds", 24 * 3600)
            now = datetime.now(timezone.utc)
            record = AttachmentRecord(
                attachment_id=attachment_id,
                session_id=session_id,
                original_filename=filename,
                media_type=normalized_mime,
                size=size,
                checksum=checksum,
                expiration_time=(now + timedelta(seconds=ttl)).isoformat(),
                status=AttachmentStatus.UPLOADED,
                selected_parser=selected_parser,
                owner=owner,
                storage_ref=storage_ref,
            )
            self._store.put_attachment(record)
            logger.info(
                f"Attachment uploaded: id={attachment_id} session={session_id} "
                f"file={filename!r} size={size} parser={selected_parser} "
                f"scanner={scan.scanner}(safe={scanner.is_production_safe()})"
            )
            return record
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    # --------------------------------------------------------------- get

    def get(self, attachment_id: str, session_id: str) -> Optional[AttachmentRecord]:
        return self._store.get_attachment(attachment_id, session_id)

    def list(self, session_id: str) -> List[AttachmentRecord]:
        return self._store.list_attachments(session_id)

    def file_path(self, record: AttachmentRecord) -> Path:
        """Return the absolute path to the stored file (internal use only)."""
        return self._base_dir / record.storage_ref

    # --------------------------------------------------------------- parse

    def parse(
        self, attachment_id: str, session_id: str
    ) -> ParsedAttachment:
        """Parse a stored attachment WITHOUT ingesting it.

        Reuses the existing parser registry.  The parsed representation is
        returned in-memory; it is not persisted to the KB, Qdrant, or graph.
        """
        record = self._store.get_attachment(attachment_id, session_id)
        if record is None:
            raise AttachmentValidationError(
                f"Attachment not found: {attachment_id} (session {session_id})"
            )
        if record.status == AttachmentStatus.DELETED:
            raise AttachmentValidationError("Attachment has been deleted")
        if record.status == AttachmentStatus.PARSED:
            # Re-parsing is allowed (idempotent); just re-run the parser.
            pass

        self._store.update_attachment(
            attachment_id, session_id, status=AttachmentStatus.PARSING
        )
        path = self.file_path(record)
        if not path.exists():
            self._store.update_attachment(
                attachment_id, session_id,
                status=AttachmentStatus.FAILED,
                failure_info="stored file missing",
            )
            raise AttachmentValidationError("Stored file missing")

        try:
            registry = CapabilityRegistry()
            parser_cls = registry.get(record.selected_parser or "parser:default")
            parser = parser_cls()
            # v2 parsers return List[CanonicalRecord].
            records = parser.parse(
                source=str(path),
                content_type=record.media_type,
            )
            elements = [self._to_element(r) for r in records]
            warnings: List[str] = []
            if not elements:
                warnings.append("parser produced no elements")
            # Detect password-protected / malformed PDFs heuristically.
            if record.media_type == "application/pdf" and not elements:
                warnings.append(
                    "PDF yielded no text — it may be password-protected, "
                    "scanned without OCR, or malformed"
                )

            parsed = ParsedAttachment(
                attachment_id=attachment_id,
                session_id=session_id,
                media_type=record.media_type,
                elements=elements,
                warnings=warnings,
                parser_name=record.selected_parser or "parser:default",
            )
            self._store.update_attachment(
                attachment_id, session_id,
                status=AttachmentStatus.PARSED,
                parsed_element_count=len(elements),
                parse_warnings=warnings,
            )
            logger.info(
                f"Attachment parsed: id={attachment_id} elements={len(elements)}"
            )
            return parsed
        except Exception as e:
            logger.error(f"Attachment parse failed: id={attachment_id} err={e}")
            self._store.update_attachment(
                attachment_id, session_id,
                status=AttachmentStatus.FAILED,
                failure_info=str(e),
            )
            raise

    @staticmethod
    def _to_element(rec) -> ParsedElement:
        """Convert a CanonicalRecord to a ParsedElement."""
        # Spreadsheet location is not natively on CanonicalRecord; Docling
        # tables carry row/col implicitly in the markdown. We expose what the
        # canonical record provides and leave sheet/row/column for the
        # extension to infer from table_markdown when needed.
        return ParsedElement(
            element_type=rec.element_type,
            text=rec.text or "",
            page=rec.page,
            heading_path=list(rec.heading_path or []),
            table_markdown=rec.table_markdown,
            table_html=rec.table_html,
            source_uri=rec.source_uri,
            parser_name=rec.parser_name,
            confidence=rec.confidence,
        )

    # ------------------------------------------------------------- delete

    def delete(self, attachment_id: str, session_id: str) -> bool:
        record = self._store.get_attachment(attachment_id, session_id)
        if record is None:
            return False
        path = self.file_path(record)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        self._store.update_attachment(
            attachment_id, session_id, status=AttachmentStatus.DELETED
        )
        return self._store.delete_attachment(attachment_id, session_id)
