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

"""Version-aware Qdrant serving visibility (Spec 028 §6/§13; ADR-033
Decision 5).

Every native/adopted point carries bounded top-level payload fields:
``tenant_id``, ``kb_ids``, ``document_id``, ``version_id``,
``serving``, ``serving_generation``, ``provenance_class``.  Ordinary
retrieval excludes points explicitly marked ``serving=false`` while
keeping unmarked legacy points visible via an ``IsEmpty(serving)``
branch.  This is a STATIC per-search filter: NO PostgreSQL query runs
per search or per result.  Qdrant ``serving=true`` is derived
evidence and never overrides PostgreSQL current-version authority.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

from retriva.logger import get_logger

_log = get_logger(__name__)

FIELD_TENANT = "tenant_id"
FIELD_KB_IDS = "kb_ids"
FIELD_DOCUMENT = "document_id"
FIELD_VERSION = "version_id"
FIELD_SERVING = "serving"
FIELD_SERVING_GENERATION = "serving_generation"
FIELD_PROVENANCE = "provenance_class"

#: The four payload indexes required by the accepted design.
INDEXED_FIELDS = (
    (FIELD_VERSION, "keyword"),
    (FIELD_SERVING, "bool"),
    (FIELD_DOCUMENT, "keyword"),
    (FIELD_KB_IDS, "keyword"),
)

#: Fields that must be present on every retrieval-visible point before
#: authority cutover is allowed.
REQUIRED_CONTRACT_FIELDS = (
    FIELD_TENANT, FIELD_DOCUMENT, FIELD_VERSION, FIELD_SERVING,
    FIELD_KB_IDS, FIELD_PROVENANCE,
)


def _models():
    from qdrant_client import models

    return models


def native_payload_fields(*, tenant_id: str, document_id: str,
                          version_id: str, kb_ids: Sequence[str],
                          serving: bool,
                          serving_generation: int = 1,
                          provenance_class: str = "native"
                          ) -> Dict[str, Any]:
    """Bounded top-level visibility fields added to native points."""
    return {
        FIELD_TENANT: tenant_id,
        FIELD_DOCUMENT: document_id,
        FIELD_VERSION: version_id,
        FIELD_SERVING: bool(serving),
        FIELD_SERVING_GENERATION: int(serving_generation),
        FIELD_PROVENANCE: provenance_class,
        FIELD_KB_IDS: [k for k in (kb_ids or []) if k],
    }


def serving_should_conditions() -> List[Any]:
    """The static serving clause: ``serving=true OR serving absent``.

    Points explicitly marked ``serving=false`` (replacement versions
    under construction, superseded versions pending cleanup) are
    excluded; pre-adoption legacy points with no ``serving`` field stay
    visible."""
    models = _models()
    return [
        models.FieldCondition(
            key=FIELD_SERVING,
            match=models.MatchValue(value=True)),
        models.IsEmptyCondition(
            is_empty=models.PayloadField(key=FIELD_SERVING)),
    ]


def with_serving_clause(qdrant_filter: Any,
                        *, authoritative: bool = False) -> Any:
    """Return a filter that additionally enforces the serving rule.

    Pre-authoritative: ``serving=true OR serving absent`` (compatibility
    for legacy/adopted points).  Authoritative: ``serving=true`` only,
    and points missing the required ``tenant_id`` are excluded — the
    missing-``serving`` compatibility branch is DISABLED after cutover."""
    models = _models()
    if authoritative:
        must = list(getattr(qdrant_filter, "must", None) or []) if \
            qdrant_filter is not None else []
        must.append(models.FieldCondition(
            key=FIELD_SERVING, match=models.MatchValue(value=True)))
        must_not = list(getattr(qdrant_filter, "must_not", None) or []) \
            if qdrant_filter is not None else []
        must_not.append(models.IsEmptyCondition(
            is_empty=models.PayloadField(key=FIELD_TENANT)))
        return models.Filter(must=must, must_not=must_not)
    should = serving_should_conditions()
    if qdrant_filter is None:
        return models.Filter(should=should)
    existing = list(getattr(qdrant_filter, "should", None) or [])
    return models.Filter(
        must=list(getattr(qdrant_filter, "must", None) or []) or None,
        should=existing + should,
        must_not=list(getattr(qdrant_filter, "must_not", None) or []) or None,
    )


_AUTH_CACHE: Dict[str, tuple] = {}
_AUTH_TTL = 5.0


def is_authoritative() -> bool:
    """Best-effort cached read of the knowledge authority state (fail
    closed to False).  One bounded PostgreSQL read at most every few
    seconds; never per Qdrant result."""
    import time
    now = time.monotonic()
    hit = _AUTH_CACHE.get("v")
    if hit and now - hit[0] < _AUTH_TTL:
        return hit[1]
    value = False
    try:
        from retriva.knowledge.authority import (
            AuthorityState, KnowledgeAuthority,
        )
        value = KnowledgeAuthority().read_state() is \
            AuthorityState.AUTHORITATIVE
    except Exception:
        value = False
    _AUTH_CACHE["v"] = (now, value)
    return value


def incomplete_payload_filter() -> Any:
    """Points missing ANY required contract field (for cutover gating)."""
    models = _models()
    should = [
        models.IsEmptyCondition(
            is_empty=models.PayloadField(key=field))
        for field in REQUIRED_CONTRACT_FIELDS
    ]
    return models.Filter(should=should)


def ensure_payload_indexes(client, collection_name: str) -> List[str]:
    """Create the four verified payload indexes (idempotent)."""
    models = _models()
    created: List[str] = []
    for field, kind in INDEXED_FIELDS:
        schema = {
            "keyword": models.PayloadSchemaType.KEYWORD,
            "bool": models.PayloadSchemaType.BOOL,
        }[kind]
        try:
            client.create_payload_index(
                collection_name=collection_name, field_name=field,
                field_schema=schema, wait=True)
            created.append(field)
        except Exception as exc:  # already exists / transient
            _log.debug(
                "payload index %s on %s: %s", field, collection_name,
                exc.__class__.__name__)
    return created


def version_filter(version_id: str) -> Any:
    models = _models()
    return models.Filter(must=[
        models.FieldCondition(
            key=FIELD_VERSION,
            match=models.MatchValue(value=version_id))])


def document_filter(document_id: str) -> Any:
    models = _models()
    return models.Filter(must=[
        models.FieldCondition(
            key=FIELD_DOCUMENT,
            match=models.MatchValue(value=document_id))])


def set_serving(client, collection_name: str, version_id: str,
                serving: bool, *, wait: bool = True) -> None:
    """Flip all points of one version to ``serving`` (bounded by the
    version_id payload index).  Vector values and ids are untouched."""
    client.set_payload(
        collection_name=collection_name,
        payload={FIELD_SERVING: bool(serving)},
        points=version_filter(version_id),
        wait=wait)


def count_version_points(client, collection_name: str,
                         version_id: str) -> int:
    result = client.count(
        collection_name=collection_name,
        count_filter=version_filter(version_id), exact=True)
    return int(getattr(result, "count", result) or 0)


def point_ids_for_version(client, collection_name: str,
                          version_id: str,
                          limit: int = 10000) -> List[str]:
    ids: List[str] = []
    offset = None
    while True:
        records, offset = client.scroll(
            collection_name=collection_name,
            scroll_filter=version_filter(version_id),
            limit=min(1000, limit),
            offset=offset, with_payload=False, with_vectors=False)
        ids.extend(str(r.id) for r in records)
        if offset is None or len(ids) >= limit:
            break
    return ids


def delete_version_points(client, collection_name: str,
                          version_id: str, *, wait: bool = True) -> None:
    client.delete(
        collection_name=collection_name,
        points_selector=version_filter(version_id), wait=wait)


def patch_point_payload(client, collection_name: str, point_id: str,
                        payload: Dict[str, Any], *,
                        wait: bool = True) -> None:
    """Metadata-only payload patch for one adopted point.

    Never touches vectors or point ids (adoption never rewrites point
    ids)."""
    client.set_payload(
        collection_name=collection_name, payload=payload,
        points=[point_id], wait=wait)


def scroll_points(client, collection_name: str, *, batch: int = 256,
                  offset: Any = None, with_payload: bool = True):
    return client.scroll(
        collection_name=collection_name, limit=batch, offset=offset,
        with_payload=with_payload, with_vectors=False)


def count_incomplete_visible_points(client, collection_name: str) -> int:
    """Count points lacking required contract fields (cutover gate)."""
    result = client.count(
        collection_name=collection_name,
        count_filter=incomplete_payload_filter(), exact=True)
    return int(getattr(result, "count", result) or 0)


def assert_native_payload_contract(payload: Dict[str, Any]) -> None:
    missing = [f for f in REQUIRED_CONTRACT_FIELDS if f not in payload]
    if missing:
        raise ValueError(
            "point payload missing required contract fields: "
            + ",".join(sorted(missing)))
