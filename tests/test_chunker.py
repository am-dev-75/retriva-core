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

import pytest
from retriva.domain.models import ParsedDocument
from retriva.ingestion.chunker import (
    create_chunks,
    recursive_split_text,
    _is_heading,
    _extract_heading_text,
    _merge_heading_paragraphs,
    _prepend_section_context,
)
from retriva.config import settings


# ---------------------------------------------------------------------------
# Existing tests (preserved)
# ---------------------------------------------------------------------------

def test_recursive_split_text():
    text = "Line 1\nLine 2. Sentence 1. Sentence 2. Word1 Word2 Word3"
    # Split by \n
    chunks = recursive_split_text(text, max_chars=10, overlap=0)
    for c in chunks:
        assert len(c) <= 10
    assert len(chunks) > 1

def test_recursive_split_text_with_overlap():
    text = "This is a very long sentence that should be split with some overlap."
    max_chars = 20
    overlap = 5
    chunks = recursive_split_text(text, max_chars=max_chars, overlap=overlap)
    
    for i in range(1, len(chunks)):
        # Check if there is some overlap (this is a bit heuristic but should work)
        # Actually, let's just check lengths first
        assert len(chunks[i-1]) <= max_chars
        assert len(chunks[i]) <= max_chars

def test_create_chunks_long_doc():
    # Mock settings
    original_max = settings.max_chunk_chars
    settings.max_chunk_chars = 50
    try:
        content = "Paragraph 1 is short.\n\nParagraph 2 is very " + "long " * 20 + "and needs splitting."
        doc = ParsedDocument(
            source_path="test.html",
            canonical_doc_id="test",
            page_title="Test",
            content_text=content
        )
        chunks = create_chunks(doc)
        
        assert len(chunks) > 2 # At least paragraph 1 and multiple for paragraph 2
        for c in chunks:
            assert len(c.text) <= 50
    finally:
        settings.max_chunk_chars = original_max


# ---------------------------------------------------------------------------
# Heading detection helpers
# ---------------------------------------------------------------------------

class TestHeadingDetection:
    """Tests for _is_heading and _extract_heading_text."""

    def test_h1_detected(self):
        assert _is_heading("# Main Title") is True

    def test_h2_detected(self):
        assert _is_heading("## Section 2.1") is True

    def test_h3_detected(self):
        assert _is_heading("### Subsection") is True

    def test_h4_detected(self):
        assert _is_heading("#### Deep Heading") is True

    def test_h5_not_detected(self):
        """Only # through #### are supported (matches records_to_parsed_document)."""
        assert _is_heading("##### Too Deep") is False

    def test_plain_text_not_heading(self):
        assert _is_heading("This is normal body text.") is False

    def test_hash_in_body_not_heading(self):
        """A '#' that doesn't start the line shouldn't match."""
        assert _is_heading("Use the # symbol for comments") is False

    def test_no_space_after_hash_not_heading(self):
        assert _is_heading("#NoSpace") is False

    def test_extract_h2_text(self):
        assert _extract_heading_text("## 3.3 CHECKPOINTS WHEN PERFORMING ON-SITE SERVICE") == \
            "3.3 CHECKPOINTS WHEN PERFORMING ON-SITE SERVICE"

    def test_extract_h1_text(self):
        assert _extract_heading_text("# Overview") == "Overview"

    def test_extract_non_heading_returns_empty(self):
        assert _extract_heading_text("Just text") == ""


# ---------------------------------------------------------------------------
# Heading merging
# ---------------------------------------------------------------------------

