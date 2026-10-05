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

# Durable jobs (Spec 025 §3.12): the fixed-resolver tenant is mandatory
# and validated at API startup; every API test session gets a valid
# default so the lifespan check passes hermetically.
os.environ.setdefault("RETRIVA_JOBS_DEFAULT_TENANT", "test-tenant")

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


# ---------------------------------------------------------------------------
# Shared PostgreSQL platform fixtures (retriva.infrastructure.postgres).
#
# Deterministic tests run against a REAL PostgreSQL server:
#
#   1. RETRIVA_PG_TEST_ADMIN_URL
#      (postgresql://user[:pass]@host:port/db) uses an existing server
#      and creates a scratch database per session;
#   2. otherwise a scratch cluster is booted from the local PostgreSQL
#      binaries (initdb/pg_ctl) when available;
#   3. otherwise PostgreSQL tests SKIP with an explicit reason.
#
# No credentials from these fixtures ever reach logs or assertions.
# The cluster is throwaway (loopback trust, tmpfs-backed tmp dir);
# it never touches any deployment volume.
# ---------------------------------------------------------------------------

import glob as _glob
import os as _os
import shutil as _shutil
import socket as _socket
import subprocess as _subprocess
import tempfile as _tempfile
from urllib.parse import urlparse as _urlparse

import pytest


def _pg_bin(name: str):
    """Locate a PostgreSQL server binary (PATH, then versioned dirs)."""
    found = _shutil.which(name)
    if found:
        return found
    patterns = (
        "/usr/lib/postgresql/*/bin",
        "/usr/local/pgsql/bin",
        "/usr/pgsql-*/bin",
    )
    for pattern in patterns:
        matches = sorted(_glob.glob(f"{pattern}/{name}"))
        if matches:
            return matches[-1]
    return None


def _free_port() -> int:
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _ScratchCluster:
    """A local throwaway PostgreSQL cluster (trust auth, loopback only)."""

    def __init__(self, base_dir: str, admin_user: str):
        self._initdb = _pg_bin("initdb")
        self._pg_ctl = _pg_bin("pg_ctl")
        self._dir = _os.path.join(base_dir, "pgdata")
        self._sock_dir = _os.path.join(base_dir, "pgsock")
        self._log = _os.path.join(base_dir, "postgres.log")
        self.admin_user = admin_user
        self.port = _free_port()
        _os.makedirs(self._sock_dir, exist_ok=True)

    def start(self):
        _subprocess.run(
            [self._initdb, "-D", self._dir, "-U", self.admin_user,
             "-A", "trust", "-E", "UTF8", "--no-locale"],
            check=True, capture_output=True, text=True)
        _subprocess.run(
            [self._pg_ctl, "-D", self._dir, "-l", self._log,
             "-o", f"-F -p {self.port} -h 127.0.0.1 -k {self._sock_dir}",
             "-w", "-t", "120", "start"],
            check=True, capture_output=True, text=True)

    def stop(self):
        try:
            _subprocess.run(
                [self._pg_ctl, "-D", self._dir, "-m", "immediate",
                 "stop"],
                capture_output=True, text=True, timeout=30)
        except Exception:
            pass

    def admin_connect(self, dbname: str = "postgres"):
        import psycopg2
        return psycopg2.connect(
            host="127.0.0.1", port=self.port, dbname=dbname,
            user=self.admin_user, connect_timeout=10)


def _platform_settings(host, port, database, admin_user,
                        admin_password):
    from pydantic import SecretStr
    from retriva.infrastructure.postgres.config import (
        PostgresPlatformSettings,
    )
    return PostgresPlatformSettings(
        host=host, port=port, database=database,
        admin_user=admin_user,
        admin_password=SecretStr(admin_password),
        migrator_password=SecretStr("migrator-test-pw"),
        core_password=SecretStr("core-test-pw"),
    )


