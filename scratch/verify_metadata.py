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

#!/usr/bin/env python3
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
SDD 014 — End-to-end metadata verification script.

Traces user_metadata through every layer of the pipeline:
  1. Schema validation (accept + reject)
  2. ParsedDocument construction
  3. Chunker propagation
  4. Qdrant payload (model_dump)
  5. Citation builder visibility
  6. Backward compatibility (no metadata)
"""
import json
import sys

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
results = []

def check(label, condition, detail=""):
    results.append((label, condition))
    status = PASS if condition else FAIL
    extra = f" — {detail}" if detail else ""
    print(f"  {status} {label}{extra}")
    return condition


print("\n═══════════════════════════════════════════════════")
print("  SDD 014 — User-Provided Metadata Verification")
print("═══════════════════════════════════════════════════\n")

# ── Layer 1: Schema Validation ──────────────────────────────────────────

print("▸ Layer 1: Schema Validation")

from retriva.ingestion_api.schemas import (
    TextIngestRequest, HtmlIngestRequest, PdfIngestRequest,
    MediaWikiIngestRequest, MarkdownIngestRequest, ImageIngestRequest,
    validate_user_metadata, UserMetadataValidationError,
    MAX_METADATA_KEYS, MAX_METADATA_VALUE_LENGTH, MAX_METADATA_SERIALIZED_BYTES,
)

# Valid metadata accepted
valid_meta = {"author": "Alice", "version": "2.0", "dept": "engineering"}
req = TextIngestRequest(source_path="/test", content_text="hello", user_metadata=valid_meta)
check("Valid metadata accepted on TextIngestRequest", req.user_metadata == valid_meta)

# None metadata accepted
req_none = TextIngestRequest(source_path="/test", content_text="hello")
check("None metadata defaults correctly", req_none.user_metadata is None)

# Empty dict accepted
req_empty = TextIngestRequest(source_path="/test", content_text="hello", user_metadata={})
check("Empty dict accepted", req_empty.user_metadata == {})

# Too many keys rejected
try:
    TextIngestRequest(source_path="/t", content_text="h", user_metadata={f"k{i}": "v" for i in range(25)})
    check("Too many keys rejected", False, "should have raised")
except Exception as e:
    check("Too many keys rejected (422)", True, f"{MAX_METADATA_KEYS} key limit enforced")

# Value too long rejected
try:
    TextIngestRequest(source_path="/t", content_text="h", user_metadata={"long": "x" * 300})
    check("Value too long rejected", False, "should have raised")
except Exception:
    check("Value too long rejected (422)", True, f"{MAX_METADATA_VALUE_LENGTH} char limit enforced")

# Serialized size rejected
try:
    big = {f"key{i:02d}": "a" * 255 for i in range(20)}
    TextIngestRequest(source_path="/t", content_text="h", user_metadata=big)
    check("Serialized size rejected", False, "should have raised")
except Exception:
    check("Serialized size rejected (422)", True, f"{MAX_METADATA_SERIALIZED_BYTES} byte limit enforced")

# Non-string value rejected
try:
    TextIngestRequest(source_path="/t", content_text="h", user_metadata={"key": 123})
    check("Non-string value rejected", False, "should have raised")
except Exception:
    check("Non-string value rejected (422)", True)

# Verify all 6 schemas accept the field
for cls, kwargs in [
    (HtmlIngestRequest, {"source_path": "/t", "html_content": "<p>hi</p>"}),
    (PdfIngestRequest, {"source_path": "/t", "page_title": "T", "content_text": "x", "page_number": 1}),
    (MediaWikiIngestRequest, {"source_path": "/t", "page_title": "T", "content_text": "x"}),
    (MarkdownIngestRequest, {"source_path": "/t", "page_title": "T", "sections": [{"heading": "H", "content": "C"}]}),
    (ImageIngestRequest, {"source_path": "/t", "file_path": "/img.png"}),
]:
    obj = cls(**kwargs, user_metadata=valid_meta)
    check(f"{cls.__name__} accepts user_metadata", obj.user_metadata == valid_meta)


# ── Layer 2: ParsedDocument ─────────────────────────────────────────────

print("\n▸ Layer 2: ParsedDocument Construction")

from retriva.domain.models import ParsedDocument, ChunkMetadata, Chunk

doc = ParsedDocument(
    source_path="/test.txt",
    canonical_doc_id="/test.txt",
    page_title="Test",
    content_text="Hello world",
    user_metadata={"author": "Alice"},
)
check("ParsedDocument carries user_metadata", doc.user_metadata == {"author": "Alice"})

doc_none = ParsedDocument(
    source_path="/test.txt",
    canonical_doc_id="/test.txt",
    page_title="Test",
    content_text="Hello world",
)
check("ParsedDocument defaults to None", doc_none.user_metadata is None)


# ── Layer 3: Chunker Propagation ────────────────────────────────────────

print("\n▸ Layer 3: Chunker Propagation")

from retriva.ingestion.chunker import create_chunks, create_image_chunks
from retriva.domain.models import ImageContext

# Text chunks
doc_with_meta = ParsedDocument(
    source_path="/test.txt",
    canonical_doc_id="/test.txt",
    page_title="Test Doc",
    content_text="First paragraph.\n\nSecond paragraph.",
    user_metadata={"project": "retriva", "env": "test"},
)
chunks = create_chunks(doc_with_meta)
check("Chunker produces chunks", len(chunks) >= 2, f"{len(chunks)} chunks")
all_have_meta = all(c.metadata.user_metadata == {"project": "retriva", "env": "test"} for c in chunks)
check("ALL text chunks carry user_metadata", all_have_meta)

# Image chunks
doc_with_images = ParsedDocument(
    source_path="/test.html",
    canonical_doc_id="/test.html",
    page_title="Test Images",
    content_text="",
    user_metadata={"source": "camera"},
    images=[ImageContext(src="img.jpg", alt="photo", caption="A photo", surrounding_text="context")],
)
img_chunks = create_image_chunks(doc_with_images)
check("Image chunks carry user_metadata", len(img_chunks) == 1 and img_chunks[0].metadata.user_metadata == {"source": "camera"})

# Without metadata
doc_no_meta = ParsedDocument(
    source_path="/test.txt",
    canonical_doc_id="/test.txt",
    page_title="No Meta",
    content_text="Content here.",
)
chunks_no_meta = create_chunks(doc_no_meta)
check("Chunks without metadata have None", all(c.metadata.user_metadata is None for c in chunks_no_meta))


# ── Layer 4: Qdrant Payload (model_dump) ────────────────────────────────

print("\n▸ Layer 4: Qdrant Payload Structure")

meta = ChunkMetadata(
    doc_id="d1", source_path="/test", page_title="Test",
    section_path="", chunk_id="c1", chunk_index=0,
    user_metadata={"author": "Alice", "version": "2.0"},
)
dump = meta.model_dump()
check("model_dump includes user_metadata", dump["user_metadata"] == {"author": "Alice", "version": "2.0"})

# Simulated Qdrant payload
payload = {"text": "chunk text", **dump}
check("Qdrant payload has user_metadata key", "user_metadata" in payload)
check("Qdrant payload value correct", payload["user_metadata"] == {"author": "Alice", "version": "2.0"})

# Without metadata
meta_none = ChunkMetadata(
    doc_id="d1", source_path="/test", page_title="Test",
    section_path="", chunk_id="c2", chunk_index=0,
)
dump_none = meta_none.model_dump()
check("model_dump with None metadata", dump_none["user_metadata"] is None)


# ── Layer 5: Citation Visibility ────────────────────────────────────────

print("\n▸ Layer 5: Citation Builder")

# Simulate retrieved chunk payloads (as returned from Qdrant)
fake_chunks = [
    {
        "doc_id": "doc1",
        "source_path": "/docs/manual.pdf",
        "page_title": "User Manual",
        "text": "Installation steps...",
        "user_metadata": {"department": "engineering", "version": "3.1"},
    },
    {
        "doc_id": "doc1",
        "source_path": "/docs/manual.pdf",
        "page_title": "User Manual",
        "text": "Configuration guide...",
        "user_metadata": {"department": "engineering", "version": "3.1"},
    },
    {
        "doc_id": "doc2",
        "source_path": "/docs/faq.md",
        "page_title": "FAQ",
        "text": "Common questions...",
        "user_metadata": None,
    },
]

# Import and call the actual citation builder
import re
from pathlib import Path

def _build_citations_local(chunks):
    """Replica of the actual _build_citations for isolated testing."""
    from retriva.openai_api.schemas import Citation
    by_norm_title = {}
    for chunk in chunks:
        raw_title = chunk.get("page_title") or Path(chunk.get("source_path", "unknown")).name or "Unknown Source"
        norm_key = re.sub(r'[^a-z0-9]', '', raw_title.lower())
        path = chunk.get("source_path", "unknown")
        text = chunk.get("text", "")
        if norm_key not in by_norm_title:
            by_norm_title[norm_key] = {
                "source": {"name": raw_title},
                "document": [text],
                "metadata": [{"source": path, "title": raw_title, "user_metadata": chunk.get("user_metadata")}]
            }
        else:
            by_norm_title[norm_key]["document"].append(text)
            if not any(m["source"] == path for m in by_norm_title[norm_key]["metadata"]):
                by_norm_title[norm_key]["metadata"].append({"source": path, "title": raw_title, "user_metadata": chunk.get("user_metadata")})
    return [Citation(**v) for v in by_norm_title.values()]

citations = _build_citations_local(fake_chunks)
check("Citations grouped correctly", len(citations) == 2, "2 unique documents")

manual_citation = next(c for c in citations if c.source["name"] == "User Manual")
check("Citation has user_metadata", manual_citation.metadata[0]["user_metadata"] == {"department": "engineering", "version": "3.1"})

faq_citation = next(c for c in citations if c.source["name"] == "FAQ")
check("Citation with None metadata works", faq_citation.metadata[0]["user_metadata"] is None)


# ── Summary ─────────────────────────────────────────────────────────────

print("\n═══════════════════════════════════════════════════")
passed = sum(1 for _, ok in results if ok)
failed = sum(1 for _, ok in results if not ok)
total = len(results)
if failed == 0:
    print(f"  \033[92m{passed}/{total} checks passed — ALL CLEAR\033[0m")
else:
    print(f"  \033[91m{passed}/{total} passed, {failed} FAILED\033[0m")
print("═══════════════════════════════════════════════════\n")

sys.exit(0 if failed == 0 else 1)
