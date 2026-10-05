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

"""Shared plumbing for the operator commands (Spec 025 §3.8).

Commands (dry-run DEFAULT, explicit ``--apply``; bounded batches;
explicit operational context for cross-tenant operation; every applied
action event-logged; uncertain external side effects surfaced for
manual review, never auto-replayed):

    python -m retriva.jobs.reconcile [--dry-run|--apply] [--batch N]
                                     [--tenant T|--all-tenants]
    python -m retriva.jobs.cleanup  [--dry-run|--apply] [--batch N]
                                    [--tenant T|--all-tenants]
    python -m retriva.jobs.retry <job_id> [--reason S]
                                 [--override-max-attempts]
                                 [--tenant T]

Exit codes: 0 = clean completion / no changes; 3 = changes applied;
4 = unresolved manual-review items; 1 = operational failure.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List, Optional

from retriva.logger import get_logger

_log = get_logger(__name__)

EXIT_OK = 0
EXIT_APPLIED = 3
EXIT_MANUAL_REVIEW = 4
EXIT_FAILURE = 1

_MAX_ITEM_LINES = 20


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="(default) compute and report actions without applying")
    parser.add_argument(
        "--apply", action="store_true",
        help="apply the computed actions (bounded batches)")
    parser.add_argument(
        "--batch", type=int, default=None,
        help="batch size (default: RETRIVA_JOBS_RECONCILE_BATCH_SIZE "
             "for reconcile, RETRIVA_JOBS_CLEANUP_BATCH_SIZE for "
             "cleanup)")
    tenant_group = parser.add_mutually_exclusive_group()
    tenant_group.add_argument(
        "--tenant", type=str, default=None,
        help="operate within one tenant (explicit operational "
             "context)")
    tenant_group.add_argument(
        "--all-tenants", action="store_true",
        help="operate across tenants (explicit operator scope; "
             "privileged context)")
    parser.add_argument(
        "--json", action="store_true",
        help="machine-readable report")


def resolve_batch(args: argparse.Namespace,
                  default: int) -> int:
    batch = args.batch or default
    return max(1, int(batch))


def resolve_scope(args: argparse.Namespace) -> tuple:
    """(tenant_id | None, privileged, scope_label)."""
    if getattr(args, "all_tenants", False):
        return None, True, "all_tenants"
    if args.tenant:
        from retriva.infrastructure.postgres.tenant import (
            validate_tenant_id,
        )
        return validate_tenant_id(args.tenant), False, args.tenant
    # No scope at all: refuse (explicit operational context required).
    raise SystemExit(
        "ERROR: an explicit operational context is required: pass "
        "--tenant T (single tenant) or --all-tenants (privileged "
        "cross-tenant operation)")


def summarize(label: str, report: Dict[str, Any]) -> int:
    """Print a bounded report and derive the exit code."""
    applied = bool(report.get("applied"))
    counts = report.get("counts") or {}
    items = report.get("items") or []
    payload = {
        "command": label,
        "mode": "apply" if applied else "dry-run",
        "scope": report.get("scope"),
        "counts": counts,
        "items": [
            {k: item.get(k) for k in
             ("job_id", "classification", "action", "note")}
            for item in items[:_MAX_ITEM_LINES]
        ],
        "items_total": len(items),
        "manual_review_pending": report.get("manual_review_pending", 0),
    }
    _log.info("operator command report: %s", json.dumps(
        payload, default=str))
    if not applied:
        print(json.dumps(payload, indent=2, default=str))
        return EXIT_OK
    print(json.dumps(payload, indent=2, default=str))
    if report.get("manual_review_pending", 0) > 0:
        return EXIT_MANUAL_REVIEW
    if any(counts.values()):
        return EXIT_APPLIED
    return EXIT_OK
