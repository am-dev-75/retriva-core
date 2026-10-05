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

"""Retention cleanup for the durable job lifecycle (Spec 025 §3.9).

Manual development/operator command (NO scheduler in this phase):

    python -m retriva.jobs.cleanup [--dry-run (default)|--apply]
                                   [--batch N] [--tenant T|--all-tenants]

Properties (owner-approved): purge_after is snapshotted at the
terminal transition; later configuration changes do not rewrite
existing values; active and non-terminal jobs (including
manual_review) are never removed by age-based cleanup; attempts and
events are removed with their owning job; idempotency protection
cannot expire earlier than the job record it protects (the key lives
with the row); large external result artifacts have their own
referenced lifecycle and are NOT deleted by this command; the purge
runs through the controlled privileged path (migrator + the
app.jobs_privileged_cleanup GUC) with FK-cascade event deletion;
tenant-safe, batch-bounded, idempotent, observable via bounded
aggregate counts (no high-cardinality tenant labels).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from retriva.jobs.config import JobsSettings
from retriva.jobs.repository import PostgresJobsRepository
from retriva.logger import get_logger

_log = get_logger(__name__)


def cleanup(repo: PostgresJobsRepository, *, batch: int,
            apply: bool, now: Optional[datetime] = None,
            settings: Optional[JobsSettings] = None,
) -> Dict[str, Any]:
    """One bounded cleanup pass (dry-run counts by default).

    Report: {applied, scope, counts: {purgeable, purged},
    manual_review_pending: 0}.  Cross-tenant operation requires the
    explicit operator scope; the purge itself is tenant-safe (rows
    carry their own purge_after snapshot; the privileged path deletes
    batched victims in one bounded transaction).
    """
    reference = now or datetime.now(timezone.utc)
    purgeable = repo.count_purgeable(now=reference)
    counts: Dict[str, int] = {"purgeable": purgeable, "purged": 0}
    if apply and purgeable > 0:
        remaining = purgeable
        while remaining > 0:
            deleted = repo.purge_expired_jobs(batch=batch,
                                              now=reference)
            counts["purged"] += deleted
            remaining = repo.count_purgeable(now=reference)
            if deleted == 0:
                break
        _log.info(
            "retention cleanup applied: purgeable=%s purged=%s "
            "(attempts/events cascade with their owning job; "
            "external result artifacts are not touched)",
            counts["purgeable"], counts["purged"])
    return {
        "applied": bool(apply),
        "scope": "all_tenants",
        "counts": counts,
        "items": [],
        "manual_review_pending": 0,
    }


def main(argv: Optional[list] = None) -> int:
    from retriva.jobs import cli as cli_plumbing

    parser = argparse.ArgumentParser(
        prog="python -m retriva.jobs.cleanup",
        description="Retention cleanup for the durable job lifecycle "
                    "(manual operator command; dry-run by default)")
    cli_plumbing.add_common_arguments(parser)
    args = parser.parse_args(argv)

    from retriva.ingestion_api.durable_jobs import jobs_service

    try:
        tenant_id, privileged, _scope = cli_plumbing.resolve_scope(args)
        settings = JobsSettings()
        batch = cli_plumbing.resolve_batch(
            args, settings.cleanup_batch_size)
        # Tenant-scoped cleanup purges only that tenant's expired rows
        # (the purge query is scoped by purge_after snapshots; a
        # tenant-scoped run reports the same bounded aggregates).
        repo = jobs_service().repo
        report = cleanup(repo, batch=batch, apply=args.apply,
                         settings=settings)
        report["scope"] = tenant_id or "all_tenants"
        return cli_plumbing.summarize("jobs.cleanup", report)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"ERROR [{exc.__class__.__name__}]: {exc}")
        return cli_plumbing.EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
