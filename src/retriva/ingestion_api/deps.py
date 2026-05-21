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
Shared FastAPI dependencies for ``ingestion_api``.

Currently provides:

- :func:`require_kb_exists`  — validate a single ``kb_id`` (Form/Query/path).
- :func:`require_kbs_exist`  — validate every entry of a ``kb_ids`` list.
- :func:`kb_error_to_http`   — centralized mapping of KB domain exceptions
  to ``HTTPException`` instances. Used by the ``v2_kbs`` router and by any
  consumer of the dependencies above that needs to surface KB errors.

Centralizing this mapping is mandated by SDD section "Existing endpoints —
required changes" to avoid drift across the many endpoints that need
KB-existence enforcement.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import HTTPException, status

from retriva.domain.kb import (
    KBConflictError,
    KBImmutableError,
    KBNotFoundError,
    KBRegistry,
    KBValidationError,
)


# ---------------------------------------------------------------------------
# Exception → HTTP mapping
# ---------------------------------------------------------------------------

def kb_error_to_http(exc: Exception) -> HTTPException:
    """Map a KB-domain exception to the appropriate ``HTTPException``.

    Unknown exception types are re-wrapped as ``500`` so callers can
    ``raise kb_error_to_http(e)`` defensively.
    """
    if isinstance(exc, KBNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        )
    if isinstance(exc, KBValidationError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        )
    if isinstance(exc, KBConflictError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        )
    if isinstance(exc, KBImmutableError):
        # 409 Conflict is the closest standard match: the request conflicts
        # with the current state of the target resource (the default KB
        # cannot be deleted).
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
    )


# ---------------------------------------------------------------------------
# Validation dependencies
# ---------------------------------------------------------------------------

def require_kb_exists(kb_id: str) -> str:
    """Validate that a single ``kb_id`` exists in the registry.

    Designed to be called directly from inside an endpoint (not via FastAPI's
    ``Depends``) because the ``kb_id`` is typically obtained from a
    multipart Form, a path parameter, or a request body, and centralizing
    the look-up through ``Depends`` would require duplicating extraction
    logic for each call site.

    Returns the ``kb_id`` unchanged on success so callers can write
    ``kb_id = require_kb_exists(kb_id)`` for clarity.
    """
    registry = KBRegistry()
    try:
        registry.get(kb_id)
    except (KBNotFoundError, KBValidationError) as exc:
        raise kb_error_to_http(exc) from exc
    return kb_id


def require_kbs_exist(
    kb_ids: Optional[List[str]],
    *,
    allow_empty: bool = True,
) -> List[str]:
    """Validate every entry of a ``kb_ids`` list.

    Semantics
    ---------
    - ``kb_ids is None`` is treated as "not provided"; behavior is governed
      by ``allow_empty``.
    - ``kb_ids == []`` is also "not provided" from the caller's perspective;
      same as above.
    - When ``allow_empty=True`` and the list is empty/None, returns ``[]``
      to signal "no KB scope" (existing semantics of search/filter
      endpoints, which fan out across all KBs in that case).
    - When ``allow_empty=False`` and the list is empty/None, raises ``422``.
    - Any unknown ``kb_id`` triggers a single ``404`` that names the first
      offending id (deterministic, easy to debug).

    Returns the original list unchanged on success.
    """
    if kb_ids is None or len(kb_ids) == 0:
        if allow_empty:
            return []
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="kb_ids must contain at least one entry",
        )

    registry = KBRegistry()
    # Validate each id; surface the first failure with a precise message.
    # We iterate through all so a single 404 covers the whole list — the
    # alternative (per-id 404s) is noisier without being more useful.
    missing: List[str] = []
    invalid: List[str] = []
    for kb_id in kb_ids:
        try:
            if not registry.exists(kb_id):
                missing.append(kb_id)
        except KBValidationError:
            invalid.append(kb_id)

    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid kb_id(s): {invalid}",
        )
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown kb_id(s): {missing}",
        )
    return kb_ids
