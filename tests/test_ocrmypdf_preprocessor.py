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
Unit tests for the OCRmyPDF preprocessor.

All tests mock the ocrmypdf module — no OCR engine required.
"""

import pytest
from unittest.mock import patch, MagicMock

from retriva.ingestion.ocrmypdf_preprocessor import OCRmyPDFPreprocessor
from retriva.ingestion.tika_client import TikaDetectionResult


class TestNeedsOCR:
    """Tests for OCRmyPDFPreprocessor.needs_ocr()."""

    def test_scanned_pdf(self):
        preprocessor = OCRmyPDFPreprocessor()
        detection = TikaDetectionResult(
            content_type="application/pdf",
            is_scanned=True,
        )
        assert preprocessor.needs_ocr(detection) is True

    def test_text_pdf(self):
        preprocessor = OCRmyPDFPreprocessor()
        detection = TikaDetectionResult(
            content_type="application/pdf",
            is_scanned=False,
        )
        assert preprocessor.needs_ocr(detection) is False

    def test_non_pdf(self):
        preprocessor = OCRmyPDFPreprocessor()
        detection = TikaDetectionResult(
            content_type="text/html",
            is_scanned=False,
        )
        assert preprocessor.needs_ocr(detection) is False

    def test_disabled(self):
        preprocessor = OCRmyPDFPreprocessor()
        preprocessor.enabled = False
        detection = TikaDetectionResult(
            content_type="application/pdf",
            is_scanned=True,
        )
        assert preprocessor.needs_ocr(detection) is False

    def test_low_quality_text_layer_triggers_ocr(self):
        """A present-but-garbled text layer should trigger re-OCR when
        force_ocr is enabled."""
        preprocessor = OCRmyPDFPreprocessor()
        preprocessor.force_ocr = True
        detection = TikaDetectionResult(
            content_type="application/pdf",
            is_scanned=False,
            low_text_quality=True,
        )
        assert preprocessor.needs_ocr(detection) is True

    def test_low_quality_without_force_ocr_does_not_trigger(self):
        """Without force_ocr, OCRmyPDF would skip pages that already have text,
        so re-OCR of a garbled layer is not attempted."""
        preprocessor = OCRmyPDFPreprocessor()
        preprocessor.force_ocr = False
        detection = TikaDetectionResult(
            content_type="application/pdf",
            is_scanned=False,
            low_text_quality=True,
        )
        assert preprocessor.needs_ocr(detection) is False


class TestPreprocess:
    """Tests for OCRmyPDFPreprocessor.preprocess()."""

    def test_preprocess_calls_ocrmypdf(self, tmp_path):
        input_file = tmp_path / "input.pdf"
        input_file.write_bytes(b"%PDF-1.4 fake")
        output_file = tmp_path / "output.pdf"

        mock_ocrmypdf = MagicMock()

        # Simulate ocrmypdf writing an output file. The preprocessor calls
        # ``ocrmypdf.ocr(input_path, output_path, ...)`` with the two paths
        # passed positionally, so accept *args here.
        def fake_ocr(*args, **kwargs):
            out = args[1] if len(args) > 1 else kwargs.get("output_file", "")
            with open(out, "wb") as f:
                f.write(b"%PDF-1.4 ocr'd content")

        mock_ocrmypdf.ocr = MagicMock(side_effect=fake_ocr)

        preprocessor = OCRmyPDFPreprocessor(language="eng")

        with patch.dict("sys.modules", {"ocrmypdf": mock_ocrmypdf}):
            result = preprocessor.preprocess(str(input_file), str(output_file))

        assert result is True
        mock_ocrmypdf.ocr.assert_called_once()

    def test_preprocess_missing_input(self, tmp_path):
        preprocessor = OCRmyPDFPreprocessor()
        result = preprocessor.preprocess(
            str(tmp_path / "nonexistent.pdf"),
            str(tmp_path / "output.pdf"),
        )
        assert result is False

    def test_preprocess_cancelled(self, tmp_path):
        input_file = tmp_path / "input.pdf"
        input_file.write_bytes(b"content")

        preprocessor = OCRmyPDFPreprocessor()
        result = preprocessor.preprocess(
            str(input_file),
            str(tmp_path / "output.pdf"),
            cancel_check=lambda: True,  # cancelled immediately
        )
        assert result is False
