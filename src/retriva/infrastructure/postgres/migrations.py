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

"""Migration-provider contract, registry, ledger, and runner.

Design (Spec 024; ADR-029):

- providers declare identity (``provider_id``), a migration stream
  (``stream_id``), stream dependencies, the roles their SQL expects,
  optional unversioned idempotent bootstrap SQL, an optional legacy
  ledger to adopt, and versioned migrations;
- migrations are plain SQL bodies with sha256 checksums
  (``sha256(up_sql + NUL + down_sql)`` — the same algorithm the
  original CRM runner used, so adopted rows keep their identity);
- the Core-owned ledger ``platform.schema_migrations`` records each
  applied migration under (provider, stream, version) with name,
  checksum, timestamp, and applying identity;
- streams are ordered deterministically (topological sort with
  sorted frontier; ties by provider then stream);
- ``core.*`` streams are namespace-reserved to the ``retriva-core``
  provider: an extension cannot overwrite or impersonate them;
- application of a stream runs its bootstrap SQL, adopts legacy
  ledger rows when the stream declares one, then applies pending
  migrations; every migration (DDL + ledger insert) runs in one
  transaction: all or nothing;
- a session-level advisory lock serializes concurrent runners;
- re-running on an up-to-date database is a no-op.

Core never imports a proprietary package; extensions register
provider modules through ``RETRIVA_PG_MIGRATION_PROVIDERS`` (the
same mechanism shape as ``RETRIVA_EXTENSIONS``).  No SQL is ever
built from user or chat input.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from retriva.infrastructure.postgres.errors import MigrationError
from retriva.logger import get_logger

_log = get_logger(__name__)

#: Core-owned ledger location.  The ``platform`` schema is the schema
#: owned by the ``core.platform`` stream (deployment-global
#: infrastructure), not a generic dumping ground.
LEDGER_SCHEMA = "platform"
LEDGER_TABLE = "platform.schema_migrations"

#: Advisory-lock key text (hashed server-side).  Shared with the
#: legacy CRM runner so old and new runners can never race.
_LOCK_KEY_TEXT = "retriva_pg_migrations"

#: Identity of the one provider that may own ``core.*`` streams.
CORE_PROVIDER_ID = "retriva-core"
#: The Core platform stream (ledger infrastructure; always present).
CORE_PLATFORM_STREAM = "core.platform"

_UP_RE = re.compile(r"^V(\d{3,})__(?P<name>[A-Za-z0-9_]+)\.up\.sql$")
_DOWN_RE = re.compile(
    r"^V(\d{3,})__(?P<name>[A-Za-z0-9_]+)\.down\.sql$")

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)*$")


# ---------------------------------------------------------------------------
# Contract types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Migration:
    """One versioned migration in one stream.

    ``checksum`` is ``sha256(up_sql + NUL + down_sql)``; it pins the
    exact applied content.  Applied checksums are never edited.
    """

    provider: str
    stream: str
    version: int
    name: str
    up_sql: str
    down_sql: str
    checksum: str


@dataclass(frozen=True)
class LegacyLedger:
    """Descriptor of a retired ledger table to adopt rows from.

    The legacy table itself is preserved read-only; adoption only
    inserts validated copies into the Core ledger.
    """

    table: str


class MigrationProvider:
    """Base class / documented contract for migration providers.

    Subclasses MUST set ``provider_id`` and ``stream_id`` and
    implement :meth:`discover`.  Overridable hooks provide stream
    dependencies, required roles, bootstrap SQL, legacy-ledger
    adoption, and a destructive-downgrade guard.
    """

    provider_id: str = ""
    stream_id: str = ""

    def discover(self) -> List[Migration]:
        raise NotImplementedError

    def stream_dependencies(self) -> Tuple[str, ...]:
        """Stream ids that must be applied before this stream."""
        return ()

    def required_roles(self) -> Tuple[str, ...]:
        """Database roles this stream's SQL expects to exist."""
        return ()

    def bootstrap_sql(self) -> str:
        """Unversioned, idempotent provider-owned infrastructure DDL
        executed (as the migrator, before any of the stream's
        migrations) — for example the retired ledger table a legacy
        migration references.  Empty string when unused."""
        return ""

    def legacy_ledger(self) -> Optional[LegacyLedger]:
        """Retired ledger whose validated rows are adopted into the
        Core ledger before pending migrations are applied."""
        return None

    def downgrade_guard(self, to: int, *,
                        confirm_destructive: bool) -> None:
        """Raise :class:`MigrationError` to refuse a destructive
        downgrade (before any down SQL runs).  Called only with
        ``confirm_destructive=True`` passes."""
        return None


