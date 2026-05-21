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
Knowledge Base CRUD endpoints — Phase 2 of the KB SDD.

Endpoints (mounted at ``/api/v2/kbs``):

- ``GET    /``            — list KBs (with document_count)
- ``POST   /``            — create a KB
- ``GET    /{kb_id}``     — fetch a single KB
- ``PATCH  /{kb_id}``     — update mutable fields
- ``DELETE /{kb_id}``     — delete a KB (cascade wired in Phase 3)

Notes
-----
- ``document_count`` is computed at request time by counting unique
  ``doc_id`` payload values in the Qdrant collection filtered by ``kb_id``.
  Errors here are tolerated (logged + treated as 0) so a Qdrant outage does
  not break the KB list page.
- All domain exceptions are translated to HTTP via
  :func:`retriva.ingestion_api.deps.kb_error_to_http`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from retriva.domain.kb import (
    KBError,
    KBNotFoundError,
    KBRegistry,
    KBRecord,
)
from retriva.ingestion_api.deps import kb_error_to_http
from retriva.indexing.qdrant_store import (
    count_documents as count_documents_store,
    delete_chunks_by_kb_id,
    get_client,
)
from retriva.ingestion.dedup import DeduplicationStore
from retriva.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v2/kbs", tags=["v2-kbs"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class KBCreateRequest(BaseModel):
    """Create-KB request body. ``kb_id`` is optional (RD-1)."""

    kb_id: Optional[str] = Field(
        None,
        description=(
            "Optional explicit id matching ^[a-z0-9][a-z0-9_-]{0,63}$. "
            "If omitted, derived from `name` via server-side slugification."
        ),
    )
    name: str = Field(..., description="Human-readable label (1-128 chars).")
    description: Optional[str] = Field(
        None, description="Optional description (up to 1024 chars)."
    )
    settings: Optional[Dict[str, Any]] = Field(
        None, description="Reserved for future per-KB settings. Stored verbatim."
    )


class KBUpdateRequest(BaseModel):
    """Patch-KB request body. All fields optional; only supplied ones change."""

    name: Optional[str] = None
    description: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None


class KBResponse(BaseModel):
    """Public KB representation (one of the entries in ``KBListResponse``)."""

    kb_id: str
    name: str
    description: Optional[str] = None
    created_at: str
    updated_at: str
    settings: Dict[str, Any] = Field(default_factory=dict)
    document_count: int = 0

    @classmethod
    def from_record(cls, rec: KBRecord, *, document_count: int = 0) -> "KBResponse":
        return cls(
            kb_id=rec.kb_id,
            name=rec.name,
            description=rec.description,
            created_at=rec.created_at,
            updated_at=rec.updated_at,
            settings=rec.settings,
            document_count=document_count,
        )


class KBListResponse(BaseModel):
    kbs: List[KBResponse]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_documents_for_kb(kb_id: str) -> int:
    """Count distinct doc_ids in the Qdrant collection for a given kb_id.

    Designed to be tolerant of vector-layer failures: returns 0 on any
    exception and logs the cause. The KB listing UI then shows ``0``
    instead of a broken page.
    """
    try:
        client = get_client()
        # ``count_documents_store`` already supports a metadata_filter param
        # that maps to Qdrant payload filters. We pass kb_id via this path
        # to avoid adding a dedicated ``count_by_kb_id`` helper. Phase 3 may
        # introduce one if the filter shape outgrows the dict format.
        return count_documents_store(client, metadata_filter={"kb_id": kb_id})
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            f"document_count_unavailable: kb_id={kb_id} reason={exc!r}"
        )
        return 0


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=KBListResponse)
async def list_kbs() -> KBListResponse:
    """List all KBs with their current document counts."""
    registry = KBRegistry()
    records = registry.list()
    return KBListResponse(
        kbs=[
            KBResponse.from_record(
                rec, document_count=_count_documents_for_kb(rec.kb_id)
            )
            for rec in records
        ]
    )


@router.post("", response_model=KBResponse, status_code=status.HTTP_201_CREATED)
async def create_kb(payload: KBCreateRequest) -> KBResponse:
    """Create a new KB.

    Validation (RD-1):
    - ``kb_id`` if provided must match ``^[a-z0-9][a-z0-9_-]{0,63}$``.
    - Otherwise it is derived from ``name`` by slugification.
    - Collisions return ``409 Conflict``; no silent UUID suffixing.
    """
    registry = KBRegistry()
    try:
        rec = registry.create(
            kb_id=payload.kb_id,
            name=payload.name,
            description=payload.description,
            settings=payload.settings,
        )
    except KBError as exc:
        raise kb_error_to_http(exc) from exc

    return KBResponse.from_record(rec, document_count=0)


