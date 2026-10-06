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

"""Spec 028 P1: core.knowledge migration stream.

Validation: clean apply, stream ordering, idempotent rerun, checksum
drift, ownership/RLS/grants probes, op-state monotonic trigger, and the
database-level downgrade guard.
"""

from __future__ import annotations

import json
import types

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from retriva.infrastructure.postgres.migrations import (  # noqa: E402
    CORE_PLATFORM_STREAM,
    MigrationError,
    ProviderRegistry,
    downgrade as framework_downgrade,
    load_provider_registry,
    platform_provider,
    upgrade as framework_upgrade,
)
from retriva.jobs.migrations import jobs_provider  # noqa: E402
from retriva.knowledge.migrations import (  # noqa: E402
    KNOWLEDGE_STREAM_ID,
    knowledge_provider,
)

KNOWLEDGE_DB = "retriva_pg_test_knowledge"
TENANT = "tenant-a"


def _registry():
    registry = ProviderRegistry()
    registry.register_core_platform(platform_provider())
    registry.register_core_stream(jobs_provider())
    registry.register_core_stream(knowledge_provider())
    return registry


@pytest.fixture(scope="module")
def knowledge_db(pg_platform_stack):
    settings = pg_platform_stack.fresh_database(KNOWLEDGE_DB)
    # A stand-in for the Pro runtime role (provisioned by the CRM
    # repo in production); Core grants it nothing on `knowledge`.
    admin = pg_platform_stack.admin()
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles "
                "WHERE rolname='retriva_application') THEN CREATE ROLE "
                "retriva_application NOLOGIN; END IF; END $$;")
    finally:
        admin.close()
    registry = _registry()
    result = framework_upgrade(registry, settings)
    assert [a["version"]
            for a in result[CORE_PLATFORM_STREAM][0]["applied"]] == [1]
    assert [a["version"] for a in result["core.jobs"][0]["applied"]] == [1]
    assert [a["version"] for a in result[KNOWLEDGE_STREAM_ID][0]["applied"]] \
        == [1]
    return types.SimpleNamespace(settings=settings, registry=registry,
                                 stack=pg_platform_stack)


def _connect(settings, role="migrator"):
    conn = psycopg2.connect(**settings.connection_kwargs(role))
    conn.autocommit = True
    return conn


def _scalar(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchone()[0]


def test_stream_order_platform_jobs_knowledge(knowledge_db):
    ordered = knowledge_db.registry.stream_ids()
    assert ordered == [
        CORE_PLATFORM_STREAM, "core.jobs", KNOWLEDGE_STREAM_ID]


def test_schema_and_tables_owned_by_migrator(knowledge_db):
    conn = _connect(knowledge_db.settings)
    try:
        owner = _scalar(
            conn, "SELECT pg_get_userbyid(nspowner) FROM pg_namespace "
            "WHERE nspname='knowledge'")
        assert owner == "retriva_migrator"
        for table in (
            "knowledge_bases", "sources", "documents",
            "kb_memberships", "document_versions", "ingestions",
            "version_chunks", "qdrant_operations", "authority"):
            relowner = _scalar(
                conn, "SELECT pg_get_userbyid(relowner) FROM pg_class "
                "WHERE oid = %s::regclass",
                (f"knowledge.{table}",))
            assert relowner == "retriva_migrator", table
    finally:
        conn.close()


def test_rls_forced_and_tenant_policies_present(knowledge_db):
    conn = _connect(knowledge_db.settings)
    try:
        forced = _scalar(
            conn, "SELECT count(*) FROM pg_class WHERE relnamespace = "
            "'knowledge'::regnamespace AND relforcerowsecurity AND "
            "relkind = 'r'")
        assert forced >= 8
        policies = _scalar(
            conn, "SELECT count(*) FROM pg_policy p JOIN pg_class c ON "
            "c.oid = p.polrelid WHERE c.relnamespace = "
            "'knowledge'::regnamespace AND p.polname='tenant_isolation'")
        assert policies >= 8
    finally:
        conn.close()


def test_runtime_grants_and_no_authority_write(knowledge_db):
    conn = _connect(knowledge_db.settings)
    try:
        for table in (
            "knowledge_bases", "sources", "documents",
            "kb_memberships", "document_versions", "ingestions",
            "version_chunks", "qdrant_operations"):
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert _scalar(
                    conn, "SELECT has_table_privilege('retriva_core', "
                    "%s, %s)", (f"knowledge.{table}", priv)), (table, priv)
        for table in ("knowledge_bases", "sources", "documents",
                      "document_versions", "ingestions"):
            assert _scalar(
                conn, "SELECT has_table_privilege('retriva_application', "
                "%s, 'SELECT')", (f"knowledge.{table}",)) is False
        assert _scalar(
            conn, "SELECT has_table_privilege('retriva_core', "
            "'knowledge.authority', 'UPDATE')") is False
        assert _scalar(
            conn, "SELECT has_table_privilege('retriva_core', "
            "'knowledge.authority', 'SELECT')") is True
        assert _scalar(
            conn, "SELECT has_schema_privilege('retriva_core', "
            "'knowledge', 'CREATE')") is False
    finally:
        conn.close()


def test_tenant_id_not_null_everywhere(knowledge_db):
    conn = _connect(knowledge_db.settings)
    try:
        missing = _scalar(
            conn, "SELECT count(*) FROM information_schema.columns WHERE "
            "table_schema='knowledge' AND column_name='tenant_id' AND "
            "is_nullable='YES'")
        assert missing == 0
        tables = _scalar(
            conn, "SELECT count(*) FROM information_schema.tables WHERE "
            "table_schema='knowledge' AND table_name <> 'authority'")
        have = _scalar(
            conn, "SELECT count(DISTINCT table_name) FROM "
            "information_schema.columns WHERE table_schema='knowledge' "
            "AND column_name='tenant_id'")
        assert have == tables
    finally:
        conn.close()


def test_authority_seeded_single_row_default_state(knowledge_db):
    conn = _connect(knowledge_db.settings)
    try:
        rows = _scalar(conn, "SELECT count(*) FROM knowledge.authority")
        assert rows == 1
        state = _scalar(conn, "SELECT state FROM knowledge.authority")
        assert state == "schema_ready"
    finally:
        conn.close()


def test_op_state_monotonic_trigger_blocks_regression(knowledge_db):
    conn = _connect(knowledge_db.settings, "migrator")
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.knowledge_privileged','granted',"
                "false)")
            cur.execute(
                "INSERT INTO knowledge.qdrant_operations (op_id, "
                "tenant_id, op_type, collection_name, op_state) VALUES "
                "('op-regress', %s, 'upsert_batch', 'c', 'verified')",
                (TENANT,))
            with pytest.raises(psycopg2.Error):
                cur.execute(
                    "UPDATE knowledge.qdrant_operations SET "
                    "op_state='prepared' WHERE op_id='op-regress'")
    finally:
        conn.rollback()
        conn.close()