class TestMergeHeadingParagraphs:
    """Tests for _merge_heading_paragraphs."""

    def test_heading_merged_with_following_body(self):
        paragraphs = [
            "## Safety Checks",
            "The power outlet must be checked.",
        ]
        result = _merge_heading_paragraphs(paragraphs)
        assert len(result) == 1
        heading, text = result[0]
        assert heading == "Safety Checks"
        assert "## Safety Checks" in text
        assert "The power outlet must be checked." in text

    def test_no_headings_returns_plain(self):
        """MediaWiki-style content: no markdown headings."""
        paragraphs = [
            "First paragraph about widgets.",
            "Second paragraph about gadgets.",
        ]
        result = _merge_heading_paragraphs(paragraphs)
        assert len(result) == 2
        for heading, _ in result:
            assert heading == ""

    def test_consecutive_headings(self):
        """Two headings with no body between them — first is emitted standalone."""
        paragraphs = [
            "## First Heading",
            "## Second Heading",
            "Body under second heading.",
        ]
        result = _merge_heading_paragraphs(paragraphs)
        # First heading emitted standalone, second merged with body
        assert len(result) == 2
        # First: standalone heading
        h1, t1 = result[0]
        assert "First Heading" in h1 or "First Heading" in t1
        # Second: merged with body
        h2, t2 = result[1]
        assert h2 == "Second Heading"
        assert "Body under second heading." in t2

    def test_heading_at_end_without_body(self):
        """A heading at the very end with no following body is emitted as-is."""
        paragraphs = [
            "Some text.",
            "## Trailing Heading",
        ]
        result = _merge_heading_paragraphs(paragraphs)
        assert len(result) == 2
        # First item is plain text
        assert result[0][0] == ""
        assert result[0][1] == "Some text."
        # Second is the orphaned heading
        assert result[1][0] == "Trailing Heading"

    def test_body_after_heading_inherits_section(self):
        """Body paragraphs after a heading (even non-adjacent) carry the section."""
        paragraphs = [
            "## Power Supply",
            "Check the outlet.",
            "Also check grounding.",
        ]
        result = _merge_heading_paragraphs(paragraphs)
        # Heading merged with first body, second body gets same section
        assert len(result) == 2
        assert result[0][0] == "Power Supply"
        assert result[1][0] == "Power Supply"
        assert "Also check grounding." in result[1][1]


# ---------------------------------------------------------------------------
# Section context prefix
# ---------------------------------------------------------------------------

class TestPrependSectionContext:
    """Tests for _prepend_section_context."""

    def test_prefix_added(self):
        result = _prepend_section_context("Body text.", "Safety Checks")
        assert result.startswith("[Section: Safety Checks]")
        assert "Body text." in result

    def test_empty_heading_noop(self):
        result = _prepend_section_context("Body text.", "")
        assert result == "Body text."


# ---------------------------------------------------------------------------
# Integration: create_chunks with section-aware content
# ---------------------------------------------------------------------------

