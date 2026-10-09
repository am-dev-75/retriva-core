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

"""Deterministic security and fidelity tests for the Spec 036 /
ADR-041 RLS-safe monitoring aggregate interface
(``monitoring.nonterminal_job_count()``) against a real PostgreSQL 16
scratch cluster.

Covers cross-tenant correctness, forced-RLS preservation, direct-access
denial, PUBLIC/wrong-role denial, shadowing resistance, function
hardening, statement timeout, and the migration lifecycle
(upgrade/fresh install/idempotent re-run/downgrade/re-apply) with no
data rewrite and no application-grant drift.
"""

from __future__ import annotations

import types

import psycopg2
import psycopg2.errors
import pytest

psycopg2 = pytest.importorskip("psycopg2")

from retriva.infrastructure.postgres.migrations import (
    load_provider_registry,
    downgrade as framework_downgrade,
    upgrade as framework_upgrade,
)

from retriva.jobs.migrations import jobs_provider

MONITOR_DB = "retriva_pg_test_monitor"
ROLLBACK_DB = "retriva_pg_test_monitor_rollback"
MONITOR_ROLE = "retriva_monitor"
MONITOR_OWNER = "retriva_monitor_owner"
#: Synthetic scratch-cluster credential (never a production secret).
MONITOR_PASSWORD = "monitor-test-pw"
FUNCTION = "monitoring.nonterminal_job_count()"
NON_TERMINAL = ("queued", "running", "retry_wait", "dispatch_unknown")
TERMINAL = ("succeeded", "failed", "cancelled")


def _admin(settings, stack):
    return stack.admin(settings.database)


