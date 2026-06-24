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
OCRmyPDF preprocessor for the v2 ingestion pipeline.

Adds a text layer to scanned PDFs (deskew, rotate, OCR) so that
downstream structural parsers (Docling, Unstructured) can extract
text reliably.
"""

from pathlib import Path
from typing import Callable, Optional

from retriva.config import settings
from retriva.logger import get_logger

logger = get_logger(__name__)


class OCRmyPDFPreprocessor:
    """Preprocess scanned PDFs by adding a text layer via OCRmyPDF.

    Only activated when ``settings.ocrmypdf_enabled`` is ``True`` and
    the Tika detection heuristic reports the PDF as scanned
    (``is_scanned=True``).
    """

    def __init__(
        self,
        language: Optional[str] = None,
        deskew: Optional[bool] = None,
        rotate_pages: Optional[bool] = None,
    ):
        self.language = language if language is not None else settings.ocrmypdf_language
        self.deskew = deskew if deskew is not None else settings.ocrmypdf_deskew
        self.rotate_pages = rotate_pages if rotate_pages is not None else settings.ocrmypdf_rotate_pages
        self.force_ocr = settings.ocrmypdf_force_ocr
        self.enabled = settings.ocrmypdf_enabled

    def needs_ocr(self, detection) -> bool:
        """Determine if OCR preprocessing is needed.

        Args:
            detection: A ``TikaDetectionResult`` from the DETECTING stage.

        Returns:
            ``True`` if OCR is enabled and the PDF is either scanned
            (no usable text layer) OR has a present-but-garbled text layer
            (``low_text_quality``). The latter requires ``force_ocr`` so that
            OCRmyPDF replaces the corrupt text layer instead of skipping pages
            that already contain text.
        """
        if not self.enabled:
            return False
        if getattr(detection, "content_type", "") != "application/pdf":
            return False
        if getattr(detection, "is_scanned", False):
            return True
        if getattr(detection, "low_text_quality", False) and self.force_ocr:
            return True
        return False

    def preprocess(
        self,
        input_path: str,
        output_path: str,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """Run OCRmyPDF on a scanned PDF to add a text layer.

        Args:
            input_path:   Path to the scanned PDF.
            output_path:  Path where the OCR'd PDF will be written.
            cancel_check: Optional cancellation callback.

        Returns:
            ``True`` if OCR succeeded, ``False`` otherwise.
        """
        if cancel_check and cancel_check():
            logger.info("OCR preprocessing cancelled before start.")
            return False

        try:
            import ocrmypdf
        except ImportError:
            logger.error(
                "ocrmypdf is not installed. Install it with: pip install ocrmypdf"
            )
            return False

        input_file = Path(input_path)
        if not input_file.is_file():
            logger.error(f"OCRmyPDF input file not found: {input_path}")
            return False

        logger.info(
            f"Running OCRmyPDF on '{input_file.name}' "
            f"(lang={self.language}, deskew={self.deskew}, rotate={self.rotate_pages}, force_ocr={self.force_ocr})"
        )

        try:
            ocrmypdf.ocr(
                str(input_path),
                str(output_path),
                language=self.language,
                deskew=self.deskew,
                rotate_pages=self.rotate_pages,
                force_ocr=self.force_ocr,
                progress_bar=False,
            )
            output_file = Path(output_path)
            if output_file.is_file() and output_file.stat().st_size > 0:
                logger.info(
                    f"OCRmyPDF completed: '{input_file.name}' → '{output_file.name}' "
                    f"({output_file.stat().st_size} bytes)"
                )
                return True
            else:
                logger.warning(f"OCRmyPDF produced empty output for '{input_file.name}'")
                return False
        except Exception as e:
            logger.error(f"OCRmyPDF failed for '{input_file.name}': {e}")
            return False


# ---------------------------------------------------------------------------
# Register in CapabilityRegistry
# ---------------------------------------------------------------------------

from retriva.registry import CapabilityRegistry

CapabilityRegistry().register("ocrmypdf_preprocessor", OCRmyPDFPreprocessor, priority=100)
