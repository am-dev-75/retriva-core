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

"""Spec 028 P3: KB registry migration SQLite -> PostgreSQL."""

from __future__ import annotations

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from retriva.knowledge.authority import (  # noqa: E402
    AuthorityState,
    KnowledgeAuthority,
)
from retriva.knowledge.kb_registry import (  # noqa: E402
    KBMigrationError,
    KBRegistryMigrator,
    KnowledgeBaseRegistry,
)

TENANT = "tenant-kb"


@pytest.fixture(autouse=True)
def _reset_authority(knowledge_repo):
    with knowledge_repo.transaction(privileged=True) as cur:
        cur.execute(
            "UPDATE knowledge.authority SET state='schema_ready', "
            "authoritative=FALSE, native_ingestion_available=FALSE, "
            "adoption_run_ref=NULL WHERE singleton=TRUE")
    yield


@pytest.fixture()
def seeded_registry():
    from retriva.domain.kb import KBRegistry, KBConflictError

    registry = KBRegistry()
    try:
        registry.create(name="Adopted Base", kb_id="adopted-base",
                        description="legacy", collection_name="col_x",
                        settings={"theme": "dark"})
    except KBConflictError:
        pass
    return registry


def test_kb_dry_run_then_apply_idempotent(knowledge_repo, seeded_registry):
    migrator = KBRegistryMigrator(knowledge_repo)
    evidence = migrator.read_sqlite()
    assert evidence["rows"]
    dry = migrator.apply(TENANT, evidence, apply=False)
    assert dry.applied == 0
    assert dry.valid
    applied = migrator.apply(TENANT, evidence, apply=True)
    assert applied.applied >= 1
    assert applied.verified is True
    second = migrator.apply(TENANT, evidence, apply=True)
    assert second.applied == 0
    assert second.skipped_existing >= 1


def test_kb_invalid_rows_reported_and_refused(knowledge_repo):
    migrator = KBRegistryMigrator(knowledge_repo)
    evidence = {"rows": [{
        "kb_id": "Bad ID!", "collection_name": "c",
        "name": "x", "settings": {}, "created_at": "", "updated_at": ""}]}
    report = migrator.apply(TENANT, evidence, apply=True)
    assert report.invalid
    assert report.applied == 0


def test_kb_config_bound_validation(knowledge_repo):
    migrator = KBRegistryMigrator(knowledge_repo)
    big = {"blob": "x" * 5000}
    evidence = {"rows": [{
        "kb_id": "ok-id", "collection_name": "c", "name": "x",
        "settings": big, "created_at": "", "updated_at": ""}]}
    report = migrator.validate(evidence)
    assert any("config_too_large" in r["problems"]
               for r in report.invalid)


def test_runtime_cutover_switches_to_postgresql(knowledge_repo,
                                                seeded_registry,
                                                equivalence_evidence):
    migrator = KBRegistryMigrator(knowledge_repo)
    evidence = migrator.read_sqlite()
    migrator.apply(TENANT, evidence, apply=True)
    accessor = KnowledgeBaseRegistry(knowledge_repo)
    assert accessor.mode() == "legacy-sqlite"
    authority = KnowledgeAuthority(knowledge_repo)
    authority.transition(AuthorityState.ADOPTION_PENDING, operator="ops")
    authority.transition(AuthorityState.ADOPTION_VERIFIED, operator="ops")
    authority.set_authoritative(
        operator="ops",
        evidence={n: True for n in authority.cutover_gates({})},
        equivalence_op_id=equivalence_evidence)
    assert accessor.mode() == "postgresql"
    rows = accessor.list(TENANT)
    assert any(r["kb_id"] == "adopted-base" for r in rows)
    # Legacy SQLite write authority is refused after cutover.
    with pytest.raises(KBMigrationError):
        accessor.refuse_legacy_write()
    authority.transition(AuthorityState.SUSPENDED, operator="ops")
