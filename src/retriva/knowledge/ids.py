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

"""Namespaced source identity (Spec 028 §4/§9; ADR-033 Decision 3).

Identity is ``namespace ":" normalized_external_ref``; the tenant is a
column, never embedded in the string.  Document identity is
``tenant + normalized namespaced source identity`` -- NEVER a content
hash.  Content fingerprints drive version identity and dedup
EVIDENCE, not document identity (Constitution §7).

Namespaces:

- ``upload``   -- stable per uploader-context + normalized filename;
  the raw client path is NOT identity.
- ``mediawiki``-- site identity + MediaWiki page id (page identity,
  NOT revision).
- ``connector``-- connector type + opaque external item id.
- ``internal`` -- server-chosen stable logical reference.
- ``path``     -- LEGACY/adoption evidence only (source_uri absolute
  paths recorded verbatim, normalized to forward slashes).
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

NAMESPACE_UPLOAD = "upload"
NAMESPACE_MEDIAWIKI = "mediawiki"
NAMESPACE_CONNECTOR = "connector"
NAMESPACE_INTERNAL = "internal"
NAMESPACE_PATH = "path"

_NAMESPACES = (
    NAMESPACE_UPLOAD, NAMESPACE_MEDIAWIKI, NAMESPACE_CONNECTOR,
    NAMESPACE_INTERNAL, NAMESPACE_PATH,
)

#: source_type values accepted by the schema, keyed by namespace.
_SOURCE_TYPE_BY_NAMESPACE = {
    NAMESPACE_UPLOAD: "upload",
    NAMESPACE_MEDIAWIKI: "mediawiki_export",
    NAMESPACE_CONNECTOR: "connector",
    NAMESPACE_INTERNAL: "internal",
    NAMESPACE_PATH: "adopted",
}

_MAX_COMPONENT = 200
_MAX_NORMALIZED_REF = 512
_SAFE = "-_.~"
_WS = re.compile(r"\s+")


class SourceIdentityError(ValueError):
    """Invalid or unsafe source identity input."""


@dataclass(frozen=True)
class SourceIdentity:
    """A resolved, normalized, namespaced source identity."""

    namespace: str
    normalized_ref: str
    source_type: str
    display_name: Optional[str] = None
    external_ref: Optional[str] = None
    connector_provider: Optional[str] = None

    @property
    def identity(self) -> str:
        return f"{self.namespace}:{self.normalized_ref}"


def new_id() -> str:
    """Opaque server-generated identifier (uuid4 hex, 32 chars)."""
    return uuid.uuid4().hex


def _clean(value: str, *, label: str) -> str:
    value = (value or "").strip()
    if not value:
        raise SourceIdentityError(f"{label} must not be empty")
    return value


def _encode_component(value: str, *, label: str) -> str:
    """Lowercase + percent-encode unsafe characters, bounded."""
    value = _clean(value, label=label)
    value = _WS.sub(" ", value).lower()
    encoded = quote(value, safe=_SAFE)
    if len(encoded) > _MAX_COMPONENT:
        encoded = encoded[:_MAX_COMPONENT]
    if not encoded:
        raise SourceIdentityError(f"{label} normalizes to empty")
    return encoded


def _finalize(namespace: str, normalized_ref: str, *,
              display_name: Optional[str] = None,
              external_ref: Optional[str] = None,
              connector_provider: Optional[str] = None,
              source_type: Optional[str] = None) -> SourceIdentity:
    if namespace not in _NAMESPACES:
        raise SourceIdentityError(
            f"unknown source namespace: {namespace!r}")
    if len(normalized_ref) > _MAX_NORMALIZED_REF:
        raise SourceIdentityError(
            f"normalized_ref exceeds {_MAX_NORMALIZED_REF} chars")
    if not normalized_ref:
        raise SourceIdentityError("normalized_ref must not be empty")
    return SourceIdentity(
        namespace=namespace,
        normalized_ref=normalized_ref,
        source_type=source_type
        or _SOURCE_TYPE_BY_NAMESPACE[namespace],
        display_name=(display_name or None),
        external_ref=(external_ref or None),
        connector_provider=(connector_provider or None),
    )


def upload_identity(kb_id: str, filename: str, *,
                    source_path: Optional[str] = None) -> SourceIdentity:
    """``upload:<hash(uploader-context)>:<normalized-filename>``.

    The uploader context is the target KB (stable per uploader); the
    raw client path is recorded only as display/evidence, never as
    identity.
    """
    context = hashlib.sha256(
        _clean(kb_id, label="kb_id").encode("utf-8")).hexdigest()[:16]
    name = filename or (source_path or "").rsplit("/", 1)[-1]
    normalized = _encode_component(name, label="upload filename")
    return _finalize(
        NAMESPACE_UPLOAD, f"{context}:{normalized}",
        display_name=filename or normalized,
        external_ref=source_path,
    )


def mediawiki_identity(site_identity: str, page_id: object, *,
                       display_name: Optional[str] = None
                       ) -> SourceIdentity:
    """``mediawiki:<site>:page:<page-id>`` (page identity, not
    revision).  The revision is recorded as version evidence."""
    site = _encode_component(site_identity, label="mediawiki site")
    page = _encode_component(str(page_id), label="mediawiki page id")
    return _finalize(
        NAMESPACE_MEDIAWIKI, f"{site}:page:{page}",
        display_name=display_name,
        external_ref=f"{site}:page:{page}",
    )


def connector_identity(connector_type: str, external_item_id: str, *,
                       connector_provider: Optional[str] = None,
                       display_name: Optional[str] = None
                       ) -> SourceIdentity:
    """``connector:<connector-type>:<external-item-id>`` (opaque,
    connector-owned)."""
    ctype = _encode_component(connector_type, label="connector type")
    item = _encode_component(
        external_item_id, label="connector item id")
    return _finalize(
        NAMESPACE_CONNECTOR, f"{ctype}:{item}",
        display_name=display_name,
        external_ref=external_item_id,
        connector_provider=connector_provider or connector_type,
    )


def internal_identity(logical_ref: str, *,
                      display_name: Optional[str] = None
                      ) -> SourceIdentity:
    """``internal:<stable-logical-reference>`` for programmatic
    sources."""
    ref = _encode_component(logical_ref, label="internal reference")
    return _finalize(NAMESPACE_INTERNAL, ref, display_name=display_name)


def legacy_path_identity(path: str, *,
                         display_name: Optional[str] = None
                         ) -> SourceIdentity:
    """LEGACY/adoption evidence: an absolute source path recorded
    verbatim (normalized to forward slashes, bounded).  NOT native
    permanent identity for new ingestion."""
    value = _clean(path, label="path").replace("\\", "/")
    if value.startswith("/"):
        value = value.lstrip("/")
    if len(value) > _MAX_NORMALIZED_REF:
        value = value[:_MAX_NORMALIZED_REF]
    return _finalize(
        NAMESPACE_PATH, value,
        display_name=display_name or path,
        external_ref=path,
        source_type="adopted",
    )


def normalize_source_identity(namespace: str, normalized_ref: str, *,
                              source_type: Optional[str] = None,
                              display_name: Optional[str] = None,
                              external_ref: Optional[str] = None,
                              connector_provider: Optional[str] = None
                              ) -> SourceIdentity:
    """Validate/reconstruct an identity from an already-normalized
    reference (used by adoption when reading evidence)."""
    return _finalize(
        namespace, normalized_ref,
        source_type=source_type,
        display_name=display_name,
        external_ref=external_ref,
        connector_provider=connector_provider,
    )


def derive_point_id(chunk_id_seed: str, ordinal: int, *,
                    chunk_type: str = "text") -> str:
    """Deterministic Qdrant point id.

    Preserves the historical derivation shape
    (``md5(seed_ordinal)`` / ``md5(seed_img_ordinal)``) so the native
    scheme stays reconcilable with legacy points; for native ingestion
    ``seed`` is the version's ``chunk_id_seed`` (NOT a content hash).
    """
    if ordinal < 0:
        raise SourceIdentityError("ordinal must be >= 0")
    suffix = f"_img_{ordinal}" if chunk_type == "image" else f"_{ordinal}"
    return hashlib.md5(
        f"{chunk_id_seed}{suffix}".encode("utf-8")).hexdigest()