class SqlMigrationProvider(MigrationProvider):
    """File-based provider: ``V<NNN>__<name>.up.sql`` paired with a
    matching ``.down.sql`` in one directory.  Deterministic:
    derived only from the shipped files."""

    def __init__(
        self,
        provider_id: str,
        stream_id: str,
        sql_dir: Path,
        *,
        dependencies: Tuple[str, ...] = (),
        required_roles: Tuple[str, ...] = (),
        bootstrap_sql: str = "",
        legacy_ledger: Optional[LegacyLedger] = None,
        downgrade_guard=None,
    ) -> None:
        self.provider_id = provider_id
        self.stream_id = stream_id
        self._sql_dir = Path(sql_dir)
        self._dependencies = tuple(dependencies)
        self._required_roles = tuple(required_roles)
        self._bootstrap_sql = bootstrap_sql
        self._legacy_ledger = legacy_ledger
        self._downgrade_guard = downgrade_guard

    def discover(self) -> List[Migration]:
        return discover_sql_migrations(
            self._sql_dir, provider=self.provider_id,
            stream=self.stream_id)

    def stream_dependencies(self) -> Tuple[str, ...]:
        return self._dependencies

    def required_roles(self) -> Tuple[str, ...]:
        return self._required_roles

    def bootstrap_sql(self) -> str:
        return self._bootstrap_sql

    def legacy_ledger(self) -> Optional[LegacyLedger]:
        return self._legacy_ledger

    def downgrade_guard(self, to: int, *,
                        confirm_destructive: bool) -> None:
        if self._downgrade_guard is not None:
            self._downgrade_guard(to,
                                  confirm_destructive=confirm_destructive)


def checksum_for(up_sql: str, down_sql: str) -> str:
    """Deterministic migration checksum (sha256 over up + NUL +
    down).  Identical to the legacy CRM runner's algorithm so
    adopted rows keep their recorded identity."""
    return hashlib.sha256(
        (up_sql + "\x00" + down_sql).encode("utf-8")).hexdigest()


def discover_sql_migrations(
        sql_dir: Path, *, provider: str, stream: str) -> List[Migration]:
    """Load all migration files from ``sql_dir``, validating naming,
    pairing, and uniqueness.  Deterministic."""
    directory = Path(sql_dir)
    if not directory.is_dir():
        raise MigrationError(
            f"migration directory not found: {directory}")
    ups: Dict[int, Tuple[Path, str]] = {}
    downs: Dict[int, Tuple[Path, str]] = {}
    for path in sorted(directory.iterdir()):
        up = _UP_RE.match(path.name)
        down = _DOWN_RE.match(path.name)
        if up:
            version = int(up.group(1))
            if version in ups:
                raise MigrationError(
                    f"duplicate migration version {version} in stream "
                    f"'{stream}': {ups[version][0].name} and {path.name}")
            ups[version] = (path, up.group("name"))
        elif down:
            version = int(down.group(1))
            if version in downs:
                raise MigrationError(
                    f"duplicate downgrade version {version} in stream "
                    f"'{stream}': {path.name}")
            downs[version] = (path, down.group("name"))
    if not ups:
        raise MigrationError(
            f"no migrations found for stream '{stream}' "
            f"({directory})")
    migrations: List[Migration] = []
    for version in sorted(ups):
        up_path, name = ups[version]
        if version not in downs:
            raise MigrationError(
                f"migration V{version:03d}__{name} (stream '{stream}') "
                "has no .down.sql")
        down_path, down_name = downs[version]
        if down_name != name:
            raise MigrationError(
                f"migration V{version:03d} (stream '{stream}') "
                f"up/down name mismatch: {name} vs {down_name}")
        up_sql = up_path.read_text(encoding="utf-8")
        down_sql = down_path.read_text(encoding="utf-8")
        migrations.append(Migration(
            provider=provider, stream=stream, version=version,
            name=name, up_sql=up_sql, down_sql=down_sql,
            checksum=checksum_for(up_sql, down_sql)))
    return migrations


