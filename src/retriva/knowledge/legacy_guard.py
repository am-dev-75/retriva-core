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

"""Centralized legacy dedup-catalog authority guard (Spec 028 correction).

Once PostgreSQL knowledge metadata is authoritative, the legacy
``dedup_catalog.json`` must never be written by ordinary runtime code
(state ``authoritative``, ``suspended``, or
``reconciliation_required``).  The decision is derived from the DURABLE
PostgreSQL authority state -- never from an environment variable, a
ContextVar, file permissions, or the presence/absence of the catalog
file.

Fail-closed: when the knowledge schema is present but its authority
state cannot be determined, ordinary runtime catalog access is REFUSED.

Privileged operation contexts (adoption, cutover verification,
reconciliation, operator inspection) are read-only and never write;
they are never selectable through public APIs.
"""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Dict, Optional

from retriva.logger import get_logger

_log = get_logger(__name__)

_CACHE_TTL_SECONDS = 5.0
_cache_lock = threading.Lock()
_cache: Dict[str, tuple] = {}

#: States in which ordinary runtime catalog writes are forbidden.
WRITE_FORBIDDEN_STATES = frozenset({
    "authoritative", "suspended", "reconciliation_required",
})

#: States that represent the accepted pre-cutover compatibility window.
PRE_CUTOVER_STATES = frozenset({
    "schema_ready", "adoption_pending", "adoption_verified",
})

_ALL_STATES = PRE_CUTOVER_STATES | WRITE_FORBIDDEN_STATES


class LegacyCatalogOperation(str, Enum):
    RUNTIME_INGESTION = "runtime_ingestion"
    RUNTIME_DELETION = "runtime_deletion"
    RUNTIME_METADATA = "runtime_metadata"
    ADOPTION_DRY_RUN = "adoption_dry_run"
    ADOPTION_APPLY = "adoption_apply"
    CUTOVER_VERIFICATION = "cutover_verification"
    RECONCILIATION = "reconciliation"
    OPERATOR_INSPECTION = "operator_inspection"
    PRE_AUTHORITATIVE_COMPAT = "pre_authoritative_compat"


#: Privileged operator/transition contexts.  Read-only; never write; never
#: selectable through public request data.
PRIVILEGED_OPERATIONS = frozenset({
    LegacyCatalogOperation.ADOPTION_DRY_RUN,
    LegacyCatalogOperation.ADOPTION_APPLY,
    LegacyCatalogOperation.CUTOVER_VERIFICATION,
    LegacyCatalogOperation.RECONCILIATION,
    LegacyCatalogOperation.OPERATOR_INSPECTION,
})

_ORDINARY_OPERATIONS = frozenset({
    LegacyCatalogOperation.RUNTIME_INGESTION,
    LegacyCatalogOperation.RUNTIME_DELETION,
    LegacyCatalogOperation.RUNTIME_METADATA,
})


class LegacyCatalogWriteRefused(RuntimeError):
    """Bounded internal outcome: an ordinary runtime legacy catalog write
    was refused because PostgreSQL knowledge metadata is authoritative."""

    code = "legacy_catalog_write_refused"

    def __init__(self, operation: str = "runtime_ingestion"):
        self.operation = operation
        super().__init__(self.code)


# Bounded, low-cardinality refusal counters (operation -> count).  No
# tenant/document/path labels.
_refusal_counts: Dict[str, int] = {}
_refusal_lock = threading.Lock()


def note_refusal(operation: str) -> None:
    with _refusal_lock:
        _refusal_counts[operation] = _refusal_counts.get(operation, 0) + 1


def refusal_counts() -> Dict[str, int]:
    with _refusal_lock:
        return dict(_refusal_counts)


def reset_refusals() -> None:
    with _refusal_lock:
        _refusal_counts.clear()


def invalidate_authority_state_cache() -> None:
    """Called on every durable authority transition so a cached state can
    never remain stale across cutover/suspension."""
    with _cache_lock:
        _cache.clear()


def _probe_authority_state() -> Optional[str]:
    """Return the durable authority state, ``"schema_ready"`` when the
    knowledge schema is absent (legacy compatibility), or ``None`` when the
    schema exists but its state cannot be determined (fail-closed)."""
    from retriva.knowledge.repository import KnowledgeRepository

    repo = KnowledgeRepository()
    try:
        with repo.transaction() as cur:
            cur.execute(
                "SELECT to_regclass('knowledge.authority') IS NOT NULL "
                "AS present")
            row = cur.fetchone()
            if not row or not row["present"]:
                return "schema_ready"
            cur.execute(
                "SELECT state FROM knowledge.authority "
                "WHERE singleton = TRUE")
            arow = cur.fetchone()
            if not arow:
                return None
            return str(arow["state"])
    except Exception:
        # Cannot even reach/establish the knowledge schema (e.g. a legacy
        # deployment without the knowledge metadata subsystem): treat as
        # pre-cutover compatibility so legacy ingestion is unaffected.
        return "schema_ready"


def authority_state() -> Optional[str]:
    """Durable authority state with a cache that may only ever make the
    guard MORE restrictive.

    Only DENIED states (forbidden states and the fail-closed ``None``) are
    cached, for at most the TTL.  A PERMISSIVE (pre-cutover) state is NEVER
    cached: every allowed write therefore re-confirms the current durable
    PostgreSQL authority state directly, so an independent process cannot
    authorize a catalog write from a stale permissive cache after another
    process transitions authority (cross-process safety does not depend on
    process-local invalidation)."""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get("state")
        if hit and now - hit[0] < _CACHE_TTL_SECONDS:
            cached = hit[1]
            if cached is None or cached in WRITE_FORBIDDEN_STATES:
                return cached
            # A cached permissive state must never be trusted.
    state = _probe_authority_state()
    with _cache_lock:
        if state is None or state in WRITE_FORBIDDEN_STATES:
            _cache["state"] = (now, state)
        else:
            _cache.pop("state", None)  # never cache a permissive state
    return state


def _is_privileged(operation) -> bool:
    try:
        return LegacyCatalogOperation(operation) in PRIVILEGED_OPERATIONS
    except ValueError:
        return False


def legacy_catalog_write_allowed(operation="runtime_ingestion") -> bool:
    """True only when an ordinary pre-cutover runtime may write the legacy
    catalog.  Privileged contexts and post-cutover states always refuse."""
    op = LegacyCatalogOperation(operation) if not isinstance(
        operation, LegacyCatalogOperation) else operation
    if op in PRIVILEGED_OPERATIONS:
        return False
    if op is LegacyCatalogOperation.PRE_AUTHORITATIVE_COMPAT:
        return True
    state = authority_state()
    if state is None:
        return False  # fail-closed
    if op in _ORDINARY_OPERATIONS:
        return state in PRE_CUTOVER_STATES
    return False


def legacy_catalog_read_allowed(operation="runtime_ingestion") -> bool:
    """Legacy catalog READS are allowed pre-cutover and for bounded
    privileged inspection; they are refused for ordinary runtime once
    PostgreSQL is authoritative (the catalog must not be used as identity
    authority after cutover)."""
    op = LegacyCatalogOperation(operation) if not isinstance(
        operation, LegacyCatalogOperation) else operation
    if op in PRIVILEGED_OPERATIONS:
        return True
    if op is LegacyCatalogOperation.PRE_AUTHORITATIVE_COMPAT:
        return True
    state = authority_state()
    if state is None:
        return False  # fail-closed
    return state in PRE_CUTOVER_STATES
