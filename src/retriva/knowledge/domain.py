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

"""Knowledge-domain state machines (Spec 028 §12).

State ownership is separate from the durable jobs subsystem: the
durable jobs machine (Spec 025 §3.2) remains the ONLY asynchronous
lifecycle authority; the knowledge domain states below describe
relational evidence and Qdrant visibility.  Neither is derived from
the other and no job-event log is copied into knowledge tables.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet, Optional


class TransitionError(ValueError):
    """An illegal domain-state transition was attempted."""


class VersionStatus(str, Enum):
    STAGING = "staging"
    PARSING = "parsing"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    INDEXED = "indexed"
    INDEX_PARTIAL = "index_partial"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    RETIRED = "retired"


class IngestionSyncState(str, Enum):
    REGISTERED = "registered"
    PARSING = "parsing"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    INDEXED = "indexed"
    INDEX_PARTIAL = "index_partial"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    DELETE_PENDING = "delete_pending"
    DELETED = "deleted"
    FAILED = "failed"


class IngestionMode(str, Enum):
    CREATE = "create"
    REINGEST = "reingest"
    METADATA_UPDATE = "metadata_update"
    ADOPT = "adopt"
    REPAIR = "repair"


class DocumentLifecycle(str, Enum):
    ACTIVE = "active"
    DELETE_PENDING = "delete_pending"
    DELETED = "deleted"
    RETENTION_HOLD = "retention_hold"


class OpType(str, Enum):
    UPSERT_BATCH = "upsert_batch"
    DELETE_POINTS = "delete_points"
    DELETE_DOCUMENT = "delete_document"
    DELETE_KB = "delete_kb"
    PAYLOAD_PATCH = "payload_patch"
    ADOPT_VERIFY = "adopt_verify"


class OpState(str, Enum):
    PREPARED = "prepared"
    EXECUTING = "executing"
    APPLIED_UNVERIFIED = "applied_unverified"
    VERIFIED = "verified"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class Provenance(str, Enum):
    NATIVE = "native"
    ADOPTED_VERIFIED = "adopted_verified"
    ADOPTED_UNCERTAIN = "adopted_uncertain"


#: Version status transitions.  Terminal states appear with empty
#: out-sets.  ``indexed`` -> ``superseded`` happens only through the
#: promotion transaction; ``superseded`` -> ``retired`` through
#: verified cleanup.
_VERSION_TRANSITIONS: Dict[VersionStatus, FrozenSet[VersionStatus]] = {
    VersionStatus.STAGING: frozenset({
        VersionStatus.PARSING, VersionStatus.INDEXED,
        VersionStatus.FAILED,
        VersionStatus.INDEX_PARTIAL, VersionStatus.SUPERSEDED}),
    VersionStatus.PARSING: frozenset({
        VersionStatus.EMBEDDING, VersionStatus.FAILED,
        VersionStatus.INDEX_PARTIAL}),
    VersionStatus.EMBEDDING: frozenset({
        VersionStatus.INDEXING, VersionStatus.FAILED,
        VersionStatus.INDEX_PARTIAL}),
    VersionStatus.INDEXING: frozenset({
        VersionStatus.INDEXED, VersionStatus.FAILED,
        VersionStatus.INDEX_PARTIAL}),
    VersionStatus.INDEXED: frozenset({VersionStatus.SUPERSEDED}),
    VersionStatus.INDEX_PARTIAL: frozenset({
        VersionStatus.INDEXING, VersionStatus.FAILED,
        VersionStatus.SUPERSEDED}),
    VersionStatus.FAILED: frozenset({VersionStatus.SUPERSEDED}),
    VersionStatus.SUPERSEDED: frozenset({VersionStatus.RETIRED}),
    VersionStatus.RETIRED: frozenset(),
}

#: Ingestion sync-state transitions.  Late/duplicate callbacks are
#: no-ops (a transition to the current state is always allowed).
_INGESTION_TRANSITIONS: Dict[
        IngestionSyncState,
        FrozenSet[IngestionSyncState]] = {
    IngestionSyncState.REGISTERED: frozenset({
        IngestionSyncState.PARSING, IngestionSyncState.INDEXING,
        IngestionSyncState.INDEXED, IngestionSyncState.FAILED,
        IngestionSyncState.DELETE_PENDING,
        IngestionSyncState.INDEX_PARTIAL,
        IngestionSyncState.RECONCILIATION_REQUIRED}),
    IngestionSyncState.PARSING: frozenset({
        IngestionSyncState.EMBEDDING, IngestionSyncState.INDEXING,
        IngestionSyncState.INDEXED, IngestionSyncState.FAILED,
        IngestionSyncState.INDEX_PARTIAL,
        IngestionSyncState.DELETE_PENDING}),
    IngestionSyncState.EMBEDDING: frozenset({
        IngestionSyncState.INDEXING, IngestionSyncState.INDEXED,
        IngestionSyncState.FAILED, IngestionSyncState.INDEX_PARTIAL,
        IngestionSyncState.DELETE_PENDING}),
    IngestionSyncState.INDEXING: frozenset({
        IngestionSyncState.INDEXED, IngestionSyncState.FAILED,
        IngestionSyncState.INDEX_PARTIAL,
        IngestionSyncState.RECONCILIATION_REQUIRED,
        IngestionSyncState.DELETE_PENDING}),
    IngestionSyncState.INDEXED: frozenset({
        IngestionSyncState.DELETE_PENDING}),
    IngestionSyncState.INDEX_PARTIAL: frozenset({
        IngestionSyncState.INDEXING, IngestionSyncState.INDEXED,
        IngestionSyncState.FAILED,
        IngestionSyncState.RECONCILIATION_REQUIRED,
        IngestionSyncState.DELETE_PENDING}),
    IngestionSyncState.RECONCILIATION_REQUIRED: frozenset({
        IngestionSyncState.INDEXING, IngestionSyncState.INDEXED,
        IngestionSyncState.FAILED,
        IngestionSyncState.DELETE_PENDING}),
    IngestionSyncState.DELETE_PENDING: frozenset({
        IngestionSyncState.DELETED,
        IngestionSyncState.RECONCILIATION_REQUIRED}),
    IngestionSyncState.DELETED: frozenset(),
    IngestionSyncState.FAILED: frozenset({
        IngestionSyncState.INDEXING,  # operator repair attempt
        IngestionSyncState.INDEXED,
        IngestionSyncState.DELETE_PENDING}),
}

#: Qdrant operation-state transitions (monotonic).  ``verified`` is
#: terminal; ``reconciliation_required`` is reachable from any
#: non-terminal state; ``failed`` may retry via ``prepared``.
_OP_TRANSITIONS: Dict[OpState, FrozenSet[OpState]] = {
    OpState.PREPARED: frozenset({
        OpState.EXECUTING, OpState.FAILED,
        OpState.RECONCILIATION_REQUIRED,
        OpState.APPLIED_UNVERIFIED}),
    OpState.EXECUTING: frozenset({
        OpState.APPLIED_UNVERIFIED, OpState.VERIFIED, OpState.FAILED,
        OpState.RECONCILIATION_REQUIRED}),
    OpState.APPLIED_UNVERIFIED: frozenset({
        OpState.VERIFIED, OpState.FAILED,
        OpState.RECONCILIATION_REQUIRED}),
    OpState.FAILED: frozenset({
        OpState.PREPARED, OpState.EXECUTING,
        OpState.RECONCILIATION_REQUIRED}),
    OpState.RECONCILIATION_REQUIRED: frozenset({
        OpState.VERIFIED, OpState.FAILED, OpState.EXECUTING}),
    OpState.VERIFIED: frozenset(),
}


def _coerce(enum_cls, value):
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise TransitionError(
            f"unknown {enum_cls.__name__} state: {value!r}") from exc


def can_transition_version(current, target) -> bool:
    cur = _coerce(VersionStatus, current)
    tgt = _coerce(VersionStatus, target)
    if cur == tgt:
        return True  # idempotent re-application
    return tgt in _VERSION_TRANSITIONS[cur]


def can_transition_ingestion(current, target) -> bool:
    cur = _coerce(IngestionSyncState, current)
    tgt = _coerce(IngestionSyncState, target)
    if cur == tgt:
        return True
    return tgt in _INGESTION_TRANSITIONS[cur]


def can_transition_operation(current, target) -> bool:
    cur = _coerce(OpState, current)
    tgt = _coerce(OpState, target)
    if cur == tgt:
        return True
    return tgt in _OP_TRANSITIONS[cur]


def assert_version_transition(current, target) -> None:
    if not can_transition_version(current, target):
        raise TransitionError(
            f"illegal version transition: {current} -> {target}")


def assert_ingestion_transition(current, target) -> None:
    if not can_transition_ingestion(current, target):
        raise TransitionError(
            f"illegal ingestion transition: {current} -> {target}")


def assert_operation_transition(current, target) -> None:
    if not can_transition_operation(current, target):
        raise TransitionError(
            f"illegal operation transition: {current} -> {target}")


def is_terminal_ingestion(state) -> bool:
    return _coerce(IngestionSyncState, state) in (
        IngestionSyncState.DELETED,)


def version_status_for_ingestion(state) -> Optional[VersionStatus]:
    """Map an ingestion sync state to the corresponding version
    status at defined evidence points only (never a blind copy)."""
    st = _coerce(IngestionSyncState, state)
    return {
        IngestionSyncState.REGISTERED: VersionStatus.STAGING,
        IngestionSyncState.PARSING: VersionStatus.PARSING,
        IngestionSyncState.EMBEDDING: VersionStatus.EMBEDDING,
        IngestionSyncState.INDEXING: VersionStatus.INDEXING,
        IngestionSyncState.INDEXED: VersionStatus.INDEXED,
        IngestionSyncState.INDEX_PARTIAL: VersionStatus.INDEX_PARTIAL,
        IngestionSyncState.FAILED: VersionStatus.FAILED,
    }.get(st)
