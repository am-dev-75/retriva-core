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

"""Automated Qdrant visible-point verification scan (Spec 028 §26).

Scans the ACTUAL target Qdrant collection and verifies every
retrieval-visible point carries the accepted authoritative payload
contract.  This is the real, computed gate authority cutover uses; it
is NOT an operator-supplied boolean.

"Retrieval-visible" is defined by the accepted serving semantics:
- pre-authoritative compatibility mode: ``serving == true`` OR
  ``serving`` absent (legacy/adopted points);
- authoritative mode: ``serving == true`` only (missing-``serving``
  compatibility disabled);
- staging replacements (``serving == false``) are NOT retrieval-visible
  and are exempt from the contract;
- superseded/deactivated (``serving == false``) are NOT visible;
- orphan points with no ``serving`` are visible in compatibility mode
  and therefore must be attributed or they block cutover.

The scan NEVER mutates Qdrant (no set/delete/upsert), is bounded and
paginated, resumable via offset, and produces bounded evidence with no
document text or full payloads.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from retriva.knowledge.visibility import (
    FIELD_DOCUMENT,
    FIELD_KB_IDS,
    FIELD_PROVENANCE,
    FIELD_SERVING,
    FIELD_TENANT,
    FIELD_VERSION,
)
from retriva.logger import get_logger

_log = get_logger(__name__)

REQUIRED_FIELDS = (
    FIELD_TENANT, FIELD_DOCUMENT, FIELD_VERSION, FIELD_SERVING,
    FIELD_KB_IDS, FIELD_PROVENANCE,
)

#: Required-field contract version (bump if the contract changes).
CONTRACT_VERSION = "visible-contract-v1"

_STRING_FIELDS = (FIELD_TENANT, FIELD_DOCUMENT, FIELD_VERSION,
                  FIELD_PROVENANCE)


def is_retrieval_visible(payload: Dict[str, Any], *,
                         authoritative: bool) -> bool:
    serving = payload.get(FIELD_SERVING)
    if authoritative:
        # Authoritative mode disables missing-serving compatibility AND
        # requires the tenant contract field; points without it are not
        # retrieval-visible at all.
        return serving is True and bool(payload.get(FIELD_TENANT))
    return serving is not False


def _missing_or_malformed(payload: Dict[str, Any]) -> List[str]:
    problems: List[str] = []
    for f in _STRING_FIELDS:
        v = payload.get(f)
        if v is None:
            problems.append(f"missing:{f}")
        elif not isinstance(v, str) or not v:
            problems.append(f"malformed:{f}")
    if FIELD_SERVING not in payload:
        problems.append(f"missing:{FIELD_SERVING}")
    elif not isinstance(payload.get(FIELD_SERVING), bool):
        problems.append(f"malformed:{FIELD_SERVING}")
    kb = payload.get(FIELD_KB_IDS)
    if kb is None:
        problems.append(f"missing:{FIELD_KB_IDS}")
    elif not isinstance(kb, list) or not all(isinstance(x, str) for x in kb):
        problems.append(f"malformed:{FIELD_KB_IDS}")
    return problems


@dataclass
class VisiblePointScan:
    collection_name: str
    tenant_id: str
    authoritative: bool
    inspected: int = 0
    visible: int = 0
    incomplete: int = 0
    conflicts: int = 0
    field_problems: Dict[str, int] = field(default_factory=dict)
    incomplete_ids: List[str] = field(default_factory=list)
    conflict_ids: List[str] = field(default_factory=list)
    next_offset: Optional[Any] = None
    complete: bool = False
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    contract_version: str = CONTRACT_VERSION

    def to_summary(self) -> Dict[str, Any]:
        completed = self.completed_at or time.time()
        return {
            "kind": "visible_point_scan",
            "collection": self.collection_name,
            "tenant_id": self.tenant_id,
            "authoritative_mode": self.authoritative,
            "inspected": self.inspected,
            "visible": self.visible,
            "incomplete": self.incomplete,
            "conflicts": self.conflicts,
            "field_problems": dict(sorted(self.field_problems.items())),
            "incomplete_ids": self.incomplete_ids[:20],
            "conflict_ids": self.conflict_ids[:20],
            "complete": self.complete,
            "contract_version": self.contract_version,
            "started_at": round(self.started_at, 3),
            "completed_at": round(completed, 3),
            "fingerprint": self.fingerprint(),
        }

    def fingerprint(self) -> str:
        basis = json.dumps({
            "c": self.collection_name, "i": self.inspected,
            "v": self.visible, "x": self.incomplete, "k": self.conflicts,
            "f": sorted(self.field_problems.items()),
            "ids": sorted(self.incomplete_ids)[:50],
            "cv": self.contract_version,
        }, sort_keys=True)
        return hashlib.sha256(basis.encode()).hexdigest()[:32]

    @property
    def ok(self) -> bool:
        return self.complete and self.incomplete == 0 and self.conflicts == 0


def scan_visible_points(
        client, collection_name: str, *, tenant_id: str,
        known_kbs: Optional[Sequence[str]] = None,
        batch: int = 256, start_offset: Any = None,
        max_batches: Optional[int] = None,
        authoritative: bool = False,
) -> VisiblePointScan:
    """Bounded, resumable, read-only scan of a Qdrant collection.

    ``known_kbs`` (from the adopted ``knowledge_bases`` mapping) lets
    the scan classify a retrieval-visible point whose ``kb_ids`` are
    unknown as an unresolved conflict.  Never mutates Qdrant.
    """
    known = set(known_kbs) if known_kbs else None
    scan = VisiblePointScan(
        collection_name=collection_name, tenant_id=tenant_id,
        authoritative=authoritative)
    offset = start_offset
    batches = 0
    while True:
        records, offset = client.scroll(
            collection_name=collection_name, limit=max(1, int(batch)),
            offset=offset, with_payload=True, with_vectors=False)
        batches += 1
        for rec in records:
            payload = getattr(rec, "payload", None) or {}
            scan.inspected += 1
            if not is_retrieval_visible(payload,
                                        authoritative=authoritative):
                continue
            scan.visible += 1
            problems = _missing_or_malformed(payload)
            if problems:
                scan.incomplete += 1
                for p in problems:
                    scan.field_problems[p] = scan.field_problems.get(p, 0) + 1
                if len(scan.incomplete_ids) < 100:
                    scan.incomplete_ids.append(str(rec.id))
                continue
            if known is not None:
                kbs = payload.get(FIELD_KB_IDS) or []
                if kbs and not any(k in known for k in kbs):
                    scan.conflicts += 1
                    if len(scan.conflict_ids) < 100:
                        scan.conflict_ids.append(str(rec.id))
        if offset is None:
            scan.complete = True
            break
        if max_batches is not None and batches >= max_batches:
            scan.next_offset = offset
            break
    scan.completed_at = time.time()
    return scan
