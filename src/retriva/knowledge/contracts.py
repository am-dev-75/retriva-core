# Copyright (C) 2026 Retriva.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.  See the License for the specific language governing
# permissions and limitations under the License.

"""Processing-contract version identifiers (Spec 028 §10).

Version identity = logical document + content fingerprint + processing
contract.  The processing contract is (parser/extractor contract,
embedding contract).  These helpers produce the bounded, deterministic
stamp written into ``knowledge.document_versions`` so that a change in
parser or embedding model produces a NEW version when output may
differ.
"""

from __future__ import annotations

from retriva.config import settings

#: Chunker contract: max chars / overlap / the id-derivation scheme.
#: Changing any of these can change the number or identity of points;
#: bump when the derivation or sizing changes.
_CHUNK_SCHEME = "md5-seed-ordinal-v1"


def embedding_contract_version() -> str:
    """Embedding contract stamp: model id + dimension + scheme.

    Fixes the current absence of an embedding-model stamp on vectors
    (architecture §2 fact 4): the stamp participates in version
    identity, so switching models creates a new version rather than
    silently mutating an existing one.
    """
    return (f"{settings.embedding_model}"
            f"|d{settings.embedding_dimension}|vec1")


def parser_contract_version(parser_name: str = "default") -> str:
    """Parser/extractor contract stamp.  ``parser_name`` is the
    parser identity reported by the ingestion pipeline (for example
    ``docling`` or ``default``)."""
    name = (parser_name or "default").strip().lower() or "default"
    return f"{name}|contract1"


def chunk_contract_version() -> str:
    """Chunker contract stamp embedded in version identity: sizing
    plus the point-id derivation scheme."""
    return (f"{_CHUNK_SCHEME}"
            f"|c{settings.max_chunk_chars}"
            f"|o{settings.chunk_overlap}")
