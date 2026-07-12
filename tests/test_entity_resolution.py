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

"""Unit tests for EntityResolutionService."""

import pytest
from retriva.graph.contracts import Entity, EntityCategory
from retriva.graph.entity_resolution import EntityResolutionService
from retriva.graph.stores.sqlite_graph_store import SQLiteGraphStore


@pytest.fixture
def resolver(tmp_path):
    store = SQLiteGraphStore(db_path=str(tmp_path / "test_graph.db"))
    return EntityResolutionService(store=store)


class TestEntityResolution:
    def test_new_entity_created(self, resolver):
        candidate = Entity(
            tenant_id="col1", kb_id="kb1",
            name="Acme Corp", category=EntityCategory.ORGANIZATION,
        )
        result = resolver.resolve(candidate)
        assert result.entity_id.startswith("ent_")
        assert result.name == "Acme Corp"
        assert result.security_scope == ["kb1"]

    def test_exact_match_returns_existing(self, resolver):
        candidate1 = Entity(
            tenant_id="col1", kb_id="kb1",
            name="Acme Corp", category=EntityCategory.ORGANIZATION,
        )
        result1 = resolver.resolve(candidate1)

        candidate2 = Entity(
            tenant_id="col1", kb_id="kb1",
            name="Acme Corp", category=EntityCategory.ORGANIZATION,
        )
        result2 = resolver.resolve(candidate2)

        assert result1.entity_id == result2.entity_id

    def test_alias_match_returns_existing(self, resolver):
        candidate1 = Entity(
            tenant_id="col1", kb_id="kb1",
            name="Acme Corp", category=EntityCategory.ORGANIZATION,
            aliases=["ACME"],
        )
        result1 = resolver.resolve(candidate1)

        candidate2 = Entity(
            tenant_id="col1", kb_id="kb1",
            name="ACME", category=EntityCategory.ORGANIZATION,
        )
        result2 = resolver.resolve(candidate2)

        assert result1.entity_id == result2.entity_id

    def test_external_id_match(self, resolver):
        candidate1 = Entity(
            tenant_id="col1", kb_id="kb1",
            name="Acme Corp", category=EntityCategory.ORGANIZATION,
            external_ids={"salesforce": "001A"},
        )
        result1 = resolver.resolve(candidate1)

        candidate2 = Entity(
            tenant_id="col1", kb_id="kb1",
            name="Different Name", category=EntityCategory.ORGANIZATION,
            external_ids={"salesforce": "001A"},
        )
        result2 = resolver.resolve(candidate2)

        assert result1.entity_id == result2.entity_id

    def test_tenant_isolation(self, resolver):
        candidate1 = Entity(
            tenant_id="col1", kb_id="kb1",
            name="Acme Corp", category=EntityCategory.ORGANIZATION,
        )
        result1 = resolver.resolve(candidate1)

        candidate2 = Entity(
            tenant_id="col2", kb_id="kb1",
            name="Acme Corp", category=EntityCategory.ORGANIZATION,
        )
        result2 = resolver.resolve(candidate2)

        assert result1.entity_id != result2.entity_id

    def test_merge_aliases_into_canonical(self, resolver):
        candidate1 = Entity(
            tenant_id="col1", kb_id="kb1",
            name="Acme Corp", category=EntityCategory.ORGANIZATION,
            aliases=["ACME"],
        )
        result1 = resolver.resolve(candidate1)

        candidate2 = Entity(
            tenant_id="col1", kb_id="kb1",
            name="Acme Corp", category=EntityCategory.ORGANIZATION,
            aliases=["Acme Inc", "ACME Corporation"],
        )
        result2 = resolver.resolve(candidate2)

        assert result1.entity_id == result2.entity_id
        assert "Acme Inc" in result2.aliases
        assert "ACME Corporation" in result2.aliases

    def test_deterministic_id(self, resolver):
        """Same name + same scope → same canonical ID."""
        candidate1 = Entity(
            tenant_id="col1", kb_id="kb1",
            name="Acme Corp", category=EntityCategory.ORGANIZATION,
        )
        result1 = resolver.resolve(candidate1)

        # Simulate a fresh resolution (the store already has the entity)
        candidate2 = Entity(
            tenant_id="col1", kb_id="kb1",
            name="Acme Corp", category=EntityCategory.ORGANIZATION,
        )
        result2 = resolver.resolve(candidate2)

        assert result1.entity_id == result2.entity_id
