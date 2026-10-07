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

"""Superseded-version Qdrant point lifecycle (Spec 031 / ADR-036).

Retain-then-delete cleanup of Qdrant points belonging to superseded
versions of active documents.  Fail-closed eligibility; non-mutating
dry-run; privileged operator-authorized apply; durable intent/evidence in
``knowledge.qdrant_operations``; explicit bounded point-ID deletion with a
mandatory exact zero-point postcondition; ambiguous outcomes are never
blindly replayed.  Operational bounds B1-B7 (owner-approved) are hard,
non-bypassable controls.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from retriva.knowledge.repository import KnowledgeRepository
from retriva.logger import get_logger

_log = get_logger(__name__)

POLICY_VERSION = "superseded-cleanup/1"
RETENTION_DAYS = 30

# Repository-accepted qdrant_operations states (no migration).
ST_PREPARED = "prepared"
ST_EXECUTING = "executing"
ST_APPLIED_UNVERIFIED = "applied_unverified"
ST_VERIFIED = "verified"
ST_FAILED = "failed"
ST_RECONCILIATION_REQUIRED = "reconciliation_required"
_OPEN_STATES = (ST_PREPARED, ST_EXECUTING, ST_APPLIED_UNVERIFIED)
OP_TYPE = "delete_points"

#: Provenance classes that are certain enough for automated deletion.
_CERTAIN_PROVENANCE = ("native",)


@dataclass(frozen=True)
class CleanupBounds:
    """Owner-approved operational bounds B1-B7 (Spec 031)."""

    max_versions_per_op: int = 100
    max_points_per_op: int = 2000
    wait_seconds: float = 30.0
    rate_limit_seconds: float = 30.0
    concurrency: int = 1
    window_start: Optional[str] = None          # "HH:MM" (local to window_tz)
    window_end: Optional[str] = None
    window_tz: Optional[str] = None             # e.g. "UTC"
    stop_backlog_points: int = 5000
    stop_p95_factor: float = 2.0
    baseline_p95_ms: Optional[float] = None
    min_latency_samples: int = 20

    def fingerprint(self) -> str:
        payload = {
            "B1": self.max_versions_per_op, "B2": self.max_points_per_op,
            "B3": self.wait_seconds, "B4": self.rate_limit_seconds,
            "B5": self.concurrency, "B6": [self.window_start,
                                           self.window_end, self.window_tz],
            "B7": [self.stop_backlog_points, self.stop_p95_factor,
                   self.baseline_p95_ms, self.min_latency_samples],
        }
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True).encode("utf-8")).hexdigest()

    def within_window(self, now: datetime) -> Optional[bool]:
        """True inside the window, False outside, None if unconfigured."""
        if not (self.window_start and self.window_end and self.window_tz):
            return None
        try:
            from zoneinfo import ZoneInfo
            start = time.fromisoformat(self.window_start)
            end = time.fromisoformat(self.window_end)
            local = now.astimezone(ZoneInfo(self.window_tz)).time()
        except Exception:
            return None
        if start <= end:
            return start <= local < end
        return local >= start or local < end  # wraps midnight


@dataclass
class Candidate:
    tenant_id: str
    document_id: str
    version_id: str
    superseded_at: Any
    provenance: str
    point_ids: List[str] = field(default_factory=list)
    blocked_reason: Optional[str] = None
    serving_incident: bool = False

    @property
    def eligible(self) -> bool:
        return self.blocked_reason is None and not self.serving_incident


class CleanupRefused(RuntimeError):
    """Bounded, operator-facing refusal (fail-closed)."""

    def __init__(self, code: str, summary: str):
        super().__init__(summary)
        self.code = code
        self.summary = summary


class SupersededCleanup:
    """Fail-closed superseded-point cleanup service."""

    def __init__(self, repository: Optional[KnowledgeRepository] = None,
                 qdrant_client=None, bounds: Optional[CleanupBounds] = None,
                 *, clock: Optional[Callable[[], datetime]] = None,
                 latency_sampler: Optional[Callable[[], List[float]]] = None,
                 collection_name_resolver: Optional[Callable[[], str]] = None,
                 page_limit: int = 10000):
        self._repo = repository or KnowledgeRepository()
        self._client = qdrant_client
        self._bounds = bounds or CleanupBounds()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._latency = latency_sampler or (lambda: [])
        self._collection = collection_name_resolver or _default_collection
        self._page_limit = page_limit

    # -- read-only discovery ---------------------------------------------

    def discover(self, tenant_id: Optional[str]) -> List[Candidate]:
        with self._repo.transaction(tenant_id, privileged=True) as cur:
            cur.execute(
                "SELECT v.version_id, v.document_id, v.tenant_id, "
                "v.superseded_at, v.provenance FROM "
                "knowledge.document_versions v JOIN knowledge.documents d "
                "ON d.document_id = v.document_id WHERE v.status='superseded' "
                "AND d.lifecycle_state='active' AND "
                "d.current_version_id IS DISTINCT FROM v.version_id AND "
                "v.superseded_at IS NOT NULL AND v.superseded_at <= "
                "now() - make_interval(days => %s) AND "
                "(%s IS NULL OR v.tenant_id = %s) "
                "ORDER BY v.superseded_at, v.version_id LIMIT %s",
                (RETENTION_DAYS, tenant_id, tenant_id,
                 max(1, self._bounds.max_versions_per_op * 10)))
            rows = [dict(r) for r in cur.fetchall()]
        out: List[Candidate] = []
        for row in rows:
            out.append(self._classify(row))
        return out

    def _classify(self, row: Dict[str, Any]) -> Candidate:
        cand = Candidate(tenant_id=row["tenant_id"],
                         document_id=row["document_id"],
                         version_id=row["version_id"],
                         superseded_at=row["superseded_at"],
                         provenance=row["provenance"])
        if row["provenance"] not in _CERTAIN_PROVENANCE:
            cand.blocked_reason = "uncertain_provenance"
            return cand
        # Open / ambiguous operation for this version blocks cleanup.
        with self._repo.transaction(row["tenant_id"], privileged=True) as cur:
            open_ops = self._repo.list_operations(
                cur, tenant_id=row["tenant_id"], states=list(_OPEN_STATES))
        if any(op.get("version_id") == row["version_id"] for op in open_ops):
            cand.blocked_reason = "open_operation"
            return cand
        # Manifest (authoritative) must be complete and non-empty.
        with self._repo.transaction(row["tenant_id"]) as cur:
            manifest = self._repo.list_chunk_states(
                cur, tenant_id=row["tenant_id"], version_id=row["version_id"])
        live = [m for m in manifest if m.get("sync_state") != "removed"]
        if not live:
            cand.blocked_reason = "missing_manifest"
            return cand
        if any(m.get("sync_state") not in ("verified", "removed") for m in manifest):
            cand.blocked_reason = "incomplete_manifest"
            return cand
        # Resolve explicit point ids + serving state from Qdrant.
        if self._client is None:
            cand.blocked_reason = "no_qdrant_client"
            return cand
        from retriva.knowledge.visibility import FIELD_SERVING
        ids: List[str] = []
        serving_true = False
        try:
            from retriva.knowledge.visibility import version_filter
            offset = None
            while True:
                recs, offset = self._client.scroll(
                    collection_name=self._collection(),
                    scroll_filter=version_filter(row["version_id"]),
                    limit=min(1000, self._page_limit), offset=offset,
                    with_payload=True, with_vectors=False)
                for r in recs:
                    ids.append(str(r.id))
                    if (r.payload or {}).get(FIELD_SERVING) is True:
                        serving_true = True
                if offset is None or len(ids) >= self._page_limit:
                    break
        except Exception:
            cand.blocked_reason = "qdrant_read_failed"
            return cand
        cand.point_ids = ids
        if serving_true:
            cand.serving_incident = True
            cand.blocked_reason = "superseded_but_serving"
            return cand
        if not ids:
            cand.blocked_reason = "no_points"
            return cand
        if len(ids) > self._bounds.max_points_per_op:
            # B2: a single oversized version fails closed (no partition
            # contract is accepted for per-version splitting).
            cand.blocked_reason = "version_exceeds_max_points"
            return cand
        return cand

    # -- dry-run ---------------------------------------------------------

    def dry_run(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        cands = self.discover(tenant_id)
        eligible = [c for c in cands if c.eligible]
        blocked: Dict[str, int] = {}
        for c in cands:
            if c.blocked_reason:
                blocked[c.blocked_reason] = blocked.get(
                    c.blocked_reason, 0) + 1
            elif c.serving_incident:
                blocked["superseded_but_serving"] = blocked.get(
                    "superseded_but_serving", 0) + 1
        eligible_points = sum(len(c.point_ids) for c in eligible)
        return {
            "policy_version": POLICY_VERSION,
            "bounds_fingerprint": self._bounds.fingerprint(),
            "candidates": len(cands),
            "eligible_versions": len(eligible),
            "eligible_points": eligible_points,
            "retained_due_to_age": _count_blocked(blocked, "young"),
            "blocked_by_reason": blocked,
            "estimated_reclaimed_points": eligible_points,
            "proposed_batches": _batches(eligible, self._bounds),
            "candidate_fingerprint": candidate_fingerprint(
                eligible, self._bounds),
            "within_window": self._bounds.within_window(self._clock()),
            "latency_evidence": len(self._latency()),
        }

    # -- apply -----------------------------------------------------------

    def apply(self, tenant_id: Optional[str], *, operator: str,
              candidate_fingerprint_expected: str,
              authorization: Optional[str] = None) -> Dict[str, Any]:
        """Privileged, operator-authorized apply.  Fail-closed."""
        if not operator or not authorization:
            raise CleanupRefused("operator_required",
                                 "explicit operator authorization required")
        now = self._clock()
        window = self._bounds.within_window(now)
        if window is None:
            raise CleanupRefused("window_unconfigured",
                                 "maintenance window not configured")
        if window is False:  # B6
            raise CleanupRefused("outside_maintenance_window",
                                 "apply refused outside off-peak window")

        # B5: one active cleanup operation globally.
        with self._repo.transaction(tenant_id, privileged=True) as cur:
            active = self._repo.list_operations(
                cur, tenant_id=None, states=[ST_EXECUTING, ST_PREPARED])
        if len(active) >= self._bounds.concurrency:
            raise CleanupRefused("concurrency_limit",
                                 "an apply operation is already active")

        # B4: durable rate limit on delete issuance.
        with self._repo.transaction(tenant_id, privileged=True) as cur:
            recent = self._repo.list_operations(
                cur, tenant_id=None, states=[ST_EXECUTING,
                                             ST_APPLIED_UNVERIFIED,
                                             ST_VERIFIED, ST_FAILED,
                                             ST_RECONCILIATION_REQUIRED],
                limit=10)
        if self._rate_limited(recent, now):
            raise CleanupRefused("rate_limited",
                                 "rate limit: wait before next delete")

        cands = self.discover(tenant_id)
        eligible = [c for c in cands if c.eligible]
        # B7: stop conditions (OR semantics), fail closed on missing evidence.
        self._evaluate_stop_conditions(eligible)

        # Revalidate fingerprint (dry-run -> apply stability).
        fp = candidate_fingerprint(eligible, self._bounds)
        if fp != candidate_fingerprint_expected:
            raise CleanupRefused("candidate_changed",
                                 "candidate set changed since dry-run")

        # B1: cap versions per operation.
        batch = eligible[:self._bounds.max_versions_per_op]
        results = []
        for cand in batch:
            results.append(self._apply_one(cand, operator=operator))
        return {
            "policy_version": POLICY_VERSION,
            "bounds_fingerprint": self._bounds.fingerprint(),
            "candidate_fingerprint": fp,
            "applied": results,
        }

    def _rate_limited(self, recent_ops, now) -> bool:
        stamps = [op.get("executed_at") for op in recent_ops
                  if op.get("executed_at")]
        if not stamps:
            return False
        latest = max(stamps)
        if isinstance(latest, datetime):
            lnow = now
            if latest.tzinfo is None:
                lnow = now.replace(tzinfo=None)
            return (lnow - latest).total_seconds() \
                < self._bounds.rate_limit_seconds
        return False

    def _evaluate_stop_conditions(self, eligible: List[Candidate]) -> None:
        backlog = sum(len(c.point_ids) for c in eligible)
        if backlog > self._bounds.stop_backlog_points:  # B7 (a)
            raise CleanupRefused("stop_backlog_exceeded",
                                 "eligible backlog exceeds stop threshold")
        samples = self._latency()
        if samples:
            observed = _p95(samples)
            baseline = self._bounds.baseline_p95_ms
            if baseline is None:
                raise CleanupRefused("latency_baseline_missing",
                                     "latency baseline unavailable")
            if len(samples) >= self._bounds.min_latency_samples and \
                    observed > baseline * self._bounds.stop_p95_factor:
                raise CleanupRefused("stop_p95_exceeded",
                                     "observed p95 exceeds stop threshold")
        elif self._bounds.baseline_p95_ms is not None:
            raise CleanupRefused("latency_evidence_missing",
                                 "latency evidence unavailable")

    def _apply_one(self, cand: Candidate, *, operator: str) -> Dict[str, Any]:
        collection = self._collection()
        with self._repo.transaction(cand.tenant_id) as cur:
            op_id = self._repo.record_operation(
                cur, tenant_id=cand.tenant_id, op_type=OP_TYPE,
                collection_name=collection, version_id=cand.version_id,
                document_id=cand.document_id,
                expected_count=len(cand.point_ids), op_state=ST_PREPARED)
        self._set_state(cand.tenant_id, op_id, ST_EXECUTING)
        try:
            self._client.delete(
                collection_name=collection,
                points_selector=cand.point_ids, wait=True)
            self._set_state(cand.tenant_id, op_id, ST_APPLIED_UNVERIFIED)
        except Exception as exc:  # bounded classification
            self._set_state(cand.tenant_id, op_id,
                            ST_RECONCILIATION_REQUIRED,
                            error_code="qdrant_delete_error",
                            error_summary=exc.__class__.__name__)
            return {"version_id": cand.version_id, "state":
                    ST_RECONCILIATION_REQUIRED}
        return self._verify(cand, op_id)

    def _verify(self, cand: Candidate, op_id: str) -> Dict[str, Any]:
        # B3: bounded wait/verification; a timeout NEVER triggers blind replay.
        from retriva.knowledge.visibility import count_version_points
        try:
            remaining = count_version_points(
                self._client, self._collection(), cand.version_id)
        except Exception:
            self._set_state(cand.tenant_id, op_id,
                            ST_RECONCILIATION_REQUIRED,
                            error_code="verify_unavailable",
                            error_summary="qdrant unavailable")
            return {"version_id": cand.version_id,
                    "state": ST_RECONCILIATION_REQUIRED}
        if remaining == 0:  # exact zero-point postcondition
            self._set_state(cand.tenant_id, op_id, ST_VERIFIED)
            return {"version_id": cand.version_id, "state": ST_VERIFIED}
        self._set_state(cand.tenant_id, op_id, ST_RECONCILIATION_REQUIRED,
                        error_code="points_remain",
                        error_summary="zero-point verification failed")
        return {"version_id": cand.version_id,
                "state": ST_RECONCILIATION_REQUIRED}

    def _set_state(self, tenant_id, op_id, state, *, error_code=None,
                   error_summary=None):
        with self._repo.transaction(tenant_id) as cur:
            self._repo.update_operation_state(
                cur, tenant_id=tenant_id, op_id=op_id, op_state=state,
                error_code=error_code, error_summary=error_summary)


# -- helpers --------------------------------------------------------------

def _default_collection() -> str:
    from retriva.indexing.qdrant_store import get_collection_name
    return get_collection_name()


def _new_op_id() -> str:
    import uuid
    return uuid.uuid4().hex


def _count_blocked(blocked: Dict[str, int], key: str) -> int:
    return sum(v for k, v in blocked.items() if k.startswith(key))


def _p95(samples: List[float]) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    idx = max(0, int(round(0.95 * (len(ordered) - 1))))
    return float(ordered[idx])


def _batches(eligible: List[Candidate], bounds: CleanupBounds
             ) -> List[int]:
    out = []
    remaining = list(eligible)
    while remaining:
        self_batch = remaining[:bounds.max_versions_per_op]
        out.append(len(self_batch))
        remaining = remaining[bounds.max_versions_per_op:]
    return out


def candidate_fingerprint(eligible: List[Candidate],
                          bounds: CleanupBounds) -> str:
    payload = {
        "policy": POLICY_VERSION,
        "bounds": bounds.fingerprint(),
        "items": sorted(
            f"{c.tenant_id}:{c.document_id}:{c.version_id}:"
            f"{sorted(c.point_ids)}" for c in eligible),
    }
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True).encode("utf-8")).hexdigest()