# ---------------------------------------------------------------------------
# Registry: deterministic registration and stream ordering
# ---------------------------------------------------------------------------

class ProviderRegistry:
    """Deterministic registry of migration providers.

    Registration rules (each violation raises :class:`MigrationError`):

    - provider and stream ids are non-empty, lowercase, dotted
      identifiers;
    - a stream id is registered by exactly one provider (no
      overwrites);
    - ``core.*`` streams are reserved to the ``retriva-core``
      provider, and the ``retriva-core`` provider identity is
      reserved to the Core platform provider registered once through
      :meth:`register_core_platform` (no extension may impersonate
      either);
    - stream dependencies must be registered and acyclic
      (validated when ordering).

    Ordering is a deterministic topological sort (Kahn's algorithm
    with a sorted frontier; ties broken by provider id then stream
    id).
    """

    def __init__(self) -> None:
        self._providers: Dict[str, MigrationProvider] = {}
        self._core_platform_registered = False

    # -- Registration -----------------------------------------------------

    def register_core_platform(self, provider: MigrationProvider) -> None:
        """Register the one Core platform provider
        (``retriva-core`` / ``core.platform``).  Callable once."""
        if self._core_platform_registered:
            raise MigrationError(
                "the Core platform provider is already registered")
        _validate_identity(provider)
        if provider.provider_id != CORE_PROVIDER_ID:
            raise MigrationError(
                "the Core platform provider must have provider_id "
                f"'{CORE_PROVIDER_ID}'")
        if provider.stream_id != CORE_PLATFORM_STREAM:
            raise MigrationError(
                "the Core platform provider must own stream "
                f"'{CORE_PLATFORM_STREAM}'")
        self._providers[provider.stream_id] = provider
        self._core_platform_registered = True

    def register(self, provider: MigrationProvider) -> None:
        """Register an extension (or additional Core module)
        provider."""
        _validate_identity(provider)
        if provider.provider_id == CORE_PROVIDER_ID:
            raise MigrationError(
                f"provider id '{CORE_PROVIDER_ID}' is reserved to the "
                "Core platform provider and cannot be registered by "
                "an extension")
        if provider.stream_id.startswith("core."):
            raise MigrationError(
                f"stream '{provider.stream_id}' is in the 'core.' "
                "namespace reserved to the 'retriva-core' provider; "
                f"provider '{provider.provider_id}' cannot register "
                "it (impersonation refused)")
        if provider.stream_id in self._providers:
            raise MigrationError(
                f"migration stream '{provider.stream_id}' is already "
                f"registered by provider "
                f"'{self._providers[provider.stream_id].provider_id}'; "
                f"provider '{provider.provider_id}' cannot overwrite "
                "it")
        self._providers[provider.stream_id] = provider

    # -- Resolution -------------------------------------------------------

    def stream_ids(self) -> List[str]:
        """All registered stream ids, deterministically ordered for
        application (dependencies first)."""
        return _order_streams(dict(self._providers))

    def provider(self, stream_id: str) -> MigrationProvider:
        provider = self._providers.get(stream_id)
        if provider is None:
            raise MigrationError(
                f"migration stream '{stream_id}' is not registered")
        return provider

    def providers_in_order(self) -> List[MigrationProvider]:
        return [self._providers[s] for s in self.stream_ids()]

    def migrations_for(self, stream_id: str) -> List[Migration]:
        """Deterministic migration list for a stream, version
        ascending; rejects duplicate provider/stream/version
        identities."""
        provider = self.provider(stream_id)
        migrations = sorted(
            provider.discover(), key=lambda m: m.version)
        seen: Dict[int, Migration] = {}
        for migration in migrations:
            existing = seen.get(migration.version)
            if existing is not None:
                raise MigrationError(
                    f"duplicate provider/stream/version identity: "
                    f"{migration.provider}/{migration.stream}/"
                    f"V{migration.version:03d} "
                    f"({existing.name} and {migration.name})")
            seen[migration.version] = migration
        return migrations

    def as_status(self) -> Dict[str, str]:
        """Human-inspectable provider/stream map (no secrets)."""
        return {stream: self._providers[stream].provider_id
                for stream in self.stream_ids()}


