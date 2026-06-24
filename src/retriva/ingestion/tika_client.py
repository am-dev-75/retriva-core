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
Tika REST client for the v2 ingestion pipeline.

Handles MIME detection, metadata extraction, and fallback text
extraction via an Apache Tika server (typically a Docker sidecar).
"""

import re
from dataclasses import dataclass, field
from typing import Dict, Optional

import requests

from retriva.config import settings
from retriva.logger import get_logger

logger = get_logger(__name__)

# Token shape used by the text-quality heuristic: a "word" is a run of letters
# (optionally with an internal apostrophe/hyphen).
_RE_WORD = re.compile(r"[A-Za-z\u00C0-\u024F]+(?:['\-][A-Za-z\u00C0-\u024F]+)*")
_RE_VOWEL = re.compile(r"[aeiouyAEIOUY\u00C0-\u024F]")


def score_text_quality(text: str, sample_chars: int = 4000) -> float:
    """Return a 0..1 score estimating how "word-like" *text* is.

    A garbled/corrupt text layer (e.g. ``edOtLo``, ``PCATTL``, ``ANWARNING``)
    yields a low score; clean prose yields a high score. The score is the
    fraction of sampled word tokens that look like plausible words, judged by
    cheap language-agnostic shape checks (contains a vowel, not absurdly long,
    reasonable consonant runs). No dictionary or network call is involved.

    Returns ``1.0`` for text with no analysable word tokens so that callers
    never re-OCR purely on the basis of "no words found" (that case is handled
    by the scanned-PDF heuristic instead).
    """
    if not text:
        return 1.0
    sample = text[:sample_chars]
    words = _RE_WORD.findall(sample)
    # Only consider tokens of length >= 2; single letters are usually fine
    # (articles, initials) and add noise to the ratio.
    words = [w for w in words if len(w) >= 2]
    if len(words) < 5:
        return 1.0

    plausible = 0
    for w in words:
        lw = w.lower()
        # 1. Must contain a vowel — vowel-less runs like "pcattl" are garble.
        if not _RE_VOWEL.search(lw):
            continue
        # 2. No run of 4+ consecutive consonants (e.g. "ndlkr").
        if re.search(r"[bcdfghjklmnpqrstvwxz]{4,}", lw):
            continue
        # 3. Reasonable length — extremely long tokens are usually merged garble.
        if len(lw) > 24:
            continue
        plausible += 1

    return plausible / len(words)

# Metadata keys that hint at a scanned (image-only) PDF
_TEXT_INDICATOR_KEYS = {
    "pdf:charsPerPage",
    "pdf:totalChars",
    "xmpTPg:NPages",
}


@dataclass
class TikaDetectionResult:
    """Output of Tika MIME detection + metadata extraction."""

    content_type: str = "application/octet-stream"
    metadata: Dict[str, str] = field(default_factory=dict)
    language: Optional[str] = None
    is_scanned: bool = False
    # True when the PDF has an embedded text layer that is present but garbled
    # (low quality), so it should be re-OCR'd even though ``is_scanned`` is
    # False. Computed by the text-quality heuristic in ``detect()``.
    low_text_quality: bool = False
    # The computed quality score (0..1), exposed for logging/diagnostics.
    text_quality_score: float = 1.0


class TikaClient:
    """Client for the Apache Tika REST API.

    Connects to a Tika server at ``settings.tika_server_url`` and
    provides MIME detection, metadata extraction, and fallback text
    extraction.
    """

    def __init__(self, server_url: Optional[str] = None):
        self.server_url = (server_url or settings.tika_server_url).rstrip("/")
        self._timeout = 60  # seconds

    # -- Public API --------------------------------------------------------

    def health_check(self) -> bool:
        """Return ``True`` if the Tika server is reachable."""
        try:
            r = requests.get(
                f"{self.server_url}/tika",
                timeout=5,
            )
            return r.status_code == 200
        except Exception:
            return False

    def detect_mime(self, file_path: str) -> str:
        """Detect MIME type via ``PUT /detect/stream``.

        Args:
            file_path: Local path to the document.

        Returns:
            MIME type string (e.g. ``"application/pdf"``).
        """
        try:
            with open(file_path, "rb") as f:
                r = requests.put(
                    f"{self.server_url}/detect/stream",
                    data=f,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=self._timeout,
                )
            r.raise_for_status()
            mime = r.text.strip()
            logger.debug(f"Tika detected MIME: {mime} for '{file_path}'")
            return mime
        except Exception as e:
            logger.error(f"Tika MIME detection failed for '{file_path}': {e}")
            return "application/octet-stream"

    def extract_metadata(self, file_path: str) -> Dict[str, str]:
        """Extract document metadata via ``PUT /meta``.

        Args:
            file_path: Local path to the document.

        Returns:
            Dictionary of metadata key/value pairs.
        """
        try:
            with open(file_path, "rb") as f:
                r = requests.put(
                    f"{self.server_url}/meta",
                    data=f,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Accept": "application/json",
                    },
                    timeout=self._timeout,
                )
            r.raise_for_status()
            raw = r.json()
            # Tika may return a list (for multi-document files) or a dict
            if isinstance(raw, list) and raw:
                raw = raw[0]
            metadata = {k: str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
            logger.debug(f"Tika extracted {len(metadata)} metadata fields from '{file_path}'")
            return metadata
        except Exception as e:
            logger.error(f"Tika metadata extraction failed for '{file_path}': {e}")
            return {}

    def extract_text(self, file_path: str) -> str:
        """Extract plain text via ``PUT /tika`` (fallback parser).

        Args:
            file_path: Local path to the document.

        Returns:
            Extracted text content.
        """
        try:
            with open(file_path, "rb") as f:
                r = requests.put(
                    f"{self.server_url}/tika",
                    data=f,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Accept": "text/plain",
                    },
                    timeout=self._timeout,
                )
            r.raise_for_status()
            text = r.text.strip()
            logger.debug(f"Tika extracted {len(text)} chars from '{file_path}'")
            return text
        except Exception as e:
            logger.error(f"Tika text extraction failed for '{file_path}': {e}")
            return ""

    def detect(self, file_path: str) -> TikaDetectionResult:
        """Combined MIME + metadata + scanned-PDF heuristic.

        This is the primary entry point for the DETECTING stage.

        Args:
            file_path: Local path to the document.

        Returns:
            A ``TikaDetectionResult`` with content_type, metadata, and
            a heuristic ``is_scanned`` flag for PDFs.
        """
        content_type = self.detect_mime(file_path)
        metadata = self.extract_metadata(file_path)

        # Detect language from metadata if available
        language = metadata.get("dc:language") or metadata.get("language")

        # Heuristic for scanned PDFs: no chars-per-page metadata or very low
        is_scanned = False
        if content_type == "application/pdf":
            chars_per_page = metadata.get("pdf:charsPerPage", "")
            total_chars = metadata.get("pdf:totalChars", "")
            # If total chars is explicitly "0" or absent, likely scanned
            if total_chars in ("0", ""):
                is_scanned = True
            elif chars_per_page:
                # charsPerPage can be a comma-separated list of per-page counts
                try:
                    counts = [int(c.strip()) for c in chars_per_page.split(",") if c.strip()]
                    avg_chars = sum(counts) / len(counts) if counts else 0
                    if avg_chars < 10:  # Threshold: fewer than 10 chars/page = scanned
                        is_scanned = True
                except (ValueError, ZeroDivisionError):
                    pass

        if is_scanned:
            logger.info(f"Tika detected scanned PDF: '{file_path}'")

        # Text-quality heuristic: detect a present-but-garbled embedded text
        # layer that the scanned heuristic misses. Only meaningful for PDFs
        # that were NOT already flagged as scanned (those will be OCR'd anyway).
        low_text_quality = False
        text_quality_score = 1.0
        if (
            settings.ocr_redo_low_quality
            and content_type == "application/pdf"
            and not is_scanned
        ):
            sample_text = self.extract_text(file_path)
            if len(sample_text) >= settings.ocr_text_quality_min_chars:
                text_quality_score = score_text_quality(sample_text)
                if text_quality_score < settings.ocr_text_quality_threshold:
                    low_text_quality = True
                    logger.info(
                        f"Tika detected low-quality text layer "
                        f"(score={text_quality_score:.2f} < "
                        f"{settings.ocr_text_quality_threshold}): '{file_path}'"
                    )

        return TikaDetectionResult(
            content_type=content_type,
            metadata=metadata,
            language=language,
            is_scanned=is_scanned,
            low_text_quality=low_text_quality,
            text_quality_score=text_quality_score,
        )


# ---------------------------------------------------------------------------
# Register in CapabilityRegistry
# ---------------------------------------------------------------------------

from retriva.registry import CapabilityRegistry

CapabilityRegistry().register("tika_client", TikaClient, priority=100)