class TestSectionAwareChunking:
    """End-to-end tests for create_chunks with markdown headings."""

    def _make_doc(self, content: str) -> ParsedDocument:
        return ParsedDocument(
            source_path="test.pdf",
            canonical_doc_id="test_doc",
            page_title="Test Manual",
            content_text=content,
        )

    def test_section_context_prepended(self):
        """Chunks under a heading carry [Section: ...] prefix."""
        content = (
            "## 3.3 CHECKPOINTS WHEN PERFORMING ON-SITE SERVICE\n\n"
            "The power outlet should have a capacity of at least the maximum power consumption."
        )
        doc = self._make_doc(content)
        chunks = create_chunks(doc)

        # Should produce 1 merged chunk (heading + body)
        text_chunks = [c for c in chunks if c.metadata.chunk_type == "text"]
        assert len(text_chunks) == 1
        assert text_chunks[0].text.startswith("[Section: 3.3 CHECKPOINTS WHEN PERFORMING ON-SITE SERVICE]")
        assert "The power outlet should have a capacity" in text_chunks[0].text

    def test_heading_merged_with_body(self):
        """Heading and body end up in the same chunk, not separated."""
        content = (
            "## Power Supply\n\n"
            "Check the outlet rating."
        )
        doc = self._make_doc(content)
        chunks = create_chunks(doc)

        text_chunks = [c for c in chunks if c.metadata.chunk_type == "text"]
        assert len(text_chunks) == 1
        # Both heading markdown and body text present in the single chunk
        assert "## Power Supply" in text_chunks[0].text
        assert "Check the outlet rating." in text_chunks[0].text

    def test_no_section_context_for_plain_text(self):
        """MediaWiki-like content (no markdown headings) is unchanged."""
        content = (
            "First paragraph about safety.\n\n"
            "Second paragraph about installation."
        )
        doc = self._make_doc(content)
        chunks = create_chunks(doc)

        text_chunks = [c for c in chunks if c.metadata.chunk_type == "text"]
        assert len(text_chunks) == 2
        # No [Section: ...] prefix
        for c in text_chunks:
            assert not c.text.startswith("[Section:")
        assert text_chunks[0].text == "First paragraph about safety."
        assert text_chunks[1].text == "Second paragraph about installation."

    def test_section_path_metadata_populated(self):
        """Chunks under a heading have section_path set in metadata."""
        content = (
            "## 3.3.1 Power Supply\n\n"
            "Check the outlet."
        )
        doc = self._make_doc(content)
        chunks = create_chunks(doc)

        text_chunks = [c for c in chunks if c.metadata.chunk_type == "text"]
        assert len(text_chunks) == 1
        assert text_chunks[0].metadata.section_path == "3.3.1 Power Supply"

    def test_section_path_empty_for_plain_text(self):
        """Plain text chunks have empty section_path (backward compat)."""
        content = "Just plain text without headings."
        doc = self._make_doc(content)
        chunks = create_chunks(doc)

        text_chunks = [c for c in chunks if c.metadata.chunk_type == "text"]
        assert len(text_chunks) == 1
        assert text_chunks[0].metadata.section_path == ""

    def test_nested_headings_use_deepest(self):
        """When a subsection heading follows a section heading, chunks use the most recent."""
        content = (
            "## 3.3 CHECKPOINTS\n\n"
            "Intro to checkpoints.\n\n"
            "### 3.3.1 Power Supply\n\n"
            "Check the outlet.\n\n"
            "### 3.3.2 Grounding\n\n"
            "Check grounding wire."
        )
        doc = self._make_doc(content)
        chunks = create_chunks(doc)

        text_chunks = [c for c in chunks if c.metadata.chunk_type == "text"]
        # Expect: merged(3.3+intro), merged(3.3.1+outlet), merged(3.3.2+grounding)
        assert len(text_chunks) == 3

        # First chunk: section 3.3
        assert text_chunks[0].metadata.section_path == "3.3 CHECKPOINTS"
        assert "[Section: 3.3 CHECKPOINTS]" in text_chunks[0].text

        # Second chunk: section 3.3.1
        assert text_chunks[1].metadata.section_path == "3.3.1 Power Supply"
        assert "[Section: 3.3.1 Power Supply]" in text_chunks[1].text
        assert "Check the outlet." in text_chunks[1].text

        # Third chunk: section 3.3.2
        assert text_chunks[2].metadata.section_path == "3.3.2 Grounding"
        assert "[Section: 3.3.2 Grounding]" in text_chunks[2].text
        assert "Check grounding wire." in text_chunks[2].text

    def test_long_section_content_splits_with_context(self):
        """When a section body exceeds max_chunk_chars, all split chunks get the section prefix."""
        original_max = settings.max_chunk_chars
        settings.max_chunk_chars = 100
        try:
            content = (
                "## Safety Warnings\n\n"
                + "This is a very important safety warning. " * 10
            )
            doc = self._make_doc(content)
            chunks = create_chunks(doc)

            text_chunks = [c for c in chunks if c.metadata.chunk_type == "text"]
            # Should produce multiple chunks due to size limit
            assert len(text_chunks) > 1
            # All chunks should carry the section context
            for c in text_chunks:
                assert c.metadata.section_path == "Safety Warnings"
                assert "[Section: Safety Warnings]" in c.text
        finally:
            settings.max_chunk_chars = original_max