def _validate_identity(provider: MigrationProvider) -> None:
    if not _ID_RE.match(provider.provider_id or ""):
        raise MigrationError(
            f"invalid provider id {provider.provider_id!r} (expected "
            "a lowercase dotted identifier)")
    if not _ID_RE.match(provider.stream_id or ""):
        raise MigrationError(
            f"invalid stream id {provider.stream_id!r} (expected a "
            "lowercase dotted identifier)")
    if provider.provider_id != provider.provider_id.lower():
        raise MigrationError(f"provider id {provider.provider_id!r} "
                             "must be lowercase")
    if provider.stream_id != provider.stream_id.lower():
        raise MigrationError(f"stream id {provider.stream_id!r} must "
                             "be lowercase")


def _order_streams(
        providers: Dict[str, MigrationProvider]) -> List[str]:
    """Deterministic topological order over stream dependencies.

    Missing dependencies and dependency cycles raise
    :class:`MigrationError`."""
    remaining = dict(providers)
    ordered: List[str] = []
    resolved: set = set()
    while remaining:
        ready = sorted(
            stream for stream, provider in remaining.items()
            if all(dep in resolved
                   for dep in provider.stream_dependencies()))
        # Detect cycles before recursing into malformed sets.
        blocked = [
            stream for stream, provider in remaining.items()
            if stream not in ready]
        for stream in blocked:
            for dep in remaining[stream].stream_dependencies():
                if dep not in providers:
                    raise MigrationError(
                        f"stream '{stream}' depends on stream "
                        f"'{dep}', which is not registered (missing "
                        "dependency)")
        if not ready:
            raise MigrationError(
                "dependency cycle between migration streams: "
                + ", ".join(sorted(remaining)))
        for stream in ready:
            ordered.append(stream)
            resolved.add(stream)
            remaining.pop(stream)
    return ordered


# ---------------------------------------------------------------------------
# Platform provider and provider loading
# ---------------------------------------------------------------------------

#: Directory with the ``core.platform`` stream SQL files.
PLATFORM_SQL_DIR = Path(__file__).resolve().parent / "sql"

#: Env var with comma-separated provider module paths (each module
#: exposes ``MIGRATION_PROVIDERS``: a list of provider instances).
PROVIDERS_ENV = "RETRIVA_PG_MIGRATION_PROVIDERS"


def platform_provider() -> SqlMigrationProvider:
    """The Core platform provider: the ``core.platform`` stream
    (platform schema and ledger grants)."""
    return SqlMigrationProvider(
        provider_id=CORE_PROVIDER_ID,
        stream_id=CORE_PLATFORM_STREAM,
        sql_dir=PLATFORM_SQL_DIR,
        required_roles=("retriva_migrator", "retriva_core"),
    )


def load_provider_modules(
        provider_modules_csv: str) -> List[MigrationProvider]:
    """Import provider modules (comma-separated dotted paths) and
    collect their ``MIGRATION_PROVIDERS`` lists.  Deterministic:
    modules are imported in the given order; registration order never
    affects application order (the registry sorts deterministically).

    Core never hard-codes a proprietary module name; the deployment
    lists the enabled extensions' provider modules itself.  A module
    that cannot be imported, or that does not declare providers, fails
    clearly (a typo in ``RETRIVA_PG_MIGRATION_PROVIDERS`` must never
    silently skip an extension's migrations: the one-shot exits
    non-zero and dependent services do not start)."""
    import importlib

    providers: List[MigrationProvider] = []
    if not provider_modules_csv.strip():
        return providers
    for module_path in provider_modules_csv.split(","):
        module_path = module_path.strip()
        if not module_path:
            continue
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            raise MigrationError(
                f"provider module '{module_path}' could not be imported "
                f"({exc.__class__.__name__}: {exc}); is the extension "
                "installed and enabled for this build?  Check "
                "RETRIVA_PG_MIGRATION_PROVIDERS") from exc
        declared = getattr(module, "MIGRATION_PROVIDERS", None)
        if not declared:
            raise MigrationError(
                f"provider module '{module_path}' does not expose "
                "MIGRATION_PROVIDERS")
        for provider in declared:
            if not isinstance(provider, MigrationProvider):
                raise MigrationError(
                    f"provider module '{module_path}' declares a "
                    "non-provider object in MIGRATION_PROVIDERS")
            providers.append(provider)
    return providers


