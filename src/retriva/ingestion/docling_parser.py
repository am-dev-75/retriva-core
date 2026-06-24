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
Docling parser for the v2 ingestion pipeline.

Uses ``docling.DocumentConverter`` to perform high-fidelity structural
parsing of PDFs, DOCX, PPTX, HTML, and other document formats.  Emits
``CanonicalRecord`` objects for downstream normalization.
"""

import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Callable, List, Optional

from retriva.domain.models import CanonicalRecord
from retriva.logger import get_logger

logger = get_logger(__name__)

# Directory under which extracted page images are persisted so that the
# NORMALIZATION stage (VLM describer) can read them back. Files here are
# transient; the pipeline removes them after enrichment.
_IMAGE_TMP_PREFIX = "retriva_docling_img_"

# Docling element types → our canonical element_type mapping
_ELEMENT_TYPE_MAP = {
    "title": "heading",
    "section_header": "heading",
    "paragraph": "text",
    "text": "text",
    "table": "table",
    "picture": "image",
    "figure": "image",
    "caption": "text",
    "formula": "text",
    "list_item": "text",
    "page_header": "text",
    "page_footer": "text",
    "footnote": "text",
    "code": "text",
}


class DoclingParser:
    """Primary structural parser using Docling.

    Converts documents to Docling's internal representation and emits
    ``CanonicalRecord`` objects with element-level granularity.

    Supports: PDF, DOCX, PPTX, XLSX, HTML, Markdown, CSV, images.
    """

    def __init__(self):
        self._converter = None
        self._lock = threading.Lock()

    @staticmethod
    def _apply_ocr_option(pipeline_options, do_ocr: bool) -> None:
        """Set Docling's ``do_ocr`` flag defensively.

        ``do_ocr`` defaults to True in Docling and enables its built-in OCR
        engine. We disable it by default (OCR is done upstream by OCRmyPDF).
        Guarded with try/except so a Docling version without the attribute
        does not break initialization.
        """
        try:
            pipeline_options.do_ocr = do_ocr
            logger.debug(f"Docling pipeline do_ocr set to {do_ocr}")
        except Exception as e:
            logger.debug(f"Could not set Docling do_ocr option: {e}")

    def _get_converter(self):
        """Lazy-initialize the DocumentConverter (heavy import)."""
        if self._converter is None:
            try:
                from docling.document_converter import DocumentConverter, PdfFormatOption
                from docling.datamodel.pipeline_options import PdfPipelineOptions
                from docling.datamodel.base_models import InputFormat
                from retriva.config import settings

                device = settings.accelerator_device

                # Configure converter to use the specified device (e.g. 'cuda', 'cpu', 'auto')
                # This affects layout analysis, table extraction, and OCR.
                pipeline_options = PdfPipelineOptions()
                try:
                    pipeline_options.num_threads = 2
                except Exception:
                    pass

                # Disable Docling's built-in OCR. OCR is handled upstream by
                # OCRmyPDF (PREPROCESSING stage); leaving Docling's OCR enabled
                # is redundant and, on some builds, crashes ``convert()`` with
                # "Unsupported configuration: torch.PP-OCRv6.det.small",
                # yielding zero records. ``do_ocr`` defaults to True in
                # Docling, so we must set it explicitly.
                self._apply_ocr_option(pipeline_options, settings.docling_do_ocr)

                # Enable picture rasterization so figures/diagrams can be
                # persisted and handed to the VLM describer. Without this,
                # Docling emits picture placeholders with no usable image and
                # the VLM enrichment step is silently skipped.
                try:
                    if settings.docling_generate_picture_images:
                        pipeline_options.generate_picture_images = True
                        pipeline_options.images_scale = settings.docling_images_scale
                except Exception as e:
                    logger.debug(f"Could not enable picture image generation: {e}")

                try:
                    pipeline_options.accelerator_options.device = device
                except AttributeError:
                    # Fallback in case older versions of Docling don't have accelerator_options
                    logger.debug("accelerator_options not found on PdfPipelineOptions, passing device directly")
                    pipeline_options = PdfPipelineOptions(device=device)
                    try:
                        pipeline_options.num_threads = 2
                    except Exception:
                        pass
                    self._apply_ocr_option(pipeline_options, settings.docling_do_ocr)
                    try:
                        if settings.docling_generate_picture_images:
                            pipeline_options.generate_picture_images = True
                            pipeline_options.images_scale = settings.docling_images_scale
                    except Exception:
                        pass

                self._converter = DocumentConverter(
                    format_options={
                        InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
                    }
                )
                logger.debug(f"Docling DocumentConverter initialized with device: {device}")
            except ImportError:
                raise ImportError(
                    "docling is not installed. Install it with: pip install docling"
                )
        return self._converter

    def parse(
        self,
        source: str,
        content_type: str,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> List[CanonicalRecord]:
        """Convert a document and emit canonical records.

        Args:
            source:       Local file path to the document.
            content_type: MIME type (from detection or explicit hint).
            cancel_check: Optional cancellation callback.

        Returns:
            List of ``CanonicalRecord`` objects.
        """
        converter = self._get_converter()
        path = Path(source)

        logger.info(f"Docling parsing '{path.name}' (type={content_type})")

        try:
            with self._lock:
                result = converter.convert(str(path))
        except Exception as e:
            logger.error(f"Docling conversion failed for '{path.name}': {e}")
            return []

        doc = result.document
        records: List[CanonicalRecord] = []

        # Iterate over document elements (Docling's internal structure).
        #
        # NOTE: In Docling >= 2.x, ``DoclingDocument.iterate_items()`` yields
        # ``(NodeItem, level)`` tuples rather than bare items.  We must unpack
        # the tuple before handing the actual item to ``_item_to_record``;
        # otherwise every text-extraction strategy operates on a ``tuple``,
        # silently returns ``None`` and the document yields 0 records.
        try:
            for entry in doc.iterate_items():
                if cancel_check and cancel_check():
                    from retriva.ingestion_api.job_manager import CancellationError
                    raise CancellationError("Cancelled during Docling parsing")

                # Docling 2.x yields (item, level) tuples; older versions yield
                # bare items. Normalize both shapes to a single ``item``.
                if isinstance(entry, tuple):
                    item = entry[0]
                else:
                    item = entry

                record = self._item_to_record(item, source, doc)
                if record is not None:
                    records.append(record)
        except AttributeError:
            # Fallback: if iterate_items() is not available (older Docling version),
            # export the whole document as markdown and create a single record
            logger.debug("Docling iterate_items() not available, using markdown export")
            markdown_text = doc.export_to_markdown()
            if markdown_text.strip():
                records.append(CanonicalRecord(
                    document_id=source,
                    element_type="text",
                    text=markdown_text,
                    source_uri=source,
                    parser_name="docling",
                ))

        logger.info(
            f"Docling produced {len(records)} canonical records from '{path.name}'"
        )
        return records

    def _item_to_record(self, item, source: str, doc) -> Optional[CanonicalRecord]:
        """Convert a single Docling document item to a CanonicalRecord."""
        # Determine element type.
        #
        # In Docling 2.x ``item.label`` is a ``DocItemLabel`` enum, not a plain
        # string.  Using the enum directly as a dict key never matches the
        # string keys in ``_ELEMENT_TYPE_MAP`` (everything would fall through to
        # "text"), so coerce it to its string value first.
        raw_label = getattr(item, "label", None)
        if raw_label is not None:
            item_type = getattr(raw_label, "value", None) or str(raw_label)
        else:
            item_type = type(item).__name__.lower()
        element_type = _ELEMENT_TYPE_MAP.get(item_type, "text")

        # Extract text content — try multiple strategies in order of preference.
        # Newer Docling versions require the `doc` context for markdown export;
        # without it, export_to_markdown() may fail or return empty.
        # NEVER fall back to str(item), which produces Python repr with
        # BoundingBox, DocItemLabel, etc. that pollutes embeddings.
        text = ""

        # Strategy 1: export_to_markdown with doc context (Docling >= 2.x)
        if not text:
            try:
                result = item.export_to_markdown(doc)
                if isinstance(result, str) and result.strip():
                    text = result
            except (AttributeError, TypeError, Exception):
                pass

        # Strategy 2: export_to_markdown without context (Docling < 2.x)
        if not text:
            try:
                result = item.export_to_markdown()
                if isinstance(result, str) and result.strip():
                    text = result
            except (AttributeError, Exception):
                pass

        # Strategy 3: direct .text attribute
        if not text:
            result = getattr(item, "text", None)
            if isinstance(result, str) and result.strip():
                text = result

        # Strategy 4: .orig attribute (original text, seen in Docling items)
        if not text:
            result = getattr(item, "orig", None)
            if isinstance(result, str) and result.strip():
                text = result

        # Image handling — persist the rasterized picture to a temp file so the
        # NORMALIZATION stage can run the VLM describer over an actual file.
        # Done before the empty-text guard because an image item legitimately
        # has no text yet (the VLM fills it in during NORMALIZATION).
        image_path = None
        if element_type == "image":
            image_path = self._persist_item_image(item, doc)
            # If the picture has a caption, keep it as the placeholder text so
            # the chunk still carries some context even if the VLM is disabled
            # or fails. The VLM description (if any) overwrites this later.
            caption = self._extract_caption(item, doc)
            if caption:
                text = caption

        # Image items are kept as long as we persisted an image; all other
        # element types require non-empty text.
        if element_type == "image":
            if not image_path:
                return None
        elif not text or not text.strip():
            return None

        # Page number
        page = None
        prov = getattr(item, "prov", None)
        if prov and isinstance(prov, list) and prov:
            page = getattr(prov[0], "page_no", None)

        # Bounding box
        bbox = None
        if prov and isinstance(prov, list) and prov:
            bbox_obj = getattr(prov[0], "bbox", None)
            if bbox_obj is not None:
                try:
                    bbox = (
                        float(bbox_obj.l),
                        float(bbox_obj.t),
                        float(bbox_obj.r),
                        float(bbox_obj.b),
                    )
                except (AttributeError, TypeError, ValueError):
                    pass

        # Heading path (hierarchical context)
        heading_path = []
        try:
            # Walk up the document tree to collect headings
            parent = getattr(item, "parent", None)
            while parent is not None:
                parent_label_raw = getattr(parent, "label", "")
                parent_label = getattr(parent_label_raw, "value", None) or str(parent_label_raw)
                if parent_label in ("title", "section_header"):
                    parent_text = getattr(parent, "text", "")
                    if parent_text:
                        heading_path.insert(0, parent_text)
                parent = getattr(parent, "parent", None)
        except Exception:
            pass

        # Table handling
        table_markdown = None
        table_html = None
        if element_type == "table":
            table_markdown = text
            # Docling 2.x deprecates ``export_to_html()`` without the ``doc``
            # argument (it floods the logs at ERROR level). Pass ``doc`` first
            # and only fall back to the no-arg form on older versions.
            try:
                table_html = item.export_to_html(doc)
            except (TypeError, AttributeError):
                try:
                    table_html = item.export_to_html()
                except Exception:
                    pass
            except Exception:
                pass

        return CanonicalRecord(
            document_id=source,
            element_type=element_type,
            text=(text or "").strip(),
            page=page,
            bbox=bbox,
            heading_path=heading_path,
            table_html=table_html,
            table_markdown=table_markdown,
            source_uri=source,
            parser_name="docling",
            image_path=image_path,
        )

    def _extract_caption(self, item, doc) -> str:
        """Return a picture/table caption if Docling extracted one."""
        try:
            caption = item.caption_text(doc)
            if isinstance(caption, str) and caption.strip():
                return caption.strip()
        except (AttributeError, TypeError, Exception):
            pass
        return ""

    def _persist_item_image(self, item, doc) -> Optional[str]:
        """Rasterize a Docling picture item and persist it to a temp PNG file.

        Returns the absolute path to the written image, or ``None`` if no
        usable image is available (picture image generation disabled, image
        too small, or any failure). Returning ``None`` causes the caller to
        drop the image record instead of emitting a useless placeholder.
        """
        from retriva.config import settings

        # Obtain a PIL image. ``get_image(doc)`` is the Docling 2.x API and
        # requires ``generate_picture_images=True`` in the pipeline options.
        pil_image = None
        try:
            pil_image = item.get_image(doc)
        except (AttributeError, TypeError, Exception):
            pil_image = None

        # Fallback: older Docling exposes a pre-rendered ``.image.pil_image``.
        if pil_image is None:
            image_ref = getattr(item, "image", None)
            pil_image = getattr(image_ref, "pil_image", None) if image_ref is not None else None

        if pil_image is None:
            return None

        # Skip images too small to carry retrievable content (icons, rules).
        try:
            width, height = pil_image.size
            if width * height < settings.docling_min_picture_area_px:
                logger.debug(
                    f"Skipping tiny image ({width}x{height}px, below "
                    f"{settings.docling_min_picture_area_px}px threshold)"
                )
                return None
        except Exception:
            pass

        # Persist as PNG to a stable temp directory.
        try:
            tmp_dir = os.path.join(tempfile.gettempdir(), "retriva_docling_images")
            os.makedirs(tmp_dir, exist_ok=True)
            filename = f"{_IMAGE_TMP_PREFIX}{uuid.uuid4().hex}.png"
            out_path = os.path.join(tmp_dir, filename)
            # Normalize mode so PNG encoding always succeeds (e.g. CMYK/P).
            if pil_image.mode not in ("RGB", "RGBA", "L"):
                pil_image = pil_image.convert("RGB")
            pil_image.save(out_path, format="PNG")
            return out_path
        except Exception as e:
            logger.warning(f"Failed to persist Docling image: {e}")
            return None


# ---------------------------------------------------------------------------
# Register in CapabilityRegistry
# ---------------------------------------------------------------------------

from retriva.registry import CapabilityRegistry

CapabilityRegistry().register("parser:docling", DoclingParser, priority=200)
