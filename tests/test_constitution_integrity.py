# Constitution integrity check (governance, Spec 023 follow-up F-1)
#
# Deterministic structural checks for the canonical Retriva
# constitution at .agent/rules/retriva-constitution.md.
#
# Constitution v1.2 was ratified by the owner on 2026-10-03 (ADR-028,
# recovery of the unrecoverable, truncated v1.1) and installed
# byte-for-byte from the ratified candidate.  This test pins the
# ratified version, the final invariant, and the full-file hash so
# that any future truncation, tampering, or partial edit of the
# canonical file fails loudly.  The pins MUST never be silenced,
# skipped, xfailed, or marked expected-failure (Constitution v1.2
# §46); a red check blocks governance acceptance until repaired
# through §47.

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

CONSTITUTION = (
    Path(__file__).resolve().parents[1]
    / ".agent" / "rules" / "retriva-constitution.md"
)

# Owner-pin constants (ratified 2026-10-03, ADR-028).
EXPECTED_VERSION = "1.2"
EXPECTED_FINAL_INVARIANT_SUBSTR = (
    "prevails until it is formally amended through section 47")
EXPECTED_SHA256 = (
    "8b2fb1fe7c26257bf92742d16e8debafd154f65975b10e5c5891d578685f69ca")

# Structural markers the accepted constitution must contain.
REQUIRED_HEADING_PATTERNS = {
    "amendment section": re.compile(r"^#+\s.*Amendments?\b", re.MULTILINE
                                    | re.IGNORECASE),
    "verification section": re.compile(r"^#+\s.*Verification\b",
                                       re.MULTILINE | re.IGNORECASE),
}
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
SECTION_RE = re.compile(r"^## (\d+)\.\s", re.MULTILINE)


def _load() -> str:
    assert CONSTITUTION.is_file(), (
        f"canonical constitution missing: {CONSTITUTION}")
    return CONSTITUTION.read_text(encoding="utf-8")


def _frontmatter(text: str) -> dict:
    m = FRONTMATTER_RE.match(text)
    assert m, "missing or malformed YAML frontmatter"
    fields = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    return fields


def test_frontmatter_and_always_apply():
    fm = _frontmatter(_load())
    assert fm.get("alwaysApply") == "true", (
        "frontmatter must declare alwaysApply: true")
    assert fm.get("description"), "frontmatter must carry a description"


def test_version_matches_expected():
    text = _load()
    m = re.search(r"\*\*Version:\*\*\s*(\S+)", text)
    assert m, "missing '**Version:**' header"
    assert m.group(1) == EXPECTED_VERSION, (
        f"unexpected constitution version {m.group(1)!r}; "
        f"expected {EXPECTED_VERSION!r}")


def test_numbered_sections_sequential():
    numbers = [int(n) for n in SECTION_RE.findall(_load())]
    assert numbers, "no numbered '## N.' sections found"
    expected = list(range(1, max(numbers) + 1))
    assert numbers == sorted(numbers), (
        f"section numbers out of order: {numbers}")
    assert numbers == expected, (
        f"missing section(s): {sorted(set(expected) - set(numbers))}")


def test_required_governance_sections_present():
    text = _load()
    missing = [
        name for name, pattern in REQUIRED_HEADING_PATTERNS.items()
        if not pattern.search(text)
    ]
    assert not missing, (
        "canonical constitution lacks required governance sections: "
        f"{missing}; repair through the amendment process of "
        "Constitution v1.2 section 47")


def test_final_invariant_present():
    text = _load()
    assert not text.rstrip().endswith("```"), (
        "premature EOF: document ends at a bare code fence (inside the "
        "section-42 pack layout); the accepted text continues past it")
    if EXPECTED_FINAL_INVARIANT_SUBSTR:
        assert EXPECTED_FINAL_INVARIANT_SUBSTR in text, (
            "missing the accepted final invariant "
            f"({EXPECTED_FINAL_INVARIANT_SUBSTR!r})")
    else:
        pytest.fail(
            "final invariant not pinned (EXPECTED_FINAL_INVARIANT_SUBSTR "
            "empty): owner must restore the accepted v1.1 and pin its "
            "final invariant here")


@pytest.mark.skipif(not EXPECTED_SHA256,
                    reason="full-file sha256 pin not configured")
def test_full_file_sha256_pinned():
    digest = hashlib.sha256(_load().encode("utf-8")).hexdigest()
    assert digest == EXPECTED_SHA256, (
        f"constitution sha256 {digest} != pinned {EXPECTED_SHA256}")