def load_provider_registry(
        provider_modules_csv: str = "") -> ProviderRegistry:
    """Build the provider registry: the Core platform provider first
    (always), then the deployment's listed provider modules.  A
    Core-only deployment passes an empty list and stays free of any
    extension package."""
    registry = ProviderRegistry()
    registry.register_core_platform(platform_provider())
    for provider in load_provider_modules(provider_modules_csv):
        registry.register(provider)
    return registry


# ---------------------------------------------------------------------------
# Runner (deployment-time controlled migration step)
# ---------------------------------------------------------------------------

def _connect_migrator(settings):
    import psycopg2

    from retriva.infrastructure.postgres.errors import (
        PostgresConnectionError,
    )
    try:
        conn = psycopg2.connect(**settings.connection_kwargs("migrator"))
    except PostgresConnectionError:
        raise
    except psycopg2.Error as exc:
        raise PostgresConnectionError(
            "cannot connect as the migrator role "
            f"({exc.__class__.__name__}); is the database deployed and "
            "have the roles been bootstrapped?") from exc
    conn.autocommit = True
    return conn


def _require_roles(conn, providers: List[MigrationProvider]) -> None:
    """Fail with an actionable error when any role a stream's SQL
    expects is missing (bootstrap step not run)."""
    expected = sorted({
        role
        for provider in providers
        for role in provider.required_roles()})
    if not expected:
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
            (expected,))
        present = {row[0] for row in cur.fetchall()}
    missing = sorted(set(expected) - present)
    if missing:
        raise MigrationError(
            f"managed roles missing: {', '.join(missing)}; run the "
            "deployment bootstrap step first "
            "(python -m retriva.infrastructure.postgres.migrate "
            "bootstrap, then the extension's own bootstrap)")


def ensure_ledger(conn, migrator_role: str) -> None:
    """Create the Core platform schema and migration ledger if
    absent.  Runs as the migrator (schema owner).  Identifiers are
    quoted through psycopg2.sql; the fixed names are package
    constants."""
    from psycopg2 import sql as pg_sql

    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}")
            .format(pg_sql.Identifier(LEDGER_SCHEMA),
                    pg_sql.Identifier(migrator_role)))
        cur.execute(
            pg_sql.SQL("CREATE TABLE IF NOT EXISTS {}.{} ("
                       "provider   TEXT NOT NULL,"
                       "stream     TEXT NOT NULL,"
                       "version    INTEGER NOT NULL,"
                       "name       TEXT NOT NULL,"
                       "checksum   TEXT NOT NULL,"
                       "applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                       "applied_by TEXT NOT NULL DEFAULT current_user,"
                       "PRIMARY KEY (provider, stream, version))")
            .format(pg_sql.Identifier(LEDGER_SCHEMA),
                    pg_sql.Identifier("schema_migrations")))


def applied_revisions(conn, provider_id: str,
                      stream_id: str) -> Dict[int, Dict]:
    """Ledger rows for one stream, keyed by version."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT version, name, checksum, applied_at, applied_by "
            f"FROM {LEDGER_TABLE} WHERE provider = %s AND stream = %s "
            "ORDER BY version",
            (provider_id, stream_id))
        rows = cur.fetchall()
    return {
        int(r[0]): {
            "version": int(r[0]), "name": r[1], "checksum": r[2],
            "applied_at": r[3].isoformat() if r[3] else None,
            "applied_by": r[4],
        } for r in rows
    }


def _legacy_ledger_rows(conn, legacy: LegacyLedger) -> List[Dict]:
    """Rows of a retired ledger table (empty when the table does not
    exist).  Read-only: legacy rows are never modified here."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (legacy.table,))
        if not cur.fetchone()[0]:
            return []
        cur.execute(
            f"SELECT version, name, checksum, applied_at, applied_by "
            f"FROM {legacy.table} ORDER BY version")
        rows = cur.fetchall()
    return [
        {
            "version": int(r[0]), "name": r[1], "checksum": r[2],
            "applied_at": r[3], "applied_by": r[4],
        } for r in rows
    ]


