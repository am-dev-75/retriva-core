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

from pathlib import Path
import threading
from typing import Callable, List, Optional

from retriva.domain.models import CanonicalRecord
from retriva.logger import get_logger

logger = get_logger(__name__)

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

        if not text or not text.strip():
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
            try:
                table_html = item.export_to_html()
            except (AttributeError, Exception):
                pass

        # Image handling
        image_path = None
        if element_type == "image":
            image_ref = getattr(item, "image", None)
            if image_ref is not None:
                img_path = getattr(image_ref, "uri", None) or getattr(image_ref, "path", None)
                if img_path:
                    image_path = str(img_path)

        return CanonicalRecord(
            document_id=source,
            element_type=element_type,
            text=text.strip(),
            page=page,
            bbox=bbox,
            heading_path=heading_path,
            table_html=table_html,
            table_markdown=table_markdown,
            source_uri=source,
            parser_name="docling",
            image_path=image_path,
        )


# ---------------------------------------------------------------------------
# Register in CapabilityRegistry
# ---------------------------------------------------------------------------

from retriva.registry import CapabilityRegistry

CapabilityRegistry().register("parser:docling", DoclingParser, priority=200)