def _scalar(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchone()[0]


def _rows(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


def _monitor_connect(settings, *, timeout_ms=5000,
                     search_path=None):
    kwargs = dict(settings.connection_kwargs("migrator"))
    kwargs.update(
        user=MONITOR_ROLE,
        password=MONITOR_PASSWORD,
        application_name="retriva_platform_monitor_test",
        options=(f"-c statement_timeout={timeout_ms}"),
    )
    conn = psycopg2.connect(**kwargs)
    conn.autocommit = True
    if search_path is not None:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path = {search_path}")
    return conn


def _ensure_monitor_role(admin) -> None:
    """Create the deployment-owned monitoring login role (scratch
    cluster only) with the exact least-privilege posture."""
    if _scalar(admin,
               "SELECT count(*) FROM pg_roles WHERE rolname = %s",
               (MONITOR_ROLE,)) == 0:
        with admin.cursor() as cur:
            cur.execute(
                "CREATE ROLE " + MONITOR_ROLE + " LOGIN PASSWORD %s "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                "NOBYPASSRLS", (MONITOR_PASSWORD,))


def _grant_template(admin, database: str) -> None:
    """The accepted deployment grant template: CONNECT + USAGE on the
    monitoring schema + EXECUTE only."""
    with admin.cursor() as cur:
        cur.execute(
            f'GRANT CONNECT ON DATABASE "{database}" TO {MONITOR_ROLE}')
        cur.execute(
            f"GRANT USAGE ON SCHEMA monitoring TO {MONITOR_ROLE}")
        cur.execute(
            f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {MONITOR_ROLE}")


def _seed_jobs(admin) -> None:
    """Multiple synthetic tenants and states.

    Ground truth: 4 non-terminal (2 x tenant-a, 1 x tenant-b,
    1 x tenant-c) and 3 terminal rows.
    """
    seeds = [
        ("seed-a1", "tenant-a", "queued"),
        ("seed-a2", "tenant-a", "running"),
        ("seed-b1", "tenant-b", "retry_wait"),
        ("seed-c1", "tenant-c", "dispatch_unknown"),
        ("seed-a3", "tenant-a", "succeeded"),
        ("seed-b2", "tenant-b", "failed"),
        ("seed-c2", "tenant-c", "cancelled"),
    ]
    with admin.cursor() as cur:
        for job_id, tenant, status in seeds:
            cur.execute(
                "INSERT INTO jobs.jobs (id, tenant_id, job_type, "
                "payload_version, status, execution_transport) "
                "VALUES (%s, %s, 'monitor.test', 'v1', %s, 'local')",
                (job_id, tenant, status))


def _upgrade_monitor_db(stack, name: str):
    settings = stack.fresh_database(name)
    registry = load_provider_registry("")
    registry.register_core_stream(jobs_provider())
    result = framework_upgrade(registry, settings)
    assert [a["version"]
            for a in result["core.jobs"][0]["applied"]] == [1, 2]
    return settings, registry


@pytest.fixture(scope="module")
def monitor_db(pg_platform_stack):
    settings, registry = _upgrade_monitor_db(pg_platform_stack, MONITOR_DB)
    admin = _admin(settings, pg_platform_stack)
    try:
        admin.autocommit = True
        _ensure_monitor_role(admin)
        _grant_template(admin, settings.database)
        _seed_jobs(admin)
    finally:
        admin.close()
    return types.SimpleNamespace(settings=settings, stack=pg_platform_stack,
                                 registry=registry)


# --- catalogue shape and hardening -----------------------------------------


def test_schema_and_function_owned_by_dedicated_nonlogin_owner(monitor_db):
    admin = _admin(monitor_db.settings, monitor_db.stack)
    try:
        assert _scalar(
            admin,
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace "
            "WHERE nspname = 'monitoring'") == MONITOR_OWNER
        owner, prosecdef, prorettype, nargs, volatility = _rows(
            admin,
            "SELECT pg_get_userbyid(p.proowner), p.prosecdef, "
            "p.prorettype::regtype::text, p.pronargs, p.provolatile "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = "
            "p.pronamespace WHERE n.nspname = 'monitoring' AND "
            "p.proname = 'nonterminal_job_count'")[0]
        assert owner == MONITOR_OWNER
        assert prosecdef is True
        assert prorettype == "bigint"
        assert nargs == 0
        assert volatility == "v"
        canlogin, superuser, createrole, createdb, replication, bypassrls = (
            _rows(
                admin,
                "SELECT rolcanlogin, rolsuper, rolcreaterole, "
                "rolcreatedb, rolreplication, rolbypassrls FROM "
                "pg_roles WHERE rolname = %s", (MONITOR_OWNER,))[0])
        assert not any((canlogin, superuser, createrole, createdb,
                        replication, bypassrls))
    finally:
        admin.close()


def test_function_definition_is_fixed_and_qualified(monitor_db):
    admin = _admin(monitor_db.settings, monitor_db.stack)
    try:
        proconfig = _scalar(
            admin,
            "SELECT array_to_string(p.proconfig, ',') FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE "
            "n.nspname = 'monitoring' AND p.proname = "
            "'nonterminal_job_count'")
        assert proconfig == "search_path=pg_catalog"
        definition = _scalar(
            admin,
            "SELECT pg_get_functiondef(p.oid) FROM pg_proc p JOIN "
            "pg_namespace n ON n.oid = p.pronamespace WHERE "
            "n.nspname = 'monitoring' AND p.proname = "
            "'nonterminal_job_count'")
        assert "jobs.jobs" in definition
        assert "pg_catalog.count" in definition
        assert "pg_catalog.set_config" in definition
        assert "EXECUTE " not in definition
        assert "format(" not in definition.lower()
        assert "current_setting" not in definition.lower()
    finally:
        admin.close()


def test_public_revoked_and_acl_limited_to_owner_and_monitor(monitor_db):
    admin = _admin(monitor_db.settings, monitor_db.stack)
    try:
        acl = _scalar(
            admin,
            "SELECT array_to_string(p.proacl, '|') FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE "
            "n.nspname = 'monitoring' AND p.proname = "
            "'nonterminal_job_count'")
        entries = dict(item.split("=", 1) for item in acl.split("|"))
        assert set(entries) == {MONITOR_OWNER, MONITOR_ROLE}
        assert all("X" in value.split("/")[0]
                   for value in entries.values())
        # PUBLIC has no EXECUTE marker (`=X/...`).
        assert not any(item.startswith("=") for item in acl.split("|"))
        # forbidden privileges must never appear in the ACL
        assert "BYPASSRLS" not in acl and "USAGE" not in acl
    finally:
        admin.close()


# --- cross-tenant fidelity --------------------------------------------------


def test_cross_tenant_aggregate_equals_ground_truth(monitor_db):
    admin = _admin(monitor_db.settings, monitor_db.stack)
    monitor = _monitor_connect(monitor_db.settings)
    try:
        ground_truth = _scalar(
            admin,
            "SELECT count(*) FROM jobs.jobs WHERE status NOT IN "
            "('succeeded', 'failed', 'cancelled')")
        assert ground_truth == 4
        assert _scalar(
            monitor, f"SELECT {FUNCTION}") == ground_truth
        # per-tenant visibility is irrelevant: the aggregate is global
        assert _scalar(
            admin,
            "SELECT count(*) FROM jobs.jobs WHERE tenant_id = "
            "'tenant-a' AND status NOT IN ('succeeded', 'failed', "
            "'cancelled')") == 2
    finally:
        monitor.close()
        admin.close()


def test_terminal_states_are_excluded(monitor_db):
    admin = _admin(monitor_db.settings, monitor_db.stack)
    monitor = _monitor_connect(monitor_db.settings)
    try:
        before = _scalar(monitor, f"SELECT {FUNCTION}")
        with admin.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs.jobs (id, tenant_id, job_type, "
                "payload_version, status, execution_transport) VALUES "
                "('seed-term', 'tenant-b', 'monitor.test', 'v1', "
                "'failed', 'local'), "
                "('seed-term2', 'tenant-c', 'monitor.test', 'v1', "
                "'succeeded', 'local')")
        try:
            assert _scalar(monitor, f"SELECT {FUNCTION}") == before
        finally:
            with admin.cursor() as cur:
                cur.execute(
                    "DELETE FROM jobs.jobs WHERE id IN "
                    "('seed-term', 'seed-term2')")
    finally:
        monitor.close()
        admin.close()


# --- direct access and privilege denial -------------------------------------


def test_direct_table_access_is_denied_for_monitoring(monitor_db):
    monitor = _monitor_connect(monitor_db.settings)
    try:
        probes = (
            "SELECT count(*) FROM jobs.jobs",
            "SELECT * FROM jobs.jobs LIMIT 1",
            "INSERT INTO jobs.jobs (id, tenant_id, job_type, "
            "payload_version, status, execution_transport) VALUES "
            "('x', 't', 'x', 'v1', 'queued', 'local')",
            "UPDATE jobs.jobs SET status = 'failed'",
            "DELETE FROM jobs.jobs",
            "TRUNCATE jobs.jobs",
            "CREATE TABLE jobs.denied_probe (x int)",
            "ALTER TABLE jobs.jobs RENAME TO jobs_denied",
            "DROP TABLE jobs.jobs",
            "GRANT SELECT ON jobs.jobs TO retriva_core",
            "SET ROLE retriva_migrator",
            "SET ROLE " + MONITOR_OWNER,
        )
        for sql in probes:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                with monitor.cursor() as cur:
                    cur.execute(sql)
    finally:
        monitor.close()


def test_monitoring_has_no_elevation_or_membership(monitor_db):
    admin = _admin(monitor_db.settings, monitor_db.stack)
    try:
        assert _rows(
            admin,
            "SELECT rolsuper, rolcreaterole, rolcreatedb, "
            "rolreplication, rolbypassrls, rolcanlogin FROM pg_roles "
            "WHERE rolname = %s", (MONITOR_ROLE,))[0] == (
                False, False, False, False, False, True)
        memberships = _scalar(
            admin,
            "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r "
            "ON r.oid = m.member WHERE r.rolname = %s",
            (MONITOR_ROLE,))
        assert memberships == 0
    finally:
        admin.close()


def test_application_role_cannot_execute(monitor_db):
    """The application role has no path to the interface.  The
    migrator can reach it only indirectly, through the documented,
    by-design membership in the non-login owner that migrations
    require for ``SET ROLE`` (asserted separately); it is the
    deployment administrator identity, never the monitoring role."""
    settings = monitor_db.settings
    conn = psycopg2.connect(**settings.connection_kwargs("core"))
    conn.autocommit = True
    try:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute(f"SELECT {FUNCTION}")
    finally:
        conn.close()


def test_migrator_membership_is_the_only_indirect_path(monitor_db):
    admin = _admin(monitor_db.settings, monitor_db.stack)
    try:
        members = _rows(
            admin,
            "SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r "
            "ON r.oid = m.member JOIN pg_roles g ON g.oid = "
            "m.roleid WHERE g.rolname = %s", (MONITOR_OWNER,))
        assert [row[0] for row in members] == ["retriva_migrator"]
    finally:
        admin.close()


# --- RLS preservation and shadowing resistance ------------------------------


def test_force_rls_still_enforced_on_protected_tables(monitor_db):
    admin = _admin(monitor_db.settings, monitor_db.stack)
    try:
        for table in ("jobs", "job_attempts", "job_events"):
            enabled, forced = _rows(
                admin,
                "SELECT relrowsecurity, relforcerowsecurity FROM "
                "pg_class c JOIN pg_namespace n ON n.oid = "
                "c.relnamespace WHERE n.nspname = 'jobs' AND "
                "c.relname = %s", (table,))[0]
            assert enabled and forced, table
        # Behavioural: the application role without a tenant context
        # still sees nothing; the function's transaction flag does not
        # leak across sessions.
        core = psycopg2.connect(**monitor_db.settings.connection_kwargs(
            "core"))
        core.autocommit = True
        try:
            assert _scalar(core, "SELECT count(*) FROM jobs.jobs") == 0
            with core.cursor() as cur:
                cur.execute("SET app.current_tenant = 'tenant-a'")
                cur.execute("SELECT count(*) FROM jobs.jobs")
                assert cur.fetchone()[0] == 3
        finally:
            core.close()
    finally:
        admin.close()


def test_shadowing_and_caller_search_path_cannot_alter_the_result(
        monitor_db):
    admin = _admin(monitor_db.settings, monitor_db.stack)
    monitor = _monitor_connect(
        monitor_db.settings, search_path="public")
    try:
        expected = _scalar(monitor, f"SELECT {FUNCTION}")
        with admin.cursor() as cur:
            cur.execute("CREATE TABLE public.jobs (status text)")
            cur.execute(
                "INSERT INTO public.jobs VALUES ('queued'), ('queued'), "
                "('queued'), ('queued'), ('queued'), ('queued'), "
                "('queued'), ('queued'), ('queued')")
            cur.execute("CREATE SCHEMA decoy_shadow")
            cur.execute("CREATE TABLE decoy_shadow.jobs "
                        "(status text)")
            cur.execute("INSERT INTO decoy_shadow.jobs VALUES "
                        "('queued'), ('queued')")
        try:
            with monitor.cursor() as cur:
                cur.execute("SET search_path = decoy_shadow, public")
            assert _scalar(monitor, f"SELECT {FUNCTION}") == expected
            monitor.close()
            monitor = _monitor_connect(
                monitor_db.settings, search_path="pg_temp")
            assert _scalar(monitor, f"SELECT {FUNCTION}") == expected
        finally:
            with admin.cursor() as cur:
                cur.execute("DROP TABLE decoy_shadow.jobs")
                cur.execute("DROP SCHEMA decoy_shadow")
                cur.execute("DROP TABLE public.jobs")
    finally:
        monitor.close()
        admin.close()


# --- session boundary -------------------------------------------------------


def test_statement_timeout_boundary_and_bounded_output(monitor_db):
    monitor = _monitor_connect(monitor_db.settings, timeout_ms=5000)
    try:
        assert _scalar(monitor, "SHOW statement_timeout") == "5s"
        result = _rows(monitor, f"SELECT {FUNCTION}")
        assert len(result) == 1 and len(result[0]) == 1
        assert isinstance(result[0][0], int)
    finally:
        monitor.close()


# --- migration lifecycle ----------------------------------------------------


def test_provider_requires_monitor_owner_role():
    assert MONITOR_OWNER in jobs_provider().required_roles()


def test_migration_lifecycle_downgrade_and_reapply(pg_platform_stack):
    settings, registry = _upgrade_monitor_db(
        pg_platform_stack, ROLLBACK_DB)
    admin = _admin(settings, pg_platform_stack)
    admin.autocommit = True
    try:
        _ensure_monitor_role(admin)
        _grant_template(admin, settings.database)
        _seed_jobs(admin)
        before_rows = _scalar(admin, "SELECT count(*) FROM jobs.jobs")
        before_policies = _scalar(
            admin,
            "SELECT count(*) FROM pg_policy p JOIN pg_class c ON "
            "c.oid = p.polrelid JOIN pg_namespace n ON n.oid = "
            "c.relnamespace WHERE n.nspname = 'jobs'")
        before_core_select = _scalar(
            admin,
            "SELECT has_table_privilege('retriva_core', "
            "'jobs.jobs', 'SELECT')")
        monitor = _monitor_connect(settings)
        try:
            assert _scalar(monitor, f"SELECT {FUNCTION}") == 4
        finally:
            monitor.close()

        # idempotent re-run
        result = framework_upgrade(registry, settings)
        assert result["core.jobs"][0]["applied"] == []

        # rollback removes only the interface and its grants
        rolled = framework_downgrade(
            registry, settings, "core.jobs", to=1,
            confirm_destructive=True)
        assert rolled and rolled[0]["version"] == 2
        assert _scalar(
            admin,
            "SELECT count(*) FROM pg_namespace WHERE nspname = "
            "'monitoring'") == 0
        assert _scalar(
            admin,
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON "
            "n.oid = p.pronamespace WHERE n.nspname = 'monitoring' "
            "AND p.proname = 'nonterminal_job_count'") == 0
        assert not _scalar(
            admin,
            "SELECT has_schema_privilege(%s, 'jobs', 'USAGE')",
            (MONITOR_OWNER,))
        assert not _scalar(
            admin,
            "SELECT has_table_privilege(%s, 'jobs.jobs', 'SELECT')",
            (MONITOR_OWNER,))
        # no data rewrite, no application-grant or policy drift
        assert _scalar(admin, "SELECT count(*) FROM jobs.jobs") == (
            before_rows)
        assert _scalar(
            admin,
            "SELECT count(*) FROM pg_policy p JOIN pg_class c ON "
            "c.oid = p.polrelid JOIN pg_namespace n ON n.oid = "
            "c.relnamespace WHERE n.nspname = 'jobs'") == (
                before_policies)
        assert _scalar(
            admin,
            "SELECT has_table_privilege('retriva_core', "
            "'jobs.jobs', 'SELECT')") == before_core_select
        assert _scalar(
            admin,
            "SELECT has_table_privilege('retriva_core', "
            "'jobs.jobs', 'INSERT')")

        # re-apply (the deployment grant template is re-applied too:
        # the EXECUTE grant vanished with the dropped function)
        result = framework_upgrade(registry, settings)
        assert [a["version"]
                for a in result["core.jobs"][0]["applied"]] == [2]
        _grant_template(admin, settings.database)
        monitor = _monitor_connect(settings)
        try:
            assert _scalar(monitor, f"SELECT {FUNCTION}") == 4
        finally:
            monitor.close()
    finally:
        admin.close()
