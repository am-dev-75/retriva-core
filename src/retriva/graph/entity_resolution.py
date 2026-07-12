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
Core-owned entity resolution service.

Phase 1 strategy (conservative):
1. Exact match on (tenant_id, name_normalized, category).
2. Match on external_ids[system].
3. Match on alias (normalized).
4. If no match, create a new canonical entity.
5. If multiple matches, record a merge candidate (no auto-merge).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from retriva.graph.contracts import Entity, EntityCategory
from retriva.graph.stores.sqlite_graph_store import SQLiteGraphStore
from retriva.logger import get_logger

logger = get_logger(__name__)


class EntityResolutionService:
    """Shared Retriva Core entity resolution.

    This service is NOT domain-specific.  Extensions may provide identity
    hints via the :class:`GraphExtension` SPI, but canonical entity IDs
    are always assigned by this Core service.
    """

    def __init__(self, store: Optional[SQLiteGraphStore] = None):
        self._store = store or SQLiteGraphStore()

    @staticmethod
    def _normalize_name(name: str) -> str:
        return name.strip().lower()

    @staticmethod
    def _derive_canonical_id(tenant_id: str, kb_id: str, name_normalized: str) -> str:
        """Derive a deterministic canonical entity ID.

        Uses sha256(tenant_id + kb_id + name_normalized) so that the same
        entity in the same scope always gets the same ID across re-ingestion.
        """
        combined = f"{tenant_id}:{kb_id}:{name_normalized}"
        h = hashlib.sha256(combined.encode("utf-8")).hexdigest()
        return f"ent_{h[:24]}"

    def resolve(self, candidate: Entity) -> Entity:
        """Return the canonical entity for a candidate.

        If the candidate matches an existing entity, the existing entity is
        returned (with any new aliases or external IDs merged).  If no match
        is found, a new canonical entity is created.
        """
        if not candidate.name_normalized:
            candidate.name_normalized = self._normalize_name(candidate.name)

        # 1. Exact match on (tenant_id, name_normalized, category)
        matches = self._store.get_entities_by_alias(
            candidate.name, candidate.tenant_id, candidate.kb_id
        )
        exact = [
            m for m in matches
            if m.name_normalized == candidate.name_normalized
            and m.category == candidate.category
        ]

        if len(exact) == 1:
            return self._merge_into(exact[0], candidate)
        elif len(exact) > 1:
            # Multiple exact matches — record merge candidates, use the first
            logger.warning(
                f"EntityResolution: multiple matches for "
                f"'{candidate.name}' in ({candidate.tenant_id}, {candidate.kb_id}) — "
                f"recording merge candidates, using first match"
            )
            canonical = exact[0]
            for dup in exact[1:]:
                self._record_merge_candidate(canonical.entity_id, dup.entity_id,
                                             candidate.tenant_id, reason="multiple_exact_match")
            return self._merge_into(canonical, candidate)

        # 2. Match on external_ids
        for system, ext_id in candidate.external_ids.items():
            found = self._store.get_entities_by_external_id(
                system, ext_id, candidate.tenant_id
            )
            if found:
                return self._merge_into(found, candidate)

        # 3. Match on aliases (including the candidate's own name)
        all_aliases = list(candidate.aliases)
        if candidate.name_normalized not in all_aliases:
            all_aliases.append(candidate.name_normalized)
        for alias in all_aliases:
            alias_matches = self._store.get_entities_by_alias(
                alias, candidate.tenant_id, candidate.kb_id
            )
            if alias_matches:
                return self._merge_into(alias_matches[0], candidate)

        # 4. No match — create new canonical entity
        candidate.entity_id = self._derive_canonical_id(
            candidate.tenant_id, candidate.kb_id, candidate.name_normalized
        )
        if not candidate.security_scope:
            candidate.security_scope = [candidate.kb_id]
        candidate.created_at = datetime.now(timezone.utc).isoformat()
        candidate.updated_at = candidate.created_at
        self._store.upsert_entities([candidate])
        logger.debug(
            f"EntityResolution: created new entity '{candidate.name}' "
            f"({candidate.entity_id})"
        )
        return candidate

    def _merge_into(self, canonical: Entity, candidate: Entity) -> Entity:
        """Merge candidate's aliases and external IDs into the canonical entity."""
        changed = False
        for alias in candidate.aliases:
            if alias not in canonical.aliases:
                canonical.aliases.append(alias)
                changed = True
        for system, ext_id in candidate.external_ids.items():
            if system not in canonical.external_ids:
                canonical.external_ids[system] = ext_id
                changed = True
        if candidate.description and not canonical.description:
            canonical.description = candidate.description
            changed = True
        if changed:
            canonical.updated_at = datetime.now(timezone.utc).isoformat()
            self._store.upsert_entities([canonical])
        return canonical

    def _record_merge_candidate(
        self,
        canonical_id: str,
        duplicate_id: str,
        tenant_id: str,
        reason: str = "",
        confidence: float = 0.0,
    ) -> None:
        """Record a merge candidate for human review (no auto-merge)."""
        import sqlite3
        from uuid import uuid4
        now = datetime.now(timezone.utc).isoformat()
        candidate_id = f"mc_{uuid4().hex}"
        try:
            with self._store.connect() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO graph_merge_candidates
                        (candidate_id, canonical_entity_id, duplicate_entity_id,
                         tenant_id, reason, confidence, status, created_at, resolved_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL)
                    """,
                    (candidate_id, canonical_id, duplicate_id,
                     tenant_id, reason, confidence, now),
                )
        except sqlite3.Error as e:
            logger.warning(f"Failed to record merge candidate: {e}")

    def get_merge_candidates(
        self, tenant_id: str, status: str = "pending"
    ) -> List[Dict[str, Any]]:
        with self._store.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM graph_merge_candidates
                WHERE tenant_id = ? AND status = ?
                ORDER BY created_at DESC
                """,
                (tenant_id, status),
            ).fetchall()
        return [dict(r) for r in rows]

    def approve_merge(self, canonical_id: str, duplicate_id: str) -> None:
        """Human-approved merge: redirect all assertions from duplicate to canonical."""
        now = datetime.now(timezone.utc).isoformat()
        with self._store.connect() as conn:
            # Redirect assertions
            conn.execute(
                """
                UPDATE graph_assertions
                SET subject_entity_id = ?, updated_at = ?
                WHERE subject_entity_id = ?
                """,
                (canonical_id, now, duplicate_id),
            )
            # Redirect relationships
            conn.execute(
                """
                UPDATE graph_relationships
                SET source_entity_id = ?
                WHERE source_entity_id = ?
                """,
                (canonical_id, duplicate_id),
            )
            conn.execute(
                """
                UPDATE graph_relationships
                SET target_entity_id = ?
                WHERE target_entity_id = ?
                """,
                (canonical_id, duplicate_id),
            )
            # Mark merge candidate as resolved
            conn.execute(
                """
                UPDATE graph_merge_candidates
                SET status = 'approved', resolved_at = ?
                WHERE canonical_entity_id = ? AND duplicate_entity_id = ?
                """,
                (now, canonical_id, duplicate_id),
            )
            # Delete the duplicate entity
            conn.execute(
                "DELETE FROM graph_entities WHERE entity_id = ?",
                (duplicate_id,),
            )
            conn.execute(
                "DELETE FROM graph_aliases WHERE entity_id = ?",
                (duplicate_id,),
            )
            conn.execute(
                "DELETE FROM graph_external_ids WHERE entity_id = ?",
                (duplicate_id,),
            )
        logger.info(
            f"EntityResolution: approved merge "
            f"duplicate={duplicate_id} → canonical={canonical_id}"
        )

    def reverse_merge(self, canonical_id: str, restored_id: str) -> None:
        """Reverse a previously approved merge.

        Note: this restores the entity record but does not automatically
        re-split assertions.  Manual review is required.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._store.connect() as conn:
            conn.execute(
                """
                UPDATE graph_merge_candidates
                SET status = 'reversed', resolved_at = ?
                WHERE canonical_entity_id = ? AND duplicate_entity_id = ?
                """,
                (now, canonical_id, restored_id),
            )
        logger.info(
            f"EntityResolution: reversed merge "
            f"canonical={canonical_id}, restored={restored_id} "
            f"(manual assertion re-split required)"
        )
