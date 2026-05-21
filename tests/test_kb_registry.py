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
Phase 1 unit tests — KB registry domain layer.

Covers:
- Slugification rules and rejection of invalid names.
- Field validation (name length, description length, settings JSON).
- CRUD: create, list, get, update, delete.
- Conflict on duplicate kb_id.
- 404 on missing kb_id (get/update/delete).
- Immutability of the 'default' KB (delete refused).
- Seeding is idempotent.
- Each test runs against an isolated SQLite file (no shared state).
"""

from __future__ import annotations

import os
import tempfile

import pytest

from retriva.domain.kb import (
    DEFAULT_KB_ID,
    KBConflictError,
    KBImmutableError,
    KBNotFoundError,
    KBRegistry,
    KBValidationError,
    seed_default_kb,
    slugify,
)
from retriva.infrastructure.registry_db import RegistryDB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    """A fresh RegistryDB pointing at a temp SQLite file (per-test isolation)."""
    db_path = tmp_path / "registry.db"
    return RegistryDB(db_path=str(db_path))


@pytest.fixture
def registry(tmp_db):
    return KBRegistry(db=tmp_db)


# ---------------------------------------------------------------------------
# Slugification (RD-1)
# ---------------------------------------------------------------------------

class TestSlugify:
    def test_simple_lowercase(self):
        assert slugify("Engineering") == "engineering"

    def test_spaces_become_hyphens(self):
        assert slugify("Engineering Docs") == "engineering-docs"

    def test_punctuation_collapsed(self):
        assert slugify("Q1 / 2026 -- Reports!") == "q1-2026-reports"

    def test_leading_trailing_stripped(self):
        assert slugify("  -hello-  ") == "hello"

    def test_truncated_to_64(self):
        long = "a" * 200
        out = slugify(long)
        assert len(out) <= 64
        assert out == "a" * 64

    def test_empty_rejected(self):
        with pytest.raises(KBValidationError):
            slugify("")

    def test_only_punctuation_rejected(self):
        with pytest.raises(KBValidationError):
            slugify("!!!---???")

    def test_non_string_rejected(self):
        with pytest.raises(KBValidationError):
            slugify(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

class TestCreate:
    def test_create_with_explicit_kb_id(self, registry):
        kb = registry.create(kb_id="eng", name="Engineering", description="Docs")
        assert kb.kb_id == "eng"
        assert kb.name == "Engineering"
        assert kb.description == "Docs"
        assert kb.settings == {}
        assert kb.created_at == kb.updated_at

    def test_create_with_derived_kb_id(self, registry):
        kb = registry.create(name="Customer Support")
        assert kb.kb_id == "customer-support"
        assert kb.name == "Customer Support"

    def test_create_settings_roundtrip(self, registry):
        kb = registry.create(name="X", settings={"a": 1, "b": ["x", "y"]})
        round_tripped = registry.get(kb.kb_id)
        assert round_tripped.settings == {"a": 1, "b": ["x", "y"]}

    def test_create_duplicate_kb_id_conflicts(self, registry):
        registry.create(kb_id="dup", name="First")
        with pytest.raises(KBConflictError):
            registry.create(kb_id="dup", name="Second")

    def test_create_duplicate_via_slug_conflicts(self, registry):
        registry.create(name="Engineering")  # -> engineering
        with pytest.raises(KBConflictError):
            registry.create(name="engineering")

    def test_invalid_explicit_kb_id_rejected(self, registry):
        for bad in ["UPPER", "with space", "-leading", "with!bang", ""]:
            with pytest.raises(KBValidationError):
                registry.create(kb_id=bad, name="x")

    def test_empty_name_rejected(self, registry):
        with pytest.raises(KBValidationError):
            registry.create(name="   ")

    def test_overlong_name_rejected(self, registry):
        with pytest.raises(KBValidationError):
            registry.create(name="x" * 200)

    def test_overlong_description_rejected(self, registry):
        with pytest.raises(KBValidationError):
            registry.create(name="x", description="y" * 2000)

    def test_non_serializable_settings_rejected(self, registry):
        class Unserializable:
            pass

        with pytest.raises(KBValidationError):
            registry.create(name="x", settings={"bad": Unserializable()})  # type: ignore[dict-item]


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

class TestRead:
    def test_list_empty(self, registry):
        assert registry.list() == []

    def test_list_sorted(self, registry):
        registry.create(kb_id="zeta", name="Z")
        registry.create(kb_id="alpha", name="A")
        registry.create(kb_id="mu", name="M")
        ids = [kb.kb_id for kb in registry.list()]
        assert ids == ["alpha", "mu", "zeta"]

    def test_get_missing(self, registry):
        with pytest.raises(KBNotFoundError):
            registry.get("nope")

    def test_exists(self, registry):
        registry.create(kb_id="here", name="H")
        assert registry.exists("here") is True
        assert registry.exists("missing") is False


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

class TestUpdate:
    def test_update_name(self, registry):
        registry.create(kb_id="eng", name="Engineering")
        updated = registry.update("eng", name="Eng Team")
        assert updated.name == "Eng Team"
        assert updated.updated_at >= updated.created_at

    def test_update_description_to_empty(self, registry):
        registry.create(kb_id="eng", name="X", description="initial")
        updated = registry.update("eng", description="")
        assert updated.description == ""

    def test_update_settings_replaces(self, registry):
        registry.create(kb_id="eng", name="X", settings={"a": 1})
        updated = registry.update("eng", settings={"b": 2})
        assert updated.settings == {"b": 2}

    def test_update_no_fields_is_noop_but_refreshes_updated_at(self, registry):
        kb = registry.create(kb_id="eng", name="X")
        updated = registry.update("eng")
        assert updated.name == kb.name
        # updated_at is refreshed even if no field changed; documented behavior.
        assert updated.updated_at >= kb.updated_at

    def test_update_missing_raises_404(self, registry):
        with pytest.raises(KBNotFoundError):
            registry.update("nope", name="x")

    def test_update_invalid_name_rejected(self, registry):
        registry.create(kb_id="eng", name="X")
        with pytest.raises(KBValidationError):
            registry.update("eng", name="")


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

class TestDelete:
    def test_delete_existing(self, registry):
        registry.create(kb_id="tmp", name="T")
        registry.delete("tmp")
        with pytest.raises(KBNotFoundError):
            registry.get("tmp")

    def test_delete_missing_raises_404(self, registry):
        with pytest.raises(KBNotFoundError):
            registry.delete("nope")

    def test_default_kb_cannot_be_deleted(self, registry):
        seed_default_kb(registry=registry)
        with pytest.raises(KBImmutableError):
            registry.delete(DEFAULT_KB_ID)
        # And it's still there.
        assert registry.exists(DEFAULT_KB_ID)


# ---------------------------------------------------------------------------
# Seeding (idempotent)
# ---------------------------------------------------------------------------

class TestSeedDefault:
    def test_seed_creates_when_missing(self, registry):
        assert not registry.exists(DEFAULT_KB_ID)
        kb = seed_default_kb(registry=registry)
        assert kb.kb_id == DEFAULT_KB_ID
        assert kb.name == "default"
        assert registry.exists(DEFAULT_KB_ID)

    def test_seed_is_idempotent(self, registry):
        first = seed_default_kb(registry=registry)
        second = seed_default_kb(registry=registry)
        third = seed_default_kb(registry=registry)
        # Same row each time — created_at preserved.
        assert first.created_at == second.created_at == third.created_at

    def test_seed_after_manual_create_returns_existing(self, registry):
        # User created the default KB manually before startup.
        registry.create(kb_id=DEFAULT_KB_ID, name="default")
        kb = seed_default_kb(registry=registry)
        assert kb.kb_id == DEFAULT_KB_ID


# ---------------------------------------------------------------------------
# Persistence across instances
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_data_survives_new_registry_instance(self, tmp_db):
        r1 = KBRegistry(db=tmp_db)
        r1.create(kb_id="persist", name="P")

        # Simulate process restart: new registry pointing at the same file.
        r2 = KBRegistry(db=tmp_db)
        kb = r2.get("persist")
        assert kb.kb_id == "persist"
        assert kb.name == "P"

    def test_separate_db_files_are_isolated(self, tmp_path):
        db_a = RegistryDB(db_path=str(tmp_path / "a.db"))
        db_b = RegistryDB(db_path=str(tmp_path / "b.db"))
        KBRegistry(db=db_a).create(kb_id="only-in-a", name="A")

        assert KBRegistry(db=db_a).exists("only-in-a")
        assert not KBRegistry(db=db_b).exists("only-in-a")
