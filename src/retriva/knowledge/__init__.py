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

"""Core-owned knowledge and ingestion metadata domain (Spec 028;
ADR-033).

PostgreSQL schema ``knowledge`` is the authoritative system of record
for source/document/version identity, KB registry, per-point manifests,
Qdrant operation evidence, provenance, deletion and authority state.
Qdrant remains the vector system of record; durable Core jobs remain
the authoritative asynchronous lifecycle store.
"""

from retriva.knowledge.authority import (
    AuthorityError,
    AuthorityState,
    KnowledgeAuthority,
    KnowledgeReadiness,
)
from retriva.knowledge.contracts import (
    chunk_contract_version,
    embedding_contract_version,
    parser_contract_version,
)
from retriva.knowledge.domain import (
    DocumentLifecycle,
    IngestionMode,
    IngestionSyncState,
    OpState,
    OpType,
    Provenance,
    TransitionError,
    VersionStatus,
    can_transition_ingestion,
    can_transition_version,
)
from retriva.knowledge.ids import (
    NAMESPACE_CONNECTOR,
    NAMESPACE_INTERNAL,
    NAMESPACE_MEDIAWIKI,
    NAMESPACE_PATH,
    NAMESPACE_UPLOAD,
    SourceIdentity,
    SourceIdentityError,
    derive_point_id,
    new_id,
    normalize_source_identity,
)
from retriva.knowledge.repository import KnowledgeRepository
from retriva.knowledge.service import KnowledgeService
from retriva.knowledge.integration import KnowledgeIntegration

__all__ = [
    "AuthorityError",
    "AuthorityState",
    "KnowledgeAuthority",
    "KnowledgeIntegration",
    "KnowledgeReadiness",
    "KnowledgeRepository",
    "KnowledgeService",
    "chunk_contract_version",
    "embedding_contract_version",
    "parser_contract_version",
    "DocumentLifecycle",
    "IngestionMode",
    "IngestionSyncState",
    "OpState",
    "OpType",
    "Provenance",
    "TransitionError",
    "VersionStatus",
    "can_transition_ingestion",
    "can_transition_version",
    "NAMESPACE_CONNECTOR",
    "NAMESPACE_INTERNAL",
    "NAMESPACE_MEDIAWIKI",
    "NAMESPACE_PATH",
    "NAMESPACE_UPLOAD",
    "SourceIdentity",
    "SourceIdentityError",
    "derive_point_id",
    "new_id",
    "normalize_source_identity",
]