def _adopt_legacy_ledger(conn, provider: MigrationProvider) -> List[Dict]:
    """Adopt applied rows from a stream's retired legacy ledger into
    the Core ledger.

    Rules (Spec 024 §3.8): every legacy row's version must exist in
    the shipped migrations with an identical checksum and name;
    adoption is one transaction; partial-adoption states fail
    clearly; legacy rows are never modified or deleted; already
    adopted streams are detected and skipped.  Returns the adopted
    rows (empty when there was nothing to do)."""
    legacy = provider.legacy_ledger()
    if legacy is None:
        return []
    try:
        shipped = {m.version: m for m in provider.discover()}
        legacy_rows = _legacy_ledger_rows(conn, legacy)
        if not legacy_rows:
            return []
        current = applied_revisions(conn, provider.provider_id,
                                     provider.stream_id)
        legacy_versions = {row["version"] for row in legacy_rows}
        new_versions = set(current)
        if legacy_versions <= new_versions:
            # Adoption already performed earlier; ledger rows are
            # checksum-verified by the caller.
            return []
        if new_versions:
            raise MigrationError(
                f"ambiguous adoption state for stream "
                f"'{provider.stream_id}': the legacy ledger "
                f"({legacy.table}) has applied versions "
                f"{sorted(legacy_versions - new_versions)} while the "
                "Core ledger already has different rows; refusing to "
                "reinterpret ownership (inspect both ledgers and "
                "resolve manually)")
        adopted: List[Dict] = []
        for row in legacy_rows:
            migration = shipped.get(row["version"])
            if migration is None:
                raise MigrationError(
                    f"cannot adopt legacy ledger {legacy.table} for "
                    f"stream '{provider.stream_id}': applied version "
                    f"V{row['version']:03d} has no shipped migration")
            if row["checksum"] != migration.checksum:
                raise MigrationError(
                    f"cannot adopt legacy ledger {legacy.table} for "
                    f"stream '{provider.stream_id}': checksum drift on "
                    f"V{row['version']:03d}__{row['name']} (ledger "
                    "record no longer matches the shipped files)")
            if row["name"] != migration.name:
                raise MigrationError(
                    f"cannot adopt legacy ledger {legacy.table} for "
                    f"stream '{provider.stream_id}': name drift on "
                    f"V{row['version']:03d} (ledger "
                    f"{row['name']!r} vs shipped {migration.name!r})")
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {LEDGER_TABLE} (provider, stream, "
                    "version, name, checksum, applied_at, applied_by) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (provider.provider_id, provider.stream_id,
                     row["version"], row["name"], row["checksum"],
                     row["applied_at"], row["applied_by"]))
            adopted.append({
                "version": row["version"], "name": row["name"],
                "checksum": row["checksum"],
                "adopted_from": legacy.table,
            })
        conn.commit()
        _log.info(
            "adopted %d applied legacy ledger records from %s into "
            "%s for stream '%s' (identity preserved)",
            len(adopted), legacy.table, LEDGER_TABLE, provider.stream_id)
        return adopted
    except MigrationError:
        import psycopg2

        try:
            conn.rollback()
        except psycopg2.Error:
            pass
        raise
    except Exception as exc:
        import psycopg2

        try:
            conn.rollback()
        except psycopg2.Error:
            pass
        raise MigrationError(
            f"legacy-ledger adoption failed for stream "
            f"'{provider.stream_id}' ({exc.__class__.__name__}); the "
            "adoption transaction was rolled back") from exc


def _run_bootstrap_sql(conn, provider: MigrationProvider) -> None:
    """Execute a stream's unversioned idempotent bootstrap SQL (as
    the migrator) when declared."""
    bootstrap_sql = provider.bootstrap_sql()
    if not bootstrap_sql:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(bootstrap_sql)
        conn.commit()
    except Exception as exc:
        import psycopg2

        if isinstance(exc, psycopg2.Error):
            try:
                conn.rollback()
            except psycopg2.Error:
                pass
        raise MigrationError(
            f"bootstrap SQL for stream '{provider.stream_id}' failed "
            f"({exc.__class__.__name__})") from exc


def _acquire_lock(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_lock(hashtext(%s))", (_LOCK_KEY_TEXT,))


def _release_lock(conn) -> None:
    import psycopg2

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_unlock(hashtext(%s))",
                (_LOCK_KEY_TEXT,))
    except psycopg2.Error:
        pass


