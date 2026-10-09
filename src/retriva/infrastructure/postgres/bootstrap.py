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

"""Generic, idempotent PostgreSQL role provisioning.

Provisions non-elevated LOGIN roles through a cluster-administrator
connection at deployment time.  Long-running services NEVER use the
administrator or superuser accounts.  Passwords are passed as
statement parameters (never interpolated into SQL text) and never
appear in logs or output.

Extensions (Retriva Pro) reuse :func:`provision_roles` for their own
runtime identities; the Core bootstrap provisions the migrator and
the Core runtime role only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import psycopg2
from psycopg2 import sql

from retriva.logger import get_logger

_log = get_logger(__name__)

#: Non-elevated posture enforced on every managed role.
_ROLE_FLAGS = "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"

#: Dedicated non-login owner of the monitoring aggregate interface
#: (Spec 036 / ADR-041).  It owns only its dedicated `monitoring`
#: schema and the aggregate function; it can never log in and holds
#: no elevated, write, DDL-on-application-schemas, or RLS-bypass
#: capability.
MONITOR_OWNER_ROLE = "retriva_monitor_owner"

_MONITOR_OWNER_FLAGS = ("NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOREPLICATION NOBYPASSRLS")


@dataclass(frozen=True)
class RoleSpec:
    """One role to provision.

    ``password`` is the resolved plaintext credential (env value or
    secret file), or ``None`` when no credential is configured: an
    existing role is then left unchanged (no rotation); a missing
    role cannot be created without a password and the provisioning
    fails with the documented env-variable names.  The plaintext
    value MUST NOT be logged, rendered, or stored anywhere else.
    """

    role_name: str
    password: Optional[str]
    password_env: Tuple[str, str]


def _password_env_names(role: str) -> Tuple[str, str]:
    """Canonical env-variable names for a platform role credential."""
    return (f"RETRIVA_PG_{role.upper()}_PASSWORD",
            f"RETRIVA_PG_{role.upper()}_PASSWORD_FILE")


def _role_exists(conn, role_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s",
                    (role_name,))
        return cur.fetchone() is not None


def _is_elevated(conn, role_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb "
            "FROM pg_roles WHERE rolname = %s", (role_name,))
        row = cur.fetchone()
        if row is None:
            return False
        return bool(row[0] or row[1] or row[2])


def provision_roles(conn, specs: List[RoleSpec]) -> Dict[str, List[str]]:
    """Create or update LOGIN roles; returns a summary WITHOUT
    credentials.  Idempotent: existing roles keep their identity and
    a configured password rotates the role password; an existing
    role without a configured password is left unchanged; a
    pre-existing role carrying elevated privileges is refused, never
    managed; a missing role without a configured password fails with
    the documented env-variable names.  ``conn`` must be an
    autocommit cluster-admin connection."""
    created: List[str] = []
    updated: List[str] = []
    unchanged: List[str] = []
    for spec in specs:
        exists = _role_exists(conn, spec.role_name)
        if exists and _is_elevated(conn, spec.role_name):
            raise RuntimeError(
                f"role '{spec.role_name}' already exists with "
                "elevated privileges; refusing to manage it")
        if exists:
            if spec.password is None:
                unchanged.append(spec.role_name)
                continue
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("ALTER ROLE {} LOGIN PASSWORD %s "
                            + _ROLE_FLAGS).format(
                                sql.Identifier(spec.role_name)),
                    (spec.password,))
            updated.append(spec.role_name)
        else:
            if spec.password is None:
                raise RuntimeError(
                    f"role '{spec.role_name}' does not exist and no "
                    "password is configured (set "
                    f"{spec.password_env[0]} or {spec.password_env[1]})")
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD %s "
                            + _ROLE_FLAGS).format(
                                sql.Identifier(spec.role_name)),
                    (spec.password,))
            created.append(spec.role_name)
    _log.info("PostgreSQL roles provisioned: created=%s updated=%s "
              "unchanged=%s", created, updated, unchanged)
    return {"created": created, "updated": updated,
            "unchanged": unchanged}


def provision_monitor_owner(conn, migrator_role: str,
                            role_name: str = MONITOR_OWNER_ROLE) -> str:
    """Provision the dedicated non-login owner role of the monitoring
    aggregate interface (Spec 036 / ADR-041).  Idempotent: a missing
    role is created with the exact non-elevated NOLOGIN posture; an
    existing role is accepted only when its posture matches exactly
    (never silently redefined, never managed when divergent); the
    membership that lets the migrator ``SET ROLE`` into it (required
    to create the interface objects it owns) is granted idempotently.
    Runs on an autocommit cluster-admin connection.  Returns
    ``"created"`` or ``"existing"``; never returns or logs
    credentials (this role has none)."""
    exists = _role_exists(conn, role_name)
    if exists:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rolcanlogin, rolsuper, rolcreaterole, "
                "rolcreatedb, rolreplication, rolbypassrls "
                "FROM pg_roles WHERE rolname = %s", (role_name,))
            row = cur.fetchone()
        if tuple(bool(value) for value in row) != (False,) * 6:
            raise RuntimeError(
                f"role '{role_name}' already exists with a divergent "
                "posture (expected NOLOGIN NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION NOBYPASSRLS); refusing to "
                "manage it")
    else:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("CREATE ROLE {} " + _MONITOR_OWNER_FLAGS).format(
                    sql.Identifier(role_name)))
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(role_name), sql.Identifier(migrator_role)))
    _log.info("monitoring owner role provisioned: role=%s state=%s",
              role_name, "existing" if exists else "created")
    return "existing" if exists else "created"


def restrict_database_create(conn, database: str,
                             migrator_role: str) -> None:
    """Only the migrator may create schemas in the application
    database; PUBLIC (and therefore every application role) never
    can."""
    db_ident = sql.Identifier(database)
    with conn.cursor() as cur:
        cur.execute(sql.SQL(
            "REVOKE CREATE ON DATABASE {} FROM PUBLIC").format(db_ident))
        cur.execute(sql.SQL(
            "GRANT CREATE ON DATABASE {} TO {}").format(
                db_ident, sql.Identifier(migrator_role)))
    _log.info("database CREATE privilege restricted to the migrator "
              "role (database=%s)", database)


def _connect_admin(connection_kwargs: Dict):
    """Connect as the cluster administrator (autocommit)."""
    conn = psycopg2.connect(**connection_kwargs)
    conn.autocommit = True
    return conn


def bootstrap_platform(settings) -> Dict:
    """Provision the Core platform roles (migrator, Core runtime) and
    the database-level CREATE restriction.  Idempotent.  Runs once at
    deployment through the Core bootstrap one-shot; never at
    application runtime."""
    if not settings.has_password("admin"):
        pw_env, pw_file_env = _password_env_names("admin")
        raise RuntimeError(
            "platform bootstrap requires the cluster-admin credential "
            f"(set {pw_env} or {pw_file_env})")
    specs = [
        RoleSpec(
            role_name=settings.migrator_user,
            password=(settings.resolved_password("migrator")
                      if settings.has_password("migrator") else None),
            password_env=_password_env_names("migrator")),
        RoleSpec(
            role_name=settings.core_user,
            password=(settings.resolved_password("core")
                      if settings.has_password("core") else None),
            password_env=_password_env_names("core")),
    ]
    conn = _connect_admin(settings.connection_kwargs("admin"))
    try:
        summary = provision_roles(conn, specs)
        summary["monitor_owner"] = provision_monitor_owner(
            conn, settings.migrator_user)
        restrict_database_create(
            conn, settings.database, settings.migrator_user)
    finally:
        conn.close()
    summary["database"] = settings.database
    return summary
