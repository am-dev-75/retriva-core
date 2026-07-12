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
from pathlib import Path
from retriva.ingestion.markdown_parser import split_by_headings, derive_title, parse_markdown

def test_split_by_headings_basic():
    text = """# Heading 1
Content 1
## Heading 2
Content 2
"""
    sections = split_by_headings(text)
    assert len(sections) == 2
    assert sections[0]["heading"] == "Heading 1"
    assert sections[0]["content"] == "Content 1"
    assert sections[1]["heading"] == "Heading 2"
    assert sections[1]["content"] == "Content 2"

def test_split_by_headings_pre_text():
    text = """Intro text
# Heading 1
Content 1
"""
    sections = split_by_headings(text)
    assert len(sections) == 2
    assert sections[0]["heading"] == ""
    assert sections[0]["content"] == "Intro text"
    assert sections[1]["heading"] == "Heading 1"
    assert sections[1]["content"] == "Content 1"

def test_derive_title_from_h1():
    text = "# My Awesome Doc\nSome content"
    assert derive_title(text, Path("test.md")) == "My Awesome Doc"

def test_derive_title_fallback():
    text = "## No H1 here\nSome content"
    assert derive_title(text, Path("my-cool-file.md")) == "My Cool File"

def test_parse_markdown_empty_sections(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    f = d / "test.md"
    f.write_text("# Heading 1\n\n# Heading 2\nContent 2")
    
    result = parse_markdown(f)
    assert result["title"] == "Heading 1"
    assert len(result["sections"]) == 1
    assert result["sections"][0]["heading"] == "Heading 2"
    assert result["sections"][0]["content"] == "Content 2"
