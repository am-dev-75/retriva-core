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
Unit tests for the Docling parser.

All tests mock the docling.DocumentConverter — no Docling models required.
"""

import enum

import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from retriva.domain.models import CanonicalRecord


class _FakeDocItemLabel(str, enum.Enum):
    """Mimics Docling 2.x ``DocItemLabel`` enum (str-based, has ``.value``)."""

    TITLE = "title"
    SECTION_HEADER = "section_header"
    PARAGRAPH = "paragraph"
    TEXT = "text"
    TABLE = "table"


class TestDoclingParser:
    """Tests for DoclingParser.parse()."""

    def _make_parser(self):
        """Create a DoclingParser with a mocked converter."""
        # We need to mock the import of docling inside the lazy init
        from retriva.ingestion.docling_parser import DoclingParser
        parser = DoclingParser()
        return parser

    def _mock_item(self, label, text, page_no=1):
        """Create a mock Docling document item.

        ``label`` is wrapped in a ``DocItemLabel``-like enum to mirror the
        real Docling 2.x API, where ``item.label`` is an enum rather than a
        bare string. ``export_to_markdown`` is intentionally NOT provided as
        a callable returning text here; real Docling text items expose their
        content via ``.text``, so we rely on that strategy.
        """
        item = MagicMock()
        item.label = _FakeDocItemLabel(label)
        item.text = text
        # Real Docling TextItem objects do not implement export_to_markdown;
        # make it fail so the parser falls through to the .text strategy.
        item.export_to_markdown.side_effect = AttributeError("no export_to_markdown")

        # Mock provenance
        prov_entry = MagicMock()
        prov_entry.page_no = page_no
        prov_entry.bbox = None
        item.prov = [prov_entry]
        item.parent = None
        item.image = None

        return item

    @staticmethod
    def _as_iterate_items(items):
        """Wrap items as Docling 2.x ``(item, level)`` tuples.

        ``DoclingDocument.iterate_items()`` yields ``(NodeItem, level)``
        tuples, NOT bare items. Tests must mirror this so the tuple-unpacking
        in the parser is actually exercised.
        """
        return iter([(it, 1) for it in items])

    @patch("retriva.ingestion.docling_parser.DoclingParser._get_converter")
    def test_parse_returns_canonical_records(self, mock_get_converter, tmp_path):
        # Create a mock Docling result
        mock_doc = MagicMock()
        items = [
            self._mock_item("title", "Chapter 1: Introduction"),
            self._mock_item("paragraph", "This is the introduction text."),
            self._mock_item("paragraph", "More content here."),
        ]
        mock_doc.iterate_items.return_value = self._as_iterate_items(items)

        mock_result = MagicMock()
        mock_result.document = mock_doc

        mock_converter = MagicMock()
        mock_converter.convert.return_value = mock_result
        mock_get_converter.return_value = mock_converter

        f = tmp_path / "test.pdf"
        f.write_bytes(b"content")

        parser = self._make_parser()
        parser._converter = mock_converter
        records = parser.parse(str(f), "application/pdf")

        assert len(records) == 3
        assert all(isinstance(r, CanonicalRecord) for r in records)
        assert records[0].element_type == "heading"
        assert records[0].text == "Chapter 1: Introduction"
        assert records[1].element_type == "text"
        assert all(r.parser_name == "docling" for r in records)

    @patch("retriva.ingestion.docling_parser.DoclingParser._get_converter")
    def test_table_element_type(self, mock_get_converter, tmp_path):
        mock_doc = MagicMock()
        table_item = self._mock_item("table", "| A | B |\n|---|---|\n| 1 | 2 |")
        table_item.export_to_html.return_value = "<table><tr><td>1</td></tr></table>"
        mock_doc.iterate_items.return_value = self._as_iterate_items([table_item])

        mock_result = MagicMock()
        mock_result.document = mock_doc

        mock_converter = MagicMock()
        mock_converter.convert.return_value = mock_result
        mock_get_converter.return_value = mock_converter

        f = tmp_path / "test.pdf"
        f.write_bytes(b"content")

        parser = self._make_parser()
        parser._converter = mock_converter
        records = parser.parse(str(f), "application/pdf")

        assert len(records) == 1
        assert records[0].element_type == "table"
        assert records[0].table_html is not None

    @patch("retriva.ingestion.docling_parser.DoclingParser._get_converter")
    def test_empty_items_skipped(self, mock_get_converter, tmp_path):
        mock_doc = MagicMock()
        items = [
            self._mock_item("paragraph", ""),  # empty
            self._mock_item("paragraph", "   "),  # whitespace only
            self._mock_item("paragraph", "Valid content"),
        ]
        mock_doc.iterate_items.return_value = self._as_iterate_items(items)

        mock_result = MagicMock()
        mock_result.document = mock_doc

        mock_converter = MagicMock()
        mock_converter.convert.return_value = mock_result
        mock_get_converter.return_value = mock_converter

        f = tmp_path / "test.pdf"
        f.write_bytes(b"content")

        parser = self._make_parser()
        parser._converter = mock_converter
        records = parser.parse(str(f), "application/pdf")

        assert len(records) == 1
        assert records[0].text == "Valid content"

    @patch("retriva.ingestion.docling_parser.DoclingParser._get_converter")
    def test_fallback_markdown_export(self, mock_get_converter, tmp_path):
        """When iterate_items() is not available, fall back to markdown export."""
        mock_doc = MagicMock()
        mock_doc.iterate_items.side_effect = AttributeError("no iterate_items")
        mock_doc.export_to_markdown.return_value = "# Fallback\n\nSome text."

        mock_result = MagicMock()
        mock_result.document = mock_doc

        mock_converter = MagicMock()
        mock_converter.convert.return_value = mock_result
        mock_get_converter.return_value = mock_converter

        f = tmp_path / "test.pdf"
        f.write_bytes(b"content")

        parser = self._make_parser()
        parser._converter = mock_converter
        records = parser.parse(str(f), "application/pdf")

        assert len(records) == 1
        assert records[0].text == "# Fallback\n\nSome text."
        assert records[0].parser_name == "docling"

    @patch("retriva.ingestion.docling_parser.DoclingParser._get_converter")
    def test_iterate_items_yields_tuples_regression(self, mock_get_converter, tmp_path):
        """Regression test for the "0 canonical records" bug.

        Docling >= 2.x ``DoclingDocument.iterate_items()`` yields
        ``(item, level)`` tuples, not bare items. If the parser does not
        unpack the tuple it operates on a ``tuple`` object, every text
        extraction strategy returns ``None`` and the document silently
        produces ZERO records — exactly the symptom seen when ingesting a
        large PDF ("Docling produced 0 canonical records").

        This test feeds genuine 2.x-shaped tuples and asserts records are
        still produced.
        """
        mock_doc = MagicMock()
        items = [
            self._mock_item("section_header", "3.3 Checkpoints"),
            self._mock_item("paragraph", "The power outlet must be checked."),
        ]
        # Explicitly yield (item, level) tuples — the real Docling 2.x shape.
        mock_doc.iterate_items.return_value = iter([(items[0], 1), (items[1], 1)])

        mock_result = MagicMock()
        mock_result.document = mock_doc

        mock_converter = MagicMock()
        mock_converter.convert.return_value = mock_result
        mock_get_converter.return_value = mock_converter

        f = tmp_path / "large.pdf"
        f.write_bytes(b"content")

        parser = self._make_parser()
        parser._converter = mock_converter
        records = parser.parse(str(f), "application/pdf")

        # The bug would make this 0.
        assert len(records) == 2
        assert records[0].text == "3.3 Checkpoints"
        assert records[1].text == "The power outlet must be checked."

    @patch("retriva.ingestion.docling_parser.DoclingParser._get_converter")
    def test_enum_label_maps_to_element_type(self, mock_get_converter, tmp_path):
        """``item.label`` is a ``DocItemLabel`` enum in Docling 2.x.

        The element-type map keys are plain strings, so the enum must be
        coerced to its string value before lookup; otherwise every element
        would fall through to the default "text" type and structural
        information (headings, tables) would be lost.
        """
        mock_doc = MagicMock()
        items = [
            self._mock_item("title", "Document Title"),
            self._mock_item("section_header", "A Section"),
        ]
        mock_doc.iterate_items.return_value = self._as_iterate_items(items)

        mock_result = MagicMock()
        mock_result.document = mock_doc

        mock_converter = MagicMock()
        mock_converter.convert.return_value = mock_result
        mock_get_converter.return_value = mock_converter

        f = tmp_path / "test.pdf"
        f.write_bytes(b"content")

        parser = self._make_parser()
        parser._converter = mock_converter
        records = parser.parse(str(f), "application/pdf")

        assert len(records) == 2
        # Both 'title' and 'section_header' map to the canonical 'heading'.
        assert records[0].element_type == "heading"
        assert records[1].element_type == "heading"

    @patch("retriva.ingestion.docling_parser.DoclingParser._get_converter")
    def test_legacy_bare_items_still_supported(self, mock_get_converter, tmp_path):
        """Older Docling (< 2.x) yields bare items; the parser must still cope."""
        mock_doc = MagicMock()
        items = [
            self._mock_item("paragraph", "Legacy content."),
        ]
        # Bare items, NOT tuples.
        mock_doc.iterate_items.return_value = iter(items)

        mock_result = MagicMock()
        mock_result.document = mock_doc

        mock_converter = MagicMock()
        mock_converter.convert.return_value = mock_result
        mock_get_converter.return_value = mock_converter

        f = tmp_path / "test.pdf"
        f.write_bytes(b"content")

        parser = self._make_parser()
        parser._converter = mock_converter
        records = parser.parse(str(f), "application/pdf")

        assert len(records) == 1
        assert records[0].text == "Legacy content."
