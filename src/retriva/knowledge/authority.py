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

"""Knowledge authority/readiness state (Spec 028 §11/§19).

Explicit readiness states stored in PostgreSQL, checked at startup,
fail-closed.  There is NO boolean configuration switch restoring
legacy (JSON/SQLite) authority: after ``authoritative`` the runtime
REFUSES legacy authority outright.  ``suspended`` stops new
metadata-dependent ingestion while preserving retrieval and operator
access (the rollback posture after cutover).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, Optional, Sequence

from retriva.knowledge.repository import (
    KnowledgeRepository,
    KnowledgeRepositoryError,
)
from retriva.logger import get_logger

_log = get_logger(__name__)


class AuthorityError(RuntimeError):
    """Authority/readiness violation (sanitized, fail-closed)."""


class AuthorityState(str, Enum):
    SCHEMA_READY = "schema_ready"
    ADOPTION_PENDING = "adoption_pending"
    ADOPTION_VERIFIED = "adoption_verified"
    AUTHORITATIVE = "authoritative"
    SUSPENDED = "suspended"
    RECONCILIATION_REQUIRED = "reconciliation_required"


#: Legal state transitions (operator-driven only).
_AUTHORITY_TRANSITIONS: Dict[
        AuthorityState, FrozenSet[AuthorityState]] = {
    AuthorityState.SCHEMA_READY: frozenset({
        AuthorityState.ADOPTION_PENDING, AuthorityState.SUSPENDED}),
    AuthorityState.ADOPTION_PENDING: frozenset({
        AuthorityState.ADOPTION_VERIFIED, AuthorityState.SUSPENDED}),
    AuthorityState.ADOPTION_VERIFIED: frozenset({
        AuthorityState.AUTHORITATIVE, AuthorityState.ADOPTION_PENDING,
        AuthorityState.SUSPENDED}),
    AuthorityState.AUTHORITATIVE: frozenset({
        AuthorityState.SUSPENDED,
        AuthorityState.RECONCILIATION_REQUIRED}),
    AuthorityState.SUSPENDED: frozenset({
        AuthorityState.AUTHORITATIVE, AuthorityState.ADOPTION_PENDING,
        AuthorityState.RECONCILIATION_REQUIRED}),
    AuthorityState.RECONCILIATION_REQUIRED: frozenset({
        AuthorityState.SUSPENDED, AuthorityState.AUTHORITATIVE,
        AuthorityState.ADOPTION_VERIFIED}),
}

#: States in which native (PostgreSQL-authoritative) ingestion is
#: permitted.
_NATIVE_INGESTION_STATES = frozenset({AuthorityState.AUTHORITATIVE})


@dataclass(frozen=True)
class KnowledgeReadiness:
    """The additive public readiness object ``knowledge_metadata``."""

    state: AuthorityState
    authoritative: bool
    native_ingestion_available: bool

    def to_public_dict(self) -> Dict[str, object]:
        return {
            "state": self.state.value,
            "authoritative": self.authoritative,
            "native_ingestion_available": self.native_ingestion_available,
        }

    @classmethod
    def from_state(cls, state: AuthorityState) -> "KnowledgeReadiness":
        authoritative = state is AuthorityState.AUTHORITATIVE
        native = state in _NATIVE_INGESTION_STATES
        if authoritative != (state is AuthorityState.AUTHORITATIVE):
            raise AuthorityError("invalid authoritative combination")
        if native and not authoritative:
            raise AuthorityError(
                "native_ingestion_available requires authoritative state")
        return cls(state=state, authoritative=authoritative,
                   native_ingestion_available=native)


class KnowledgeAuthority:
    """Reads and transitions the durable authority row."""

    def __init__(self, repository: Optional[KnowledgeRepository] = None):
        self._repo = repository or KnowledgeRepository()

    # -- read ------------------------------------------------------------

    def read_state(self) -> AuthorityState:
        """Read the durable authority state.  Missing/unknown state is
        fail-closed: treated as ``schema_ready`` (no native ingestion)."""
        try:
            with self._repo.transaction() as cur:
                row = self._repo.get_authority_row(cur)
        except KnowledgeRepositoryError as exc:
            _log.warning(
                "knowledge authority unreadable; fail-closed: %s",
                exc.__class__.__name__)
            return AuthorityState.SCHEMA_READY
        if not row:
            return AuthorityState.SCHEMA_READY
        try:
            return AuthorityState(row["state"])
        except ValueError:
            _log.warning("unknown knowledge authority state; fail-closed")
            return AuthorityState.SCHEMA_READY

    def readiness(self) -> KnowledgeReadiness:
        return KnowledgeReadiness.from_state(self.read_state())

    def public_readiness(self) -> Dict[str, object]:
        return self.readiness().to_public_dict()

    def is_authoritative(self) -> bool:
        return self.read_state() is AuthorityState.AUTHORITATIVE

    # -- gating ----------------------------------------------------------

    def require_native_ingestion(self) -> None:
        """Raise when native metadata-dependent ingestion is not
        available (the explicit pre-cutover posture and suspended
        posture reject new ingestion; retrieval is unaffected)."""
        state = self.read_state()
        if state is AuthorityState.AUTHORITATIVE:
            return
        raise AuthorityError(
            "native knowledge ingestion is unavailable while knowledge "
            f"authority state is '{state.value}'; this is the explicit "
            "pre-cutover/suspended posture (no silent legacy fallback)")

    # -- transition ------------------------------------------------------

    def transition(self, target: AuthorityState, *,
                   operator: str, note: Optional[str] = None,
                   adoption_run_ref: Optional[str] = None,
                   catalog_frozen: bool = False,
                   sqlite_frozen: bool = False) -> Dict[str, object]:
        """Privileged operator transition with durable evidence.

        Refuses illegal transitions and invalid state combinations.
        Never silently falls back: the transition runs on a migrator
        (privileged) connection only.
        """
        target = AuthorityState(target)
        with self._repo.transaction(privileged=True) as cur:
            row = self._repo.get_authority_row(cur)
            if row is None:
                raise AuthorityError(
                    "knowledge authority row is missing; the migration "
                    "has not been applied")
            current = AuthorityState(row["state"])
            if target != current and target not in _AUTHORITY_TRANSITIONS[
                    current]:
                raise AuthorityError(
                    f"illegal knowledge authority transition: "
                    f"{current.value} -> {target.value}")
            readiness = KnowledgeReadiness.from_state(target)
            cur.execute(
                "UPDATE knowledge.authority SET state=%s, "
                "authoritative=%s, native_ingestion_available=%s, "
                "adoption_run_ref=COALESCE(%s, adoption_run_ref), "
                "operator_note=COALESCE(%s, operator_note), "
                "catalog_frozen_at=CASE WHEN %s THEN "
                "COALESCE(catalog_frozen_at, now()) ELSE catalog_frozen_at "
                "END, "
                "sqlite_frozen_at=CASE WHEN %s THEN "
                "COALESCE(sqlite_frozen_at, now()) ELSE sqlite_frozen_at "
                "END, "
                "updated_at=now(), updated_by=%s "
                "WHERE singleton=TRUE",
                (target.value, readiness.authoritative,
                 readiness.native_ingestion_available, adoption_run_ref,
                 note, catalog_frozen, sqlite_frozen, operator))
            updated = self._repo.get_authority_row(cur)
        _log.info(
            "knowledge authority transition: %s -> %s by operator",
            current.value, target.value)
        return updated or {}

    # -- cutover gates ---------------------------------------------------

    def cutover_gates(self, evidence: Dict[str, bool]) -> Dict[str, bool]:
        """Evaluate the documented cutover gates.  Returns a mapping of
        gate name to satisfied; cutover requires every gate True."""
        required = (
            "kb_registry_adoption_verified",
            "catalog_adoption_verified",
            "qdrant_scan_completed",
            "conflicts_classified",
            "visible_points_have_payload_evidence",
            "uncertain_records_tracked",
            "retrieval_equivalence_passed",
            "operation_evidence_consistent",
            "post_adoption_reconcile_clean",
            "suspension_procedure_ready",
        )
        return {name: bool(evidence.get(name, False))
                for name in required}

    def set_authoritative(self, *, operator: str,
                          evidence: Dict[str, bool],
                          adoption_run_ref: Optional[str] = None,
                          equivalence_op_id: Optional[str] = None,
                          target_collection: Optional[str] = None,
                          tenant_id: Optional[str] = None,
                          max_scan_age_seconds: Optional[int] = 900,
                          note: Optional[str] = None) -> Dict[str, object]:
        """Explicit privileged cutover: all gates must pass.

        The retrieval-equivalence / visible-point gate must be backed by
        DURABLE evidence from the automated Qdrant visible-point scan
        (``adopt_verify`` operation whose bounded summary records
        ``complete=true``, ``incomplete=0``, ``conflicts=0``).  An
        operator-supplied boolean is never sufficient.
        """
        gates = self.cutover_gates(evidence)
        failed = sorted(name for name, ok in gates.items() if not ok)
        if failed:
            raise AuthorityError(
                "knowledge authority cutover rejected; unsatisfied "
                "gates: " + ", ".join(failed))
        self._require_durable_scan_evidence(
            equivalence_op_id, target_collection=target_collection,
            tenant_id=tenant_id, max_age_seconds=max_scan_age_seconds)
        return self.transition(
            AuthorityState.AUTHORITATIVE, operator=operator,
            note=note, adoption_run_ref=adoption_run_ref
            or equivalence_op_id)

    def compute_cutover_scan(self, *, tenant_id: str,
                             collection_name: str, client, operator: str,
                             known_kbs: Optional[Sequence[str]] = None,
                             authoritative: bool = False):
        """Run the AUTOMATED Qdrant visible-point scan and persist its
        bounded evidence as a durable ``adopt_verify`` operation.

        Returns ``(op_id, scan)``; the operation is ``verified`` only
        when the scan found zero incomplete visible points and zero
        unresolved conflicts.  Qdrant is only read."""
        import json

        from retriva.knowledge.scan import scan_visible_points

        scan = scan_visible_points(
            client, collection_name, tenant_id=tenant_id,
            known_kbs=known_kbs, authoritative=authoritative)
        summary = json.dumps(scan.to_summary())
        with self._repo.transaction(tenant_id, privileged=True) as cur:
            op_id = self._repo.record_operation(
                cur, tenant_id=tenant_id, op_type="adopt_verify",
                collection_name=collection_name,
                expected_count=scan.inspected,
                target_summary=summary, op_state="prepared")
            state = "verified" if scan.ok else "failed"
            self._repo.finalize_operation_evidence(
                cur, tenant_id=tenant_id, op_id=op_id, summary=summary,
                op_state=state, inspected=scan.inspected)
        _log.info(
            "cutover scan persisted: op=%s inspected=%d visible=%d "
            "incomplete=%d conflicts=%d ok=%s", op_id, scan.inspected,
            scan.visible, scan.incomplete, scan.conflicts, scan.ok)
        return op_id, scan

    def _require_durable_scan_evidence(
            self, op_id: Optional[str], *,
            target_collection: Optional[str],
            tenant_id: Optional[str],
            max_age_seconds: Optional[int]) -> None:
        import json
        import time

        if not op_id:
            raise AuthorityError(
                "retrieval-equivalence gate requires durable evidence "
                "(a verified automated visible-point scan operation id)")
        op = self._repo.get_operation_privileged(op_id)
        if not op or op.get("op_type") != "adopt_verify":
            raise AuthorityError(
                "cutover evidence is not a durable adopt_verify operation")
        if op.get("op_state") != "verified":
            raise AuthorityError(
                "cutover evidence operation is not verified "
                f"(state={op.get('op_state')})")
        try:
            summary = json.loads(op.get("target_summary") or "{}")
        except (TypeError, ValueError):
            raise AuthorityError("cutover evidence summary is unreadable")
        if summary.get("kind") != "visible_point_scan":
            raise AuthorityError(
                "cutover evidence is not an automated visible-point scan")
        if not summary.get("complete"):
            raise AuthorityError(
                "visible-point scan did not complete")
        if int(summary.get("incomplete", -1)) != 0:
            raise AuthorityError(
                "visible-point scan found incomplete retrieval-visible "
                f"points ({summary.get('incomplete')})")
        if int(summary.get("conflicts", -1)) != 0:
            raise AuthorityError(
                "visible-point scan found unresolved conflicts "
                f"({summary.get('conflicts')})")
        if target_collection and summary.get("collection") != \
                target_collection:
            raise AuthorityError(
                "cutover evidence collection does not match the target")
        if tenant_id and summary.get("tenant_id") != tenant_id:
            raise AuthorityError(
                "cutover evidence tenant scope does not match")
        if max_age_seconds is not None and \
                summary.get("completed_at") is not None:
            age = time.time() - float(summary["completed_at"])
            if age > max_age_seconds:
                raise AuthorityError(
                    "cutover evidence is stale "
                    f"(age {int(age)}s > {max_age_seconds}s)")
