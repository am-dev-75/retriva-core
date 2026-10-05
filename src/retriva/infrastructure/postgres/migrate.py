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

"""Deployment-time platform CLI.

Run: ``python -m retriva.infrastructure.postgres.migrate <command>``

- ``bootstrap`` — provision the Core platform roles (admin conn);
- ``upgrade``   — apply pending migrations of all registered
  streams (migrator conn only; providers from
  ``RETRIVA_PG_MIGRATION_PROVIDERS`` or ``--providers``);
- ``status``    — per-stream ledger and pending state;
- ``downgrade`` — revert one stream (requires
  ``--confirm-destructive``);
- ``verify``    — framework invariant report;
- ``readiness`` — connectivity/ledger report (no credentials).

Never run by application processes; a controlled one-shot step only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from retriva.infrastructure.postgres.bootstrap import (
    bootstrap_platform,
)
from retriva.infrastructure.postgres.config import (
    get_platform_settings,
)
from retriva.infrastructure.postgres.errors import (
    PostgresNotConfiguredError,
)
from retriva.infrastructure.postgres.migrations import (
    PROVIDERS_ENV,
    LEDGER_TABLE,
    downgrade,
    load_provider_modules,
    load_provider_registry,
    status,
    upgrade,
    verify,
)
from retriva.logger import get_logger

_log = get_logger(__name__)

#: Core-owned migration streams registered by THIS CLI next to
#: ``core.platform`` (Core→Core import only; Spec 025).  The existing
#: core one-shot therefore applies ``core.jobs`` without any Compose
#: change and without any extension provider listed.  Extension
#: providers (``RETRIVA_PG_MIGRATION_PROVIDERS`` / ``--providers``)
#: remain deployment-listed and may never own ``core.*`` streams.
CORE_STREAM_PROVIDER_MODULES = ("retriva.jobs.migrations",)


def _cli_summary(data) -> str:
    return json.dumps(data, indent=2, default=str)


def platform_readiness(settings) -> dict:
    """Platform readiness (no credentials): connectivity as the Core
    runtime role, ledger readability, applied migration count."""
    payload = {
        "status": "degraded",
        "database": settings.database,
        "ledger": LEDGER_TABLE,
    }
    try:
        import psycopg2

        conn = psycopg2.connect(**settings.connection_kwargs("core"))
    except Exception as exc:  # noqa: BLE001 - readiness never 500s
        payload["connectivity"] = {
            "reachable": False,
            "error": exc.__class__.__name__,
        }
        payload["status"] = "unreachable"
        return payload
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.execute(
                f"SELECT count(*) FROM {LEDGER_TABLE}")
            applied = int(cur.fetchone()[0])
        payload["connectivity"] = {"reachable": True}
        payload["ledger_readable"] = True
        payload["applied_migrations"] = applied
        payload["status"] = "ok"
    except psycopg2.Error as exc:
        payload["connectivity"] = {
            "reachable": True,
            "error": exc.__class__.__name__,
        }
        payload["status"] = "degraded"
    finally:
        conn.close()
    return payload


def _registry_from(providers_csv: Optional[str]):
    csv_value = (providers_csv
                 if providers_csv is not None
                 else os.environ.get(PROVIDERS_ENV, ""))
    registry = load_provider_registry(csv_value)
    # Core-owned streams are registered by the Core CLI itself
    # (Core→Core import; deterministic ordering places them after
    # ``core.platform`` per their declared dependencies).
    for module_path in CORE_STREAM_PROVIDER_MODULES:
        for provider in load_provider_modules(module_path):
            registry.register_core_stream(provider)
    return registry


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m retriva.infrastructure.postgres.migrate",
        description="Retriva shared-PostgreSQL platform tool "
                    "(controlled deployment step; never run by "
                    "application processes)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "bootstrap",
        help="create/update the Core platform roles "
             "(migrator, core runtime; admin connection)")
    p_up = sub.add_parser("upgrade", help="apply pending migrations")
    p_up.add_argument("--providers", default=None,
                      help=f"comma-separated provider modules "
                           f"(default: ${PROVIDERS_ENV})")
    p_up.add_argument("--to", type=int, default=None,
                      help="target version per stream (default: latest)")
    p_st = sub.add_parser("status", help="per-stream ledger and "
                                         "pending state")
    p_st.add_argument("--providers", default=None)
    p_dn = sub.add_parser("downgrade", help="revert one stream")
    p_dn.add_argument("--providers", default=None)
    p_dn.add_argument("--stream", required=True,
                     help="stream to downgrade")
    p_dn.add_argument("--to", type=int, required=True,
                      help="revert to this version (0 = all)")
    p_dn.add_argument("--confirm-destructive", action="store_true",
                      help="required; acknowledges data loss")
    p_vf = sub.add_parser("verify", help="framework invariant report")
    p_vf.add_argument("--providers", default=None)
    p_rf = sub.add_parser("readiness", help="readiness report "
                                            "(no credentials)")

    args = parser.parse_args(argv)
    settings = get_platform_settings()

    try:
        if args.command == "bootstrap":
            print(_cli_summary(bootstrap_platform(settings)))
        elif args.command == "upgrade":
            registry = _registry_from(args.providers)
            applied = upgrade(registry, settings, to=args.to)
            print(_cli_summary({"streams": applied}))
        elif args.command == "status":
            registry = _registry_from(args.providers)
            print(_cli_summary(status(registry, settings)))
        elif args.command == "downgrade":
            registry = _registry_from(args.providers)
            reverted = downgrade(
                registry, settings, args.stream, to=args.to,
                confirm_destructive=args.confirm_destructive)
            print(_cli_summary({"reverted": reverted}))
        elif args.command == "verify":
            registry = _registry_from(args.providers)
            report = verify(registry, settings)
            print(_cli_summary(report))
            if not report["ok"]:
                return 1
        elif args.command == "readiness":
            payload = platform_readiness(settings)
            print(_cli_summary(payload))
            if payload.get("status") not in ("ok",):
                return 1
        return 0
    except PostgresNotConfiguredError as exc:
        print(f"ERROR [{exc.__class__.__name__}]: {exc}",
              file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        # Exception classes carry actionable, secret-free messages.
        print(f"ERROR [{exc.__class__.__name__}]: {exc}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
