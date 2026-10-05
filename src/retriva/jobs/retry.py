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

"""Operator manual retry (Spec 025 §3.12; owner decision 5).

OPERATOR CLI / administrative surface ONLY — NEVER exposed through
the unauthenticated public API:

    python -m retriva.jobs.retry <job_id> [--reason S]
                                 [--override-max-attempts]
                                 [--tenant T]

Operator retry requires an explicitly retryable terminal job state
(``failed``), is tenant-safe (explicit operational ``--tenant``),
uses the operational identity (never an arbitrary caller-selected
tenant context), creates a NEW durable attempt (previous attempt
history and errors are never erased), records append-only events with
a bounded actor classification, obeys ``max_attempts`` unless an
override is durably recorded, and is idempotent under duplicate
invocation where practical (the status guard refuses a second
concurrent retry of the same terminal state).
"""

from __future__ import annotations

import argparse
from typing import Optional

from retriva.jobs import cli as cli_plumbing
from retriva.logger import get_logger

_log = get_logger(__name__)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m retriva.jobs.retry",
        description="Operator manual retry for a failed durable job "
                    "(administrative surface; NEVER on the "
                    "unauthenticated public API)")
    parser.add_argument("job_id", help="the durable job id")
    parser.add_argument("--reason", default="",
                        help="bounded operator reason recorded in "
                             "the append-only event")
    parser.add_argument("--override-max-attempts", action="store_true",
                        help="durably recorded override permitting a "
                             "retry past max_attempts")
    cli_plumbing.add_common_arguments(parser)
    args = parser.parse_args(argv)

    from retriva.ingestion_api.durable_jobs import jobs_service
    from retriva.jobs.errors import (
        JobNotFoundError,
        JobsError,
        OperatorRetryRefusedError,
        TenantContextMissingError,
    )

    try:
        tenant_id, _privileged, _scope = cli_plumbing.resolve_scope(args)
        service = jobs_service()
        job = service.operator_retry(
            tenant_id=tenant_id, job_id=args.job_id,
            reason=(args.reason or "")[:200],
            override_max_attempts=bool(args.override_max_attempts))
        report = {
            "applied": True,
            "scope": tenant_id,
            "counts": {"operator_retry": 1},
            "items": [{"job_id": job.id, "classification": "T21",
                       "action": "operator_retry",
                       "note": job.status.value}],
            "manual_review_pending": 0,
        }
        return cli_plumbing.summarize("jobs.retry", report)
    except OperatorRetryRefusedError as exc:
        print(f"REFUSED: {exc}")
        return cli_plumbing.EXIT_FAILURE
    except (JobNotFoundError, JobsError,
            TenantContextMissingError) as exc:
        print(f"ERROR [{exc.__class__.__name__}]: {exc}")
        return cli_plumbing.EXIT_FAILURE
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"ERROR [{exc.__class__.__name__}]: {exc}")
        return cli_plumbing.EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