@pytest.fixture(scope="session")
def pg_platform_stack(tmp_path_factory):
    """Session-scoped shared-PostgreSQL platform stack: scratch
    database + Core role bootstrap (no migrations applied yet).

    Yields an object with:
      - settings: platform settings bound to the scratch database;
      - admin(): fresh admin connection;
      - fresh_database(name): an empty separate scratch database.
    """
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        pytest.skip("psycopg2 not installed; platform tests skipped")

    url = _os.environ.get("RETRIVA_PG_TEST_ADMIN_URL", "")
    cluster = None
    cleanup_dir = None
    drop_db = None

    if url:
        base = _tempfile.mkdtemp(prefix="retriva-pg-platform-test-",
                                 dir="/tmp")
        scratch_db = (
            f"retriva_pg_platform_{_os.getpid():d}_{_free_port()}")
        admin = psycopg2.connect(url)
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{scratch_db}"')
        host = _urlparse(url).hostname or "127.0.0.1"
        port = _urlparse(url).port or 5432
        password = _urlparse(url).password or "unused"

        def _drop():
            with admin.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity WHERE datname = %s",
                    (scratch_db,))
                cur.execute(f'DROP DATABASE IF EXISTS "{scratch_db}"')
            admin.close()

        drop_db = _drop
        settings = _platform_settings(
            host, port, scratch_db,
            _urlparse(url).username or "postgres", password)
    elif _pg_bin("initdb") and _pg_bin("pg_ctl"):
        base = _tempfile.mkdtemp(prefix="retriva-pg-platform-test-",
                                 dir="/tmp")
        cleanup_dir = base
        cluster = _ScratchCluster(base, "retriva_admin")
        cluster.start()
        admin_conn = cluster.admin_connect()
        admin_conn.autocommit = True
        with admin_conn.cursor() as cur:
            cur.execute('CREATE DATABASE "retriva"')
        admin_conn.close()
        settings = _platform_settings(
            "127.0.0.1", cluster.port, "retriva",
            "retriva_admin", "unused-trust")
        drop_db = None
    else:
        pytest.skip(
            "no PostgreSQL server binaries (initdb/pg_ctl) and no "
            "RETRIVA_PG_TEST_ADMIN_URL; platform tests skipped")

    from retriva.infrastructure.postgres import bootstrap as pg_bs

    class _Stack:
        def __init__(self, settings):
            self.settings = settings

        def admin(self, dbname=None):
            if cluster is not None:
                return cluster.admin_connect(
                    dbname or self.settings.database)
            import psycopg2 as _p2
            return _p2.connect(
                host=self.settings.host, port=self.settings.port,
                dbname=dbname or self.settings.database,
                user=self.settings.admin_user,
                password=self.settings.resolved_password("admin"),
                connect_timeout=10)

        def fresh_database(self, name: str):
            """Create an empty scratch database and apply the same
            per-database bootstrap the deployment performs: the
            migrator (and only the migrator) may create schemas."""
            from retriva.infrastructure.postgres.bootstrap import (
                restrict_database_create,
            )
            conn = self.admin()
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
                    cur.execute(f'CREATE DATABASE "{name}"')
            finally:
                conn.close()
            db_conn = self.admin(name)
            db_conn.autocommit = True
            try:
                restrict_database_create(
                    db_conn, name, self.settings.migrator_user)
            finally:
                db_conn.close()
            return _platform_settings(
                self.settings.host, self.settings.port, name,
                self.settings.admin_user,
                self.settings.resolved_password("admin"))

    try:
        pg_bs.bootstrap_platform(settings)
        yield _Stack(settings)
    finally:
        if drop_db is not None:
            drop_db()
        if cluster is not None:
            cluster.stop()
        if cleanup_dir is not None:
            _shutil.rmtree(cleanup_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Durable-jobs integration (Spec 025): a scratch database with
# core.platform + core.jobs applied, and the process-wide durable
# service bound to it with the production LOCAL executor (no Celery
# in tests).  Modules that exercise v2 submission endpoints request
# these fixtures explicitly; nothing is forced on unrelated tests.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def durable_jobs_database(pg_platform_stack):
    """Scratch database with the core.platform and core.jobs streams
    applied (the same one-shot the deployment performs)."""
    from retriva.infrastructure.postgres.migrations import (
        CORE_PLATFORM_STREAM,
        load_provider_registry,
        upgrade as framework_upgrade,
    )

    settings = pg_platform_stack.fresh_database(
        "retriva_pg_test_jobs_shared")
    registry = load_provider_registry("")
    from retriva.jobs.migrations import jobs_provider
    registry.register_core_stream(jobs_provider())
    result = framework_upgrade(registry, settings)
    assert [a["version"] for a in
            result[CORE_PLATFORM_STREAM][0]["applied"]] == [1]
    assert [a["version"] for a in
            result["core.jobs"][0]["applied"]] == [1]
    return settings


@pytest.fixture()
def durable_service(durable_jobs_database):
    """Bind the durable jobs service singleton to the scratch
    database for the current test (production local-transport
    wiring: LocalExecutor + LocalPublisher, no Celery)."""
    from retriva.ingestion_api import durable_jobs as dj

    dj.reset_jobs_service()
    try:
        from retriva.jobs.repository import PostgresJobsRepository

        service = dj.build_service(
            repo=PostgresJobsRepository(durable_jobs_database))
        dj._service = service
        yield service
    finally:
        dj.reset_jobs_service()