def upgrade(registry: ProviderRegistry, settings,
            to: Optional[int] = None) -> Dict[str, List[Dict]]:
    """Apply all pending migrations of all registered streams, in
    deterministic stream order; returns per-stream what was adopted
    and applied.  Idempotent: nothing to do on an up-to-date
    database."""
    providers = registry.providers_in_order()
    conn = _connect_migrator(settings)
    result: Dict[str, List[Dict]] = {}
    try:
        _require_roles(conn, providers)
        # The lock must cover ledger creation too: two concurrent
        # runners would otherwise race CREATE TABLE IF NOT EXISTS.
        _acquire_lock(conn)
        ensure_ledger(conn, getattr(
            settings, "migrator_user", "retriva_migrator"))
        conn.autocommit = False
        for provider in providers:
            _run_bootstrap_sql(conn, provider)
            adopted = _adopt_legacy_ledger(conn, provider)
            current = applied_revisions(conn, provider.provider_id,
                                        provider.stream_id)
            shipped = registry.migrations_for(provider.stream_id)
            applied: List[Dict] = []
            for migration in shipped:
                if to is not None and migration.version > to:
                    break
                recorded = current.get(migration.version)
                if recorded is not None:
                    if recorded["checksum"] != migration.checksum:
                        raise MigrationError(
                            f"migration V{migration.version:03d}__"
                            f"{migration.name} of stream "
                            f"'{migration.stream}' on disk no longer "
                            "matches the applied ledger entry "
                            "(checksum drift)")
                    continue
                try:
                    with conn.cursor() as cur:
                        cur.execute(migration.up_sql)
                        cur.execute(
                            f"INSERT INTO {LEDGER_TABLE} (provider, "
                            "stream, version, name, checksum) VALUES "
                            "(%s, %s, %s, %s, %s)",
                            (migration.provider, migration.stream,
                             migration.version, migration.name,
                             migration.checksum))
                    conn.commit()
                except MigrationError:
                    raise
                except Exception as exc:
                    import psycopg2

                    if isinstance(exc, psycopg2.Error):
                        try:
                            conn.rollback()
                        except psycopg2.Error:
                            pass
                        raise MigrationError(
                            f"migration V{migration.version:03d}__"
                            f"{migration.name} of stream "
                            f"'{migration.stream}' failed "
                            f"({exc.__class__.__name__}); the migration's "
                            "transaction was rolled back") from exc
                    raise
                _log.info(
                    "migration applied: provider=%s stream=%s "
                    "version=%03d name=%s",
                    migration.provider, migration.stream,
                    migration.version, migration.name)
                applied.append({
                    "version": migration.version,
                    "name": migration.name,
                    "checksum": migration.checksum,
                })
            result[provider.stream_id] = [
                {"adopted": adopted, "applied": applied}]
        return result
    except Exception:
        import psycopg2

        try:
            conn.rollback()
        except psycopg2.Error:
            pass
        raise
    finally:
        import psycopg2

        try:
            conn.autocommit = True
            _release_lock(conn)
        except psycopg2.Error:
            pass
        conn.close()