def test_authority_invalid_combination_refused(knowledge_db):
    conn = _connect(knowledge_db.settings, "migrator")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.knowledge_privileged','granted',"
                "false)")
            with pytest.raises(psycopg2.Error):
                cur.execute(
                    "UPDATE knowledge.authority SET "
                    "authoritative=TRUE WHERE singleton=TRUE")
    finally:
        conn.rollback()
        conn.close()


def test_idempotent_rerun_is_noop(knowledge_db):
    registry = _registry()
    result = framework_upgrade(registry, knowledge_db.settings)
    for stream in registry.stream_ids():
        assert result[stream][0]["applied"] == []


def test_checksum_drift_detected(knowledge_db):
    from retriva.infrastructure.postgres.migrations import Migration

    drift = Migration(
        provider="retriva-core", stream=KNOWLEDGE_STREAM_ID, version=1,
        name="knowledge_foundation", up_sql="SELECT 1", down_sql="",
        checksum="deadbeef")
    registry = _registry()
    original = registry.migrations_for

    def patched(stream_id):
        if stream_id == KNOWLEDGE_STREAM_ID:
            return [drift]
        return original(stream_id)

    registry.migrations_for = patched  # type: ignore[assignment]
    with pytest.raises(MigrationError, match="checksum drift"):
        framework_upgrade(registry, knowledge_db.settings)


def test_downgrade_guard_refuses_with_native_rows(knowledge_db):
    settings = knowledge_db.settings
    writer = _connect(settings, "migrator")
    try:
        with writer.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.knowledge_privileged','granted',"
                "false)")
            cur.execute(
                "INSERT INTO knowledge.sources (source_id, tenant_id, "
                "source_type, namespace, normalized_ref, provenance) "
                "VALUES ('s-guard', %s, 'upload', 'upload', "
                "'guard:one', 'native') ON CONFLICT DO NOTHING",
                (TENANT,))
        with pytest.raises(Exception, match="downgrade refused"):
            framework_downgrade(
                _registry(), settings, KNOWLEDGE_STREAM_ID, to=0,
                confirm_destructive=True)
    finally:
        writer.close()


def test_empty_downgrade_succeeds_on_separate_database(pg_platform_stack):
    settings = pg_platform_stack.fresh_database(
        "retriva_pg_test_knowledge_down")
    registry = _registry()
    framework_upgrade(registry, settings)
    reverted = framework_downgrade(
        _registry(), settings, KNOWLEDGE_STREAM_ID, to=0,
        confirm_destructive=True)
    assert [r["version"] for r in reverted] == [1]
    conn = _connect(settings)
    try:
        assert _scalar(
            conn, "SELECT to_regclass('knowledge.documents') IS NULL")
    finally:
        conn.close()