@router.get("/{kb_id}", response_model=KBResponse)
async def get_kb(kb_id: str) -> KBResponse:
    """Fetch a single KB. Returns 404 if unknown."""
    registry = KBRegistry()
    try:
        rec = registry.get(kb_id)
    except KBError as exc:
        raise kb_error_to_http(exc) from exc

    return KBResponse.from_record(
        rec, document_count=_count_documents_for_kb(rec.kb_id)
    )


@router.patch("/{kb_id}", response_model=KBResponse)
async def update_kb(kb_id: str, payload: KBUpdateRequest) -> KBResponse:
    """Update mutable fields. ``kb_id`` is immutable.

    Only fields explicitly present in the body are applied. To clear the
    description, send an empty string (``""``); ``null`` / omission leaves
    it unchanged.
    """
    registry = KBRegistry()
    try:
        rec = registry.update(
            kb_id,
            name=payload.name,
            description=payload.description,
            settings=payload.settings,
        )
    except KBError as exc:
        raise kb_error_to_http(exc) from exc

    return KBResponse.from_record(
        rec, document_count=_count_documents_for_kb(rec.kb_id)
    )


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kb(kb_id: str) -> None:
    """Delete a KB and cascade to its vector points and dedup records.

    Cascade order (SDD Phase 3):

    1. **Refuse early** if ``kb_id == "default"`` — the registry layer
       raises ``KBImmutableError`` and we map it to 409 *before* touching
       any other store. Verifying existence first also gives a clean 404
       for unknown KBs without performing any cascade work.
    2. **Qdrant points**: filtered delete by ``kb_id`` payload. Largest
       blast radius; done first so a mid-flight failure leaves only
       dangling dedup/registry rows (recoverable) rather than orphaned
       points that still surface in search.
    3. **Deduplication catalog**: purge every record whose ``kb_id``
       matches.
    4. **Registry row**: remove the row from ``knowledge_bases``.

    The cascade is **not** wrapped in a distributed transaction. The order
    guarantees that any partial failure leaves the system in a state that
    an offline reconciler can repair (out of scope for this SDD).
    """
    from retriva.domain.kb import DEFAULT_KB_ID, KBImmutableError

    registry = KBRegistry()

    # Step 1a — short-circuit the immutability invariant *before* touching
    # any store. Re-checking in step 4 would still work, but only after
    # destroying the default KB's points and dedup records — which is
    # exactly what the invariant exists to prevent.
    if kb_id == DEFAULT_KB_ID:
        raise kb_error_to_http(
            KBImmutableError("The 'default' KB cannot be deleted.")
        )

    # Step 1b — 404 for unknown KBs, before any cascade work.
    try:
        registry.get(kb_id)
    except KBError as exc:
        raise kb_error_to_http(exc) from exc

    # Step 2 — Qdrant points.
    try:
        client = get_client()
        points_removed = delete_chunks_by_kb_id(client, kb_id)
    except Exception as exc:
        logger.error(
            f"kb_delete_failed_qdrant: kb_id={kb_id} reason={exc!r}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete vector points for kb_id={kb_id}: {exc}",
        ) from exc

    # Step 3 — dedup records.
    try:
        dedup_removed = DeduplicationStore().delete_by_kb_id(kb_id)
    except Exception as exc:
        # The points are already gone; report the partial-failure state.
        logger.error(
            f"kb_delete_failed_dedup: kb_id={kb_id} reason={exc!r} "
            f"points_removed={points_removed}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Vector points deleted but dedup cleanup failed for "
                f"kb_id={kb_id}: {exc}"
            ),
        ) from exc

    # Step 4 — registry row. This is where KBImmutableError (default KB)
    # actually fires; it is by design that we re-check it here rather than
    # in step 1, because the SDD specifies the immutability invariant lives
    # in the domain layer and we want a single enforcement site.
    try:
        registry.delete(kb_id)
    except KBError as exc:
        logger.error(
            f"kb_delete_failed_registry: kb_id={kb_id} reason={exc!r} "
            f"points_removed={points_removed} dedup_removed={dedup_removed}"
        )
        raise kb_error_to_http(exc) from exc

    logger.info(
        f"kb_deleted: kb_id={kb_id} points_removed={points_removed} "
        f"dedup_removed={dedup_removed}"
    )
    return None