def downgrade(registry: ProviderRegistry, settings, stream: str, *,
              to: int,
              confirm_destructive: bool = False) -> List[Dict]:
    """Revert stream ``stream`` to version ``to`` (0 = all).

    Destructive by definition: ``confirm_destructive=True`` (CLI:
    ``--confirm-destructive``) is the required, documented
    destructive override; the provider's downgrade guard still runs
    and may refuse (for example when persisted domain history would
    be dropped)."""
    if not confirm_destructive:
        raise MigrationError(
            "downgrade is destructive; pass confirm_destructive=True "
            "(CLI: --confirm-destructive)")
    provider = registry.provider(stream)
    provider.downgrade_guard(to, confirm_destructive=confirm_destructive)
    conn = _connect_migrator(settings)
    reverted: List[Dict] = []
    try:
        ensure_ledger(conn, getattr(
            settings, "migrator_user", "retriva_migrator"))
        _acquire_lock(conn)
        conn.autocommit = False
        current = applied_revisions(conn, provider.provider_id,
                                   provider.stream_id)
        by_version = {m.version: m
                      for m in registry.migrations_for(stream)}
        for version in sorted(
                (v for v in current if v > to), reverse=True):
            migration = by_version.get(version)
            if migration is None:
                raise MigrationError(
                    f"applied migration V{version:03d} of stream "
                    f"'{stream}' has no files on disk; cannot build a "
                    "downgrade")
            if not migration.down_sql.strip():
                raise MigrationError(
                    f"migration V{version:03d}__{migration.name} of "
                    f"stream '{stream}' has an empty down script; "
                    "cannot downgrade")
            with conn.cursor() as cur:
                cur.execute(migration.down_sql)
                cur.execute(
                    f"DELETE FROM {LEDGER_TABLE} WHERE provider = %s "
                    "AND stream = %s AND version = %s",
                    (migration.provider, migration.stream, version))
            conn.commit()
            _log.info(
                "migration reverted: provider=%s stream=%s "
                "version=%03d name=%s",
                migration.provider, migration.stream, migration.version,
                migration.name)
            reverted.append({"version": version,
                             "name": migration.name})
        return reverted
    except Exception:
        import psycopg2

        try:
            conn.rollback()
        except psycopg2.Error:
            pass
        raise
    finally:
        import psycopg2

        try:
            conn.autocommit = True
            _release_lock(conn)
        except psycopg2.Error:
            pass
        conn.close()


def status(registry: ProviderRegistry, settings) -> Dict:
    """Inspectable migration status (ledger vs shipped files), per
    stream."""
    conn = _connect_migrator(settings)
    try:
        conn.autocommit = True
        ensure_ledger(conn, getattr(
            settings, "migrator_user", "retriva_migrator"))
        streams: Dict[str, Dict] = {}
        for provider in registry.providers_in_order():
            shipped = registry.migrations_for(provider.stream_id)
            current = applied_revisions(conn, provider.provider_id,
                                        provider.stream_id)
            pending = [
                {"version": m.version, "name": m.name}
                for m in shipped if m.version not in current
            ]
            streams[provider.stream_id] = {
                "provider": provider.provider_id,
                "current_revision": max(current) if current else None,
                "applied": [current[v] for v in sorted(current)],
                "pending": pending,
                "latest_available": shipped[-1].version,
            }
        return {"streams": streams}
    finally:
        conn.close()


def verify(registry: ProviderRegistry, settings) -> Dict:
    """Framework invariants: every registered stream fully applied
    (checksum-identical), ledger present, no duplicate migration
    identities.  Never mutates data.  Domain invariants (RLS,
    grants, triggers) are verified by the owning extension's own
    oracle."""
    conn = _connect_migrator(settings)
    checks: List[Dict] = []
    try:
        conn.autocommit = True
        ensure_ledger(conn, getattr(
            settings, "migrator_user", "retriva_migrator"))
        for provider in registry.providers_in_order():
            shipped = registry.migrations_for(provider.stream_id)
            shipped_by_version = {m.version: m for m in shipped}
            current = applied_revisions(conn, provider.provider_id,
                                        provider.stream_id)
            all_applied = (
                {v for v in current} == set(shipped_by_version))
            drifted = sorted(
                f"V{v:03d}" for v, row in current.items()
                if v in shipped_by_version
                and row["checksum"] != shipped_by_version[v].checksum)
            checks.append({
                "check": f"stream_fully_applied[{provider.stream_id}]",
                "ok": all_applied and not drifted,
                "detail": (
                    f"applied={len(current)} shipped={len(shipped)}"
                    + (f" drift={drifted}" if drifted else "")),
            })
        # Duplicate identity protection is enforced by the registry
        # construction itself; assert the ordering shape once more.
        try:
            ordered = registry.stream_ids()
            checks.append({
                "check": "stream_ordering_deterministic",
                "ok": len(ordered) == len(set(ordered)),
                "detail": "streams=" + ",".join(ordered),
            })
        except MigrationError as exc:
            checks.append({
                "check": "stream_ordering_deterministic",
                "ok": False,
                "detail": str(exc),
            })
        return {
            "ok": all(c["ok"] for c in checks),
            "checks": checks,
            "ledger": LEDGER_TABLE,
        }
    finally:
        conn.close()
