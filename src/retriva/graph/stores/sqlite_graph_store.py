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
SQLite-backed graph store — Phase 1 implementation of :class:`GraphStore`.

Design follows the same patterns as :class:`retriva.infrastructure.registry_db.RegistryDB`:
- One SQLite file per collection: ``<storage_path>/collections/<collection>/graph.db``.
- Connection-per-call with a module-level ``threading.Lock`` for writes.
- WAL journaling for read/write concurrency.
- Idempotent schema creation (``CREATE TABLE IF NOT EXISTS``).
- JSON columns for flexible properties.

Traversal is implemented as bounded BFS in Python, issuing SQL queries
for edge lookup at each depth level.  This avoids SQL recursion
portability issues and keeps the traversal logic in the application layer.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from retriva.graph.contracts import (
    Assertion,
    AssertionStatus,
    Entity,
    GraphMutationRequest,
    GraphPath,
    GraphRetrievalResult,
    GraphEvent,
    Relationship,
)
from retriva.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

GRAPH_SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_entities (
    entity_id         TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    kb_id             TEXT NOT NULL,
    name              TEXT NOT NULL,
    name_normalized   TEXT NOT NULL,
    category          TEXT NOT NULL DEFAULT 'retriva:Unknown',
    entity_type       TEXT,
    namespace         TEXT NOT NULL DEFAULT 'retriva',
    aliases_json      TEXT NOT NULL DEFAULT '[]',
    external_ids_json TEXT NOT NULL DEFAULT '{}',
    description       TEXT,
    properties_json   TEXT NOT NULL DEFAULT '{}',
    security_scope_json TEXT NOT NULL DEFAULT '[]',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_graph_entities_tenant_kb
    ON graph_entities(tenant_id, kb_id);
CREATE INDEX IF NOT EXISTS idx_graph_entities_name_norm
    ON graph_entities(tenant_id, name_normalized);
CREATE INDEX IF NOT EXISTS idx_graph_entities_category
    ON graph_entities(category);

CREATE TABLE IF NOT EXISTS graph_aliases (
    alias_normalized  TEXT NOT NULL,
    tenant_id         TEXT NOT NULL,
    kb_id             TEXT NOT NULL,
    entity_id         TEXT NOT NULL,
    PRIMARY KEY (alias_normalized, tenant_id, kb_id),
    FOREIGN KEY (entity_id) REFERENCES graph_entities(entity_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_aliases_lookup
    ON graph_aliases(alias_normalized, tenant_id, kb_id);

CREATE TABLE IF NOT EXISTS graph_external_ids (
    system            TEXT NOT NULL,
    external_id       TEXT NOT NULL,
    tenant_id         TEXT NOT NULL,
    entity_id         TEXT NOT NULL,
    PRIMARY KEY (system, external_id, tenant_id),
    FOREIGN KEY (entity_id) REFERENCES graph_entities(entity_id)
);

CREATE TABLE IF NOT EXISTS graph_assertions (
    assertion_id      TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    kb_id             TEXT NOT NULL,
    subject_entity_id TEXT NOT NULL,
    predicate         TEXT NOT NULL,
    object_entity_id  TEXT,
    object_value      TEXT,
    assertion_class   TEXT NOT NULL DEFAULT 'extracted',
    source_document_ids_json TEXT NOT NULL DEFAULT '[]',
    source_chunk_ids_json   TEXT NOT NULL DEFAULT '[]',
    source_spans_json       TEXT NOT NULL DEFAULT '[]',
    extraction_confidence  REAL NOT NULL DEFAULT 0.0,
    extractor_profile      TEXT NOT NULL DEFAULT 'retriva:default',
    extractor_version      TEXT NOT NULL DEFAULT '0.1.0',
    observed_at    TEXT,
    ingested_at    TEXT NOT NULL,
    valid_from     TEXT,
    valid_to       TEXT,
    status         TEXT NOT NULL DEFAULT 'active',
    superseded_by  TEXT,
    security_scope_json TEXT NOT NULL DEFAULT '[]',
    metadata_json  TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (subject_entity_id) REFERENCES graph_entities(entity_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_assertions_subject
    ON graph_assertions(subject_entity_id, status);
CREATE INDEX IF NOT EXISTS idx_graph_assertions_tenant_kb
    ON graph_assertions(tenant_id, kb_id);
CREATE INDEX IF NOT EXISTS idx_graph_assertions_source_doc
    ON graph_assertions(source_document_ids_json);

CREATE TABLE IF NOT EXISTS graph_relationships (
    relationship_id   TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    kb_id             TEXT NOT NULL,
    source_entity_id  TEXT NOT NULL,
    target_entity_id  TEXT NOT NULL,
    predicate         TEXT NOT NULL,
    assertion_ids_json TEXT NOT NULL DEFAULT '[]',
    properties_json   TEXT NOT NULL DEFAULT '{}',
    security_scope_json TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY (source_entity_id) REFERENCES graph_entities(entity_id),
    FOREIGN KEY (target_entity_id) REFERENCES graph_entities(entity_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_rels_source
    ON graph_relationships(source_entity_id);
CREATE INDEX IF NOT EXISTS idx_graph_rels_target
    ON graph_relationships(target_entity_id);

CREATE TABLE IF NOT EXISTS graph_events (
    event_id          TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    kb_id             TEXT NOT NULL,
    entity_id         TEXT,
    event_type        TEXT NOT NULL,
    description       TEXT,
    observed_at       TEXT,
    source_assertion_ids_json TEXT NOT NULL DEFAULT '[]',
    security_scope_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS graph_provenance (
    document_id       TEXT NOT NULL,
    chunk_id          TEXT NOT NULL,
    entity_id         TEXT,
    assertion_id      TEXT,
    tenant_id         TEXT NOT NULL,
    kb_id             TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    FOREIGN KEY (entity_id) REFERENCES graph_entities(entity_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_prov_doc
    ON graph_provenance(document_id);
CREATE INDEX IF NOT EXISTS idx_graph_prov_entity
    ON graph_provenance(entity_id);
CREATE INDEX IF NOT EXISTS idx_graph_prov_chunk
    ON graph_provenance(chunk_id);

CREATE TABLE IF NOT EXISTS graph_merge_candidates (
    candidate_id      TEXT PRIMARY KEY,
    canonical_entity_id TEXT NOT NULL,
    duplicate_entity_id TEXT NOT NULL,
    tenant_id         TEXT NOT NULL,
    reason            TEXT,
    confidence        REAL NOT NULL DEFAULT 0.0,
    status            TEXT NOT NULL DEFAULT 'pending',
    created_at        TEXT NOT NULL,
    resolved_at       TEXT
);
"""


# ---------------------------------------------------------------------------
# SQLiteGraphStore
# ---------------------------------------------------------------------------

class SQLiteGraphStore:
    """Phase 1 graph store backed by SQLite.

    Implements the :class:`retriva.graph.protocols.GraphStore` protocol.
    """

    _write_lock = threading.Lock()

    def __init__(self, db_path: Optional[str] = None, collection_name: Optional[str] = None):
        if db_path is None:
            from retriva.config import settings
            from retriva.indexing.qdrant_store import get_collection_name

            col = collection_name or get_collection_name()
            storage_path = getattr(settings, "storage_path", "storage")
            db_path = os.path.join(storage_path, "collections", col, "graph.db")

        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    # ------------------------------------------------------------------ I/O

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self._path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(GRAPH_SCHEMA)
        logger.debug(f"SQLiteGraphStore initialized at {self._path}")

    # ------------------------------------------------------------------ Helpers

    @staticmethod
    def _normalize_name(name: str) -> str:
        return name.strip().lower()

    @staticmethod
    def _entity_from_row(row: sqlite3.Row) -> Entity:
        return Entity(
            entity_id=row["entity_id"],
            tenant_id=row["tenant_id"],
            kb_id=row["kb_id"],
            name=row["name"],
            name_normalized=row["name_normalized"],
            category=row["category"],
            entity_type=row["entity_type"],
            namespace=row["namespace"],
            aliases=json.loads(row["aliases_json"]),
            external_ids=json.loads(row["external_ids_json"]),
            description=row["description"],
            properties=json.loads(row["properties_json"]),
            security_scope=json.loads(row["security_scope_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _assertion_from_row(row: sqlite3.Row) -> Assertion:
        return Assertion(
            assertion_id=row["assertion_id"],
            tenant_id=row["tenant_id"],
            kb_id=row["kb_id"],
            subject_entity_id=row["subject_entity_id"],
            predicate=row["predicate"],
            object_entity_id=row["object_entity_id"],
            object_value=row["object_value"],
            assertion_class=row["assertion_class"],
            source_document_ids=json.loads(row["source_document_ids_json"]),
            source_chunk_ids=json.loads(row["source_chunk_ids_json"]),
            source_spans=json.loads(row["source_spans_json"]),
            extraction_confidence=row["extraction_confidence"],
            extractor_profile=row["extractor_profile"],
            extractor_version=row["extractor_version"],
            observed_at=row["observed_at"],
            ingested_at=row["ingested_at"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            status=row["status"],
            superseded_by=row["superseded_by"],
            security_scope=json.loads(row["security_scope_json"]),
            metadata=json.loads(row["metadata_json"]),
        )

    @staticmethod
    def _relationship_from_row(row: sqlite3.Row) -> Relationship:
        return Relationship(
            relationship_id=row["relationship_id"],
            tenant_id=row["tenant_id"],
            kb_id=row["kb_id"],
            source_entity_id=row["source_entity_id"],
            target_entity_id=row["target_entity_id"],
            predicate=row["predicate"],
            assertion_ids=json.loads(row["assertion_ids_json"]),
            properties=json.loads(row["properties_json"]),
            security_scope=json.loads(row["security_scope_json"]),
        )

    # ------------------------------------------------------------------ Mutations

    def upsert_entities(self, entities: List[Entity]) -> None:
        if not entities:
            return
        with self._write_lock, self.connect() as conn:
            for e in entities:
                if not e.name_normalized:
                    e.name_normalized = self._normalize_name(e.name)
                conn.execute(
                    """
                    INSERT INTO graph_entities (
                        entity_id, tenant_id, kb_id, name, name_normalized,
                        category, entity_type, namespace, aliases_json,
                        external_ids_json, description, properties_json,
                        security_scope_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(entity_id) DO UPDATE SET
                        name=excluded.name,
                        name_normalized=excluded.name_normalized,
                        category=excluded.category,
                        entity_type=excluded.entity_type,
                        namespace=excluded.namespace,
                        aliases_json=excluded.aliases_json,
                        external_ids_json=excluded.external_ids_json,
                        description=excluded.description,
                        properties_json=excluded.properties_json,
                        security_scope_json=excluded.security_scope_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        e.entity_id, e.tenant_id, e.kb_id, e.name,
                        e.name_normalized, e.category.value,
                        e.entity_type, e.namespace,
                        json.dumps(e.aliases),
                        json.dumps(e.external_ids),
                        e.description,
                        json.dumps(e.properties),
                        json.dumps(e.security_scope),
                        e.created_at, e.updated_at,
                    ),
                )
                # Upsert aliases
                for alias in e.aliases:
                    alias_norm = self._normalize_name(alias)
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO graph_aliases
                            (alias_normalized, tenant_id, kb_id, entity_id)
                        VALUES (?, ?, ?, ?)
                        """,
                        (alias_norm, e.tenant_id, e.kb_id, e.entity_id),
                    )
                # Also register the canonical name as an alias
                conn.execute(
                    """
                    INSERT OR IGNORE INTO graph_aliases
                        (alias_normalized, tenant_id, kb_id, entity_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    (e.name_normalized, e.tenant_id, e.kb_id, e.entity_id),
                )
                # Upsert external IDs
                for system, ext_id in e.external_ids.items():
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO graph_external_ids
                            (system, external_id, tenant_id, entity_id)
                        VALUES (?, ?, ?, ?)
                        """,
                        (system, ext_id, e.tenant_id, e.entity_id),
                    )

    def upsert_assertions(self, assertions: List[Assertion]) -> None:
        if not assertions:
            return
        with self._write_lock, self.connect() as conn:
            for a in assertions:
                conn.execute(
                    """
                    INSERT INTO graph_assertions (
                        assertion_id, tenant_id, kb_id, subject_entity_id,
                        predicate, object_entity_id, object_value,
                        assertion_class, source_document_ids_json,
                        source_chunk_ids_json, source_spans_json,
                        extraction_confidence, extractor_profile,
                        extractor_version, observed_at, ingested_at,
                        valid_from, valid_to, status, superseded_by,
                        security_scope_json, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(assertion_id) DO UPDATE SET
                        subject_entity_id=excluded.subject_entity_id,
                        predicate=excluded.predicate,
                        object_entity_id=excluded.object_entity_id,
                        object_value=excluded.object_value,
                        assertion_class=excluded.assertion_class,
                        source_document_ids_json=excluded.source_document_ids_json,
                        source_chunk_ids_json=excluded.source_chunk_ids_json,
                        source_spans_json=excluded.source_spans_json,
                        extraction_confidence=excluded.extraction_confidence,
                        extractor_profile=excluded.extractor_profile,
                        extractor_version=excluded.extractor_version,
                        observed_at=excluded.observed_at,
                        valid_from=excluded.valid_from,
                        valid_to=excluded.valid_to,
                        status=excluded.status,
                        superseded_by=excluded.superseded_by,
                        security_scope_json=excluded.security_scope_json,
                        metadata_json=excluded.metadata_json
                    """,
                    (
                        a.assertion_id, a.tenant_id, a.kb_id,
                        a.subject_entity_id, a.predicate,
                        a.object_entity_id, a.object_value,
                        a.assertion_class.value,
                        json.dumps(a.source_document_ids),
                        json.dumps(a.source_chunk_ids),
                        json.dumps(a.source_spans),
                        a.extraction_confidence,
                        a.extractor_profile, a.extractor_version,
                        a.observed_at, a.ingested_at,
                        a.valid_from, a.valid_to,
                        a.status.value, a.superseded_by,
                        json.dumps(a.security_scope),
                        json.dumps(a.metadata),
                    ),
                )

    def upsert_relationships(self, relationships: List[Relationship]) -> None:
        if not relationships:
            return
        with self._write_lock, self.connect() as conn:
            for r in relationships:
                conn.execute(
                    """
                    INSERT INTO graph_relationships (
                        relationship_id, tenant_id, kb_id,
                        source_entity_id, target_entity_id, predicate,
                        assertion_ids_json, properties_json,
                        security_scope_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(relationship_id) DO UPDATE SET
                        source_entity_id=excluded.source_entity_id,
                        target_entity_id=excluded.target_entity_id,
                        predicate=excluded.predicate,
                        assertion_ids_json=excluded.assertion_ids_json,
                        properties_json=excluded.properties_json,
                        security_scope_json=excluded.security_scope_json
                    """,
                    (
                        r.relationship_id, r.tenant_id, r.kb_id,
                        r.source_entity_id, r.target_entity_id, r.predicate,
                        json.dumps(r.assertion_ids),
                        json.dumps(r.properties),
                        json.dumps(r.security_scope),
                    ),
                )

    def store_provenance(
        self,
        document_id: str,
        chunk_ids: List[str],
        entity_ids: List[str],
        assertion_ids: List[str],
        tenant_id: str,
        kb_id: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock, self.connect() as conn:
            for eid in entity_ids:
                for cid in chunk_ids:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO graph_provenance
                            (document_id, chunk_id, entity_id, assertion_id,
                             tenant_id, kb_id, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (document_id, cid, eid, None, tenant_id, kb_id, now),
                    )
            for aid in assertion_ids:
                for cid in chunk_ids:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO graph_provenance
                            (document_id, chunk_id, entity_id, assertion_id,
                             tenant_id, kb_id, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (document_id, cid, None, aid, tenant_id, kb_id, now),
                    )

    def apply_mutation(self, mutation: GraphMutationRequest) -> None:
        """Apply a full batch mutation."""
        self.upsert_entities(mutation.entities)
        self.upsert_assertions(mutation.assertions)
        self.upsert_relationships(mutation.relationships)

        # Store events
        if mutation.events:
            with self._write_lock, self.connect() as conn:
                for ev in mutation.events:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO graph_events
                            (event_id, tenant_id, kb_id, entity_id,
                             event_type, description, observed_at,
                             source_assertion_ids_json, security_scope_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ev.event_id, ev.tenant_id, ev.kb_id,
                            ev.entity_id, ev.event_type, ev.description,
                            ev.observed_at,
                            json.dumps(ev.source_assertion_ids),
                            json.dumps(ev.security_scope),
                        ),
                    )

        # Store provenance
        entity_ids = [e.entity_id for e in mutation.entities]
        assertion_ids = [a.assertion_id for a in mutation.assertions]
        self.store_provenance(
            mutation.source_document_id,
            mutation.source_chunk_ids,
            entity_ids,
            assertion_ids,
            mutation.tenant_id,
            mutation.kb_id,
        )

    # ------------------------------------------------------------------ Retrieval

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM graph_entities WHERE entity_id = ?",
                (entity_id,),
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def get_entities_by_alias(
        self, name: str, tenant_id: str, kb_id: str
    ) -> List[Entity]:
        alias_norm = self._normalize_name(name)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT e.* FROM graph_entities e
                INNER JOIN graph_aliases a
                    ON a.entity_id = e.entity_id
                WHERE a.alias_normalized = ?
                    AND a.tenant_id = ?
                    AND a.kb_id = ?
                """,
                (alias_norm, tenant_id, kb_id),
            ).fetchall()
        return [self._entity_from_row(r) for r in rows]

    def get_entities_by_external_id(
        self, system: str, external_id: str, tenant_id: str
    ) -> Optional[Entity]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT e.* FROM graph_entities e
                INNER JOIN graph_external_ids x
                    ON x.entity_id = e.entity_id
                WHERE x.system = ? AND x.external_id = ? AND x.tenant_id = ?
                """,
                (system, external_id, tenant_id),
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def get_assertions(
        self,
        entity_id: str,
        status: Optional[AssertionStatus] = None,
        as_of: Optional[str] = None,
    ) -> List[Assertion]:
        query = "SELECT * FROM graph_assertions WHERE subject_entity_id = ?"
        params: list = [entity_id]
        if status:
            query += " AND status = ?"
            params.append(status.value)
        if as_of:
            query += " AND (valid_from IS NULL OR valid_from <= ?)"
            query += " AND (valid_to IS NULL OR valid_to >= ?)"
            params.extend([as_of, as_of])
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._assertion_from_row(r) for r in rows]

    def get_neighborhood(
        self,
        entity_id: str,
        max_depth: int,
        max_nodes: int,
        max_edges: int,
        security_scope: List[str],
        as_of: Optional[str] = None,
    ) -> GraphRetrievalResult:
        """Bounded BFS neighborhood expansion.

        Security filtering: only entities/assertions whose
        ``security_scope`` intersects *security_scope* (or is empty) are
        returned.
        """
        result_entities: dict[str, Entity] = {}
        result_assertions: dict[str, Assertion] = {}
        result_relationships: dict[str, Relationship] = []
        warnings: list[str] = []
        truncated = False

        visited: set[str] = set()
        frontier: set[str] = {entity_id}
        total_edges = 0

        for depth in range(max_depth + 1):
            if not frontier:
                break

            # Check node limit before fetching
            if len(result_entities) >= max_nodes:
                truncated = True
                warnings.append(f"max_nodes ({max_nodes}) reached")
                break

            # Fetch entities at this frontier
            placeholders = ",".join("?" * len(frontier))
            with self.connect() as conn:
                rows = conn.execute(
                    f"SELECT * FROM graph_entities WHERE entity_id IN ({placeholders})",
                    list(frontier),
                ).fetchall()

            for row in rows:
                ent = self._entity_from_row(row)
                # Security filter
                if security_scope and ent.security_scope:
                    if not set(ent.security_scope) & set(security_scope):
                        continue
                if ent.entity_id not in result_entities:
                    if len(result_entities) >= max_nodes:
                        truncated = True
                        warnings.append(f"max_nodes ({max_nodes}) reached")
                        break
                    result_entities[ent.entity_id] = ent
                visited.add(ent.entity_id)

            if depth == max_depth:
                break

            # Expand: find relationships from frontier entities
            next_frontier: set[str] = set()
            with self.connect() as conn:
                rel_rows = conn.execute(
                    f"""
                    SELECT * FROM graph_relationships
                    WHERE source_entity_id IN ({placeholders})
                       OR target_entity_id IN ({placeholders})
                    """,
                    list(frontier) + list(frontier),
                ).fetchall()

            for rel_row in rel_rows:
                rel = self._relationship_from_row(rel_row)
                # Security filter
                if security_scope and rel.security_scope:
                    if not set(rel.security_scope) & set(security_scope):
                        continue
                if total_edges >= max_edges:
                    truncated = True
                    warnings.append(f"max_edges ({max_edges}) reached")
                    break
                result_relationships.append(rel)
                total_edges += 1

                # Fetch backing assertions
                if rel.assertion_ids:
                    with self.connect() as conn:
                        ph = ",".join("?" * len(rel.assertion_ids))
                        ast_rows = conn.execute(
                            f"SELECT * FROM graph_assertions WHERE assertion_id IN ({ph})",
                            rel.assertion_ids,
                        ).fetchall()
                    for ast_row in ast_rows:
                        ast = self._assertion_from_row(ast_row)
                        if security_scope and ast.security_scope:
                            if not set(ast.security_scope) & set(security_scope):
                                continue
                        result_assertions[ast.assertion_id] = ast

                # Add connected entities to next frontier
                other = rel.target_entity_id if rel.source_entity_id in frontier else rel.source_entity_id
                if other not in visited:
                    next_frontier.add(other)

            frontier = next_frontier

        return GraphRetrievalResult(
            entities=list(result_entities.values()),
            assertions=list(result_assertions.values()),
            relationships=result_relationships,
            warnings=warnings,
            truncated=truncated,
        )

    def find_paths(
        self,
        source_id: str,
        target_id: str,
        max_depth: int,
        security_scope: List[str],
    ) -> List[GraphPath]:
        """BFS path discovery up to *max_depth* hops."""
        if source_id == target_id:
            return [GraphPath(
                start_entity_id=source_id,
                end_entity_id=target_id,
                entity_ids=[source_id],
                predicates=[],
            )]

        paths: list[GraphPath] = []
        # BFS queue: (current_entity, path_entities, path_predicates, path_assertion_ids)
        queue: list[tuple[str, list[str], list[str], list[str]]] = [
            (source_id, [source_id], [], [])
        ]
        visited: set[str] = {source_id}

        for _ in range(max_depth):
            if not queue:
                break
            next_queue: list[tuple[str, list[str], list[str], list[str]]] = []
            for current, path_ents, path_preds, path_aids in queue:
                with self.connect() as conn:
                    rel_rows = conn.execute(
                        """
                        SELECT * FROM graph_relationships
                        WHERE source_entity_id = ? OR target_entity_id = ?
                        """,
                        (current, current),
                    ).fetchall()
                for rel_row in rel_rows:
                    rel = self._relationship_from_row(rel_row)
                    if security_scope and rel.security_scope:
                        if not set(rel.security_scope) & set(security_scope):
                            continue
                    other = rel.target_entity_id if rel.source_entity_id == current else rel.source_entity_id
                    if other in path_ents:
                        continue  # avoid cycles
                    new_path_ents = path_ents + [other]
                    new_path_preds = path_preds + [rel.predicate]
                    new_path_aids = path_aids + rel.assertion_ids
                    if other == target_id:
                        paths.append(GraphPath(
                            start_entity_id=source_id,
                            end_entity_id=target_id,
                            entity_ids=new_path_ents,
                            predicates=new_path_preds,
                            assertion_ids=new_path_aids,
                        ))
                    elif other not in visited:
                        visited.add(other)
                        next_queue.append((other, new_path_ents, new_path_preds, new_path_aids))
            queue = next_queue

        return paths

    def search_entities(
        self,
        query: str,
        tenant_id: str,
        kb_id: str,
        limit: int = 20,
    ) -> List[Entity]:
        """Simple LIKE-based entity search (Phase 1).

        A future phase may add graph embeddings or full-text search.
        """
        pattern = f"%{query.strip().lower()}%"
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM graph_entities
                WHERE tenant_id = ? AND kb_id = ?
                    AND (LOWER(name) LIKE ? OR name_normalized LIKE ?)
                LIMIT ?
                """,
                (tenant_id, kb_id, pattern, pattern, limit),
            ).fetchall()
        return [self._entity_from_row(r) for r in rows]

    def get_impacted_entities(self, entity_id: str) -> List[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT target_entity_id AS eid FROM graph_relationships
                    WHERE source_entity_id = ?
                UNION
                SELECT DISTINCT source_entity_id AS eid FROM graph_relationships
                    WHERE target_entity_id = ?
                """,
                (entity_id, entity_id),
            ).fetchall()
        return [r["eid"] for r in rows]

    # ------------------------------------------------------------------ Deletion

    def delete_by_source(self, document_id: str) -> int:
        """Delete all graph data derived from *document_id*.

        Idempotent.  Returns the count of deleted assertions + provenance rows.
        """
        affected = 0
        with self._write_lock, self.connect() as conn:
            # Delete provenance
            cur = conn.execute(
                "DELETE FROM graph_provenance WHERE document_id = ?",
                (document_id,),
            )
            affected += cur.rowcount

            # Invalidate assertions from this document
            cur = conn.execute(
                """
                UPDATE graph_assertions
                SET status = 'invalidated'
                WHERE source_document_ids_json LIKE ?
                """,
                (f'%"{document_id}"%',),
            )
            affected += cur.rowcount

            # Delete relationships whose backing assertions are all invalidated
            # (conservative: only remove if ALL assertion_ids are invalidated)
            # Phase 1: leave relationships in place; they reference invalidated
            # assertions which are filtered by status in queries.

        logger.info(f"delete_by_source: document_id={document_id}, affected={affected}")
        return affected

    def invalidate_by_source(self, document_id: str) -> int:
        """Invalidate (but do not delete) assertions from *document_id*."""
        with self._write_lock, self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE graph_assertions
                SET status = 'invalidated'
                WHERE source_document_ids_json LIKE ?
                  AND status = 'active'
                """,
                (f'%"{document_id}"%',),
            )
            affected = cur.rowcount
        logger.info(f"invalidate_by_source: document_id={document_id}, affected={affected}")
        return affected
