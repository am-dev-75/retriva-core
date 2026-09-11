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

"""Table-aware chunking tests.

Spreadsheet-derived markdown tables must be split at row boundaries: a row
split mid-cell loses its first cells (company name) and the next chunk
starts with orphaned cell fragments — the bug observed with CRM reference
spreadsheets.
"""

from retriva.ingestion.chunker import (
    _count_table_header_lines,
    _is_table_text,
    split_table_rows,
)


def _make_table(rows: int, row_chars: int = 100, with_header: bool = True) -> str:
    lines = []
    if with_header:
        lines.append("| Company | Domain | Industry | Employees | Revenue |")
        lines.append("|---|---|---|---|---|")
    for i in range(rows):
        pad = "x" * max(1, row_chars - 60)
        lines.append(
            f"| Company{i:03d} | company{i:03d}.com | Industry {i} | {100+i} | "
            f"€{i}M-{i+1}M | City {i} | Region {i} | {pad} |"
        )
    return "\n".join(lines)


def test_is_table_text_detects_markdown_tables():
    table = _make_table(5)
    assert _is_table_text(table) is True
    assert _is_table_text("plain text line\nanother plain line") is False
    assert _is_table_text("| only one row |") is False


def test_count_table_header_lines():
    table = _make_table(3)
    assert _count_table_header_lines(table) == 2  # header + separator
    assert _count_table_header_lines("| a | b |\n| c | d |") == 0


def test_split_table_rows_respects_row_boundaries():
    table = _make_table(30, row_chars=100)
    chunks = split_table_rows(table, max_chars=800, header_lines=2)
    assert len(chunks) > 1
    for chunk in chunks:
        for line in chunk.splitlines():
            # Every line must be a complete row: header, separator, or data.
            assert line.startswith("|") and line.endswith("|") or line.startswith("|---") \
                or line.startswith("| Company") or line.startswith("|--") or line == "" \
                or line.endswith("|"), f"incomplete row: {line[:80]}"


def test_split_table_rows_no_orphaned_fragments():
    """The failure mode that produced 'mbardy | OEM...' garbage orgs."""
    table = _make_table(30, row_chars=100)
    chunks = split_table_rows(table, max_chars=800, header_lines=2)
    for chunk in chunks:
        lines = chunk.splitlines()
        # The first data line must start with a pipe (a company name cell),
        # not a mid-cell fragment like "mbardy | ...".
        data_lines = [l for l in lines if l.startswith("|") and "---" not in l]
        for l in data_lines:
            assert l.startswith("| "), f"orphaned fragment: {l[:80]}"


def test_split_table_rows_repeats_header():
    table = _make_table(30, row_chars=100)
    chunks = split_table_rows(table, max_chars=800, header_lines=2)
    for chunk in chunks:
        lines = chunk.splitlines()
        assert lines[0].startswith("| Company"), "header missing from chunk"
        assert lines[1].startswith("|---"), "separator missing from chunk"


def test_split_table_rows_single_oversized_row_not_split():
    row = "|" + " x" * 500 + "|"  # ~1000 chars, exceeds max
    table = "| H1 | H2 |\n|---|---|\n" + row + "\n| small | row |"
    chunks = split_table_rows(table, max_chars=800, header_lines=2)
    # The oversized row must appear complete in one chunk, never truncated.
    assert any(row in c for c in chunks)


def test_split_table_rows_no_header():
    table = _make_table(20, row_chars=100, with_header=False)
    chunks = split_table_rows(table, max_chars=800, header_lines=0)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.splitlines()[0].startswith("|")
