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
Tests for the CanonicalRecord -> ParsedDocument conversion, focusing on the
noise-filtering behaviour (table-of-contents lines, OCR fragments, image
placeholders) introduced to improve retrieval quality.
"""

from retriva.domain.models import CanonicalRecord
from retriva.ingestion_api.routers.v2_documents import (
    records_to_parsed_document,
    _is_toc_line,
    _is_noise_fragment,
)


def _text(text, element_type="text", **kw):
    return CanonicalRecord(
        document_id="doc",
        element_type=element_type,
        text=text,
        source_uri="doc.pdf",
        parser_name="docling",
        **kw,
    )


# ---------------------------------------------------------------------------
# Low-level predicates
# ---------------------------------------------------------------------------

class TestTocLineDetection:
    def test_dot_leader_with_page_ref(self):
        assert _is_toc_line("3.3 CHECKPOINTS WHEN PERFORMING ON-SITE SERVICE..........eA-7")

    def test_dot_leader_short_code(self):
        assert _is_toc_line("3.14.34 C7601.... eiL-124")

    def test_plain_decimal_not_toc(self):
        # A body sentence merely containing a decimal number must NOT match.
        assert not _is_toc_line(
            "Section 3.3 explains the checks the engineer must perform on site."
        )

    def test_normal_sentence_not_toc(self):
        assert not _is_toc_line("The power outlet must be checked before service.")


class TestNoiseFragmentDetection:
    def test_empty_is_noise(self):
        assert _is_noise_fragment("")
        assert _is_noise_fragment("   ")

    def test_toc_is_noise(self):
        assert _is_noise_fragment("Overview....... 12")

    def test_no_alpha_is_noise(self):
        assert _is_noise_fragment("|---|---|")
        assert _is_noise_fragment("....")

    def test_short_garble_is_noise(self):
        assert _is_noise_fragment("BAN")

    def test_real_short_phrase_kept(self):
        # Has a space -> treated as a phrase, kept.
        assert not _is_noise_fragment("On site")

    def test_real_sentence_kept(self):
        assert not _is_noise_fragment("Check the protective earth connection.")

    def test_heading_exempt_from_short_check(self):
        # Terse section numbers are valid headings.
        assert not _is_noise_fragment("3.3.1", is_heading=True)


# ---------------------------------------------------------------------------
# Integration: records_to_parsed_document
# ---------------------------------------------------------------------------

class TestRecordsToParsedDocumentFiltering:
    def test_toc_and_fragments_dropped(self):
        records = [
            _text("# 3.3 CHECKPOINTS", element_type="heading"),
            _text("The power outlet must be checked before service."),
            _text("3.3 CHECKPOINTS WHEN PERFORMING ON-SITE SERVICE..........eA-7"),  # TOC
            _text("/\\"),        # OCR garble (no alpha)
            _text("|---|---|"),  # rule line
        ]
        pd = records_to_parsed_document(records, "doc.pdf", None)

        assert "The power outlet must be checked" in pd.content_text
        assert "# 3.3 CHECKPOINTS" in pd.content_text
        # Noise removed.
        assert "eA-7" not in pd.content_text
        assert "/\\" not in pd.content_text
        assert "|---|---|" not in pd.content_text

    def test_image_without_text_dropped(self):
        records = [
            _text("Body text that should survive."),
            _text("", element_type="image", image_path="/tmp/x.png"),  # placeholder
        ]
        pd = records_to_parsed_document(records, "doc.pdf", None)
        # No image context kept for the empty placeholder.
        assert pd.images == []
        assert "Body text that should survive." in pd.content_text

    def test_image_with_vlm_description_kept(self):
        records = [
            _text(
                "Wiring diagram showing the protective earth path from the inlet "
                "to the chassis ground stud.",
                element_type="image",
                image_path="/tmp/fig1.png",
            ),
        ]
        pd = records_to_parsed_document(records, "doc.pdf", None)
        assert len(pd.images) == 1
        assert "protective earth path" in pd.images[0].vlm_description

    def test_title_not_derived_from_toc_line(self):
        # First heading is a TOC artifact; title should fall back to a real one.
        records = [
            _text("Overview............ 1", element_type="heading"),  # noise heading
            _text("Service Manual", element_type="heading"),
            _text("Body content."),
        ]
        pd = records_to_parsed_document(records, "doc.pdf", None)
        assert pd.page_title == "Service Manual"

    def test_clean_document_unchanged(self):
        records = [
            _text("# Introduction", element_type="heading"),
            _text("This manual describes on-site service procedures."),
            _text("Always disconnect power before opening the unit."),
        ]
        pd = records_to_parsed_document(records, "doc.pdf", None)
        assert "This manual describes on-site service procedures." in pd.content_text
        assert "Always disconnect power before opening the unit." in pd.content_text
