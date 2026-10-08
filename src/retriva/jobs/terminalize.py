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
# implied.  See the License for the specific language governing permissions
# and limitations under the License.

"""Private operator command (Spec 033 / ADR-038): terminalize ONE proven
no-effect ``dispatch_unknown`` generation.

Job-scoped only: no wildcard, batch, discovery, or all-tenants mode.
Dry-run is the default and prints the durable no-effect evidence plus a
stable fingerprint; ``--apply`` requires that exact fingerprint and uses
only the accepted legal transitions:

    attempt: QUEUED -> LOST
    job:     DISPATCH_UNKNOWN -> MANUAL_REVIEW -> FAILED
"""
from __future__ import annotations

import argparse
import json
from typing import Optional

from retriva.jobs import cli as cli_plumbing

REASON = "operator_fail_clean_pre_fix_ambiguous_generation_no_effects"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m retriva.jobs.terminalize",
        description="Terminalize one no-effect dispatch_unknown "
                    "generation (Spec 033; dry-run by default)")
    parser.add_argument("--tenant", type=str, required=True,
                        help="exact tenant scope (required)")
    parser.add_argument("--job-id", type=str, required=True,
                        help="exact durable job id")
    parser.add_argument("--attempt-id", type=str, required=True,
                        help="exact selected attempt id")
    parser.add_argument("--execution-generation", type=int,
                        required=True,
                        help="expected attempt execution_generation")
    parser.add_argument("--publication-state", type=str,
                        default="unknown",
                        help="expected publication state (default unknown)")
    parser.add_argument("--reason", type=str, required=True,
                        help=f"bounded reason (must be {REASON})")
    parser.add_argument("--actor", type=str, default="operator",
                        help="operator actor class (audit)")
    parser.add_argument("--evidence-fingerprint", type=str, default=None,
                        help="dry-run fingerprint (required with --apply)")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="(default) report evidence without writing")
    parser.add_argument("--apply", action="store_true",
                        help="apply the terminalization (requires the "
                             "dry-run fingerprint)")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = _parser().parse_args(argv)
    from retriva.infrastructure.postgres.tenant import validate_tenant_id
    from retriva.ingestion_api.durable_jobs import jobs_service

    try:
        tenant_id = validate_tenant_id(args.tenant)
        service = jobs_service()
        result = service.terminalize_no_effect_dispatch_unknown_generation(
            tenant_id=tenant_id, job_id=args.job_id,
            attempt_id=args.attempt_id,
            expected_execution_generation=args.execution_generation,
            expected_publication_state=args.publication_state,
            reason=args.reason, actor=args.actor,
            evidence_fingerprint=args.evidence_fingerprint,
            dry_run=not args.apply)
        print(json.dumps(result, indent=2, default=str))
        return cli_plumbing.EXIT_OK if not args.apply \
            else cli_plumbing.EXIT_APPLIED
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"ERROR [{exc.__class__.__name__}]: {exc}")
        return cli_plumbing.EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
