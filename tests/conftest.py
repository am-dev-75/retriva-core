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

import pytest
import os
from pathlib import Path

@pytest.fixture
def mock_mirror_dir(tmp_path):
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    
    domain_dir = mirror / "wiki.dave.eu"
    domain_dir.mkdir()
    
    (domain_dir / "index.html").write_text("<html><head><title>Home</title></head><body><main>Home Page</main></body></html>")
    (domain_dir / "about.html").write_text("<html><head><title>About</title></head><body><div id='content'>About Page</div></body></html>")
    
    return mirror

# ---------------------------------------------------------------------------
# Isolate the KB registry DB from the dev/production registry.db file.
#
# Phase 1 introduced `seed_default_kb()` in the FastAPI lifespan. Without
# isolation, running the test suite would write to
# `<storage_path>/registry.db` in the developer's checkout. We redirect the
# module-level singleton to a session-scoped temp file so tests are
# hermetic by default. Individual tests that need their own registry still
# create a `RegistryDB(db_path=...)` explicitly (see test_kb_registry.py).
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True, scope="session")
def _isolated_kb_registry_db(tmp_path_factory):
    from retriva.infrastructure import registry_db as _registry_db_mod

    tmp_dir = tmp_path_factory.mktemp("registry_db")
    db_path = tmp_dir / "registry.db"
    isolated = _registry_db_mod.RegistryDB(db_path=str(db_path))

    saved = _registry_db_mod._default_db
    _registry_db_mod._default_db = isolated
    try:
        yield isolated
    finally:
        _registry_db_mod._default_db = saved


# ---------------------------------------------------------------------------
# Ensure the 'default' KB exists for every test.
#
# Many existing tests use `TestClient(app)` at module scope without entering
# its context, which means the FastAPI lifespan (and therefore
# `seed_default_kb()`) does not run. Those tests then hit Phase 2's KB
# enforcement on retrieval/search endpoints and receive 404 for the default
# kb_id they did not create.
#
# We seed defensively here so test files do not need to change. Tests that
# want a clean registry create their own RegistryDB and KBRegistry locally
# (see test_kb_registry.py).
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _ensure_default_kb(_isolated_kb_registry_db):
    from retriva.domain.kb import seed_default_kb, KBRegistry
    seed_default_kb(registry=KBRegistry(db=_isolated_kb_registry_db))
    yield
