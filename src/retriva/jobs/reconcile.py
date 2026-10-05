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

"""Manual reconciliation for the durable job lifecycle (Spec 025
§3.8; R1–R9).

Operator-invoked, dry-run by default, batch-bounded, idempotent;
every applied action is event-logged; uncertain external side effects
are NEVER automatically replayed — they are parked in
``manual_review`` and surfaced in the report for operator resolution.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from retriva.jobs.config import JobsSettings
from retriva.jobs.domain import (
    AttemptStatus,
    JobStatus,
)
from retriva.jobs.repository import (
    ClaimOutcome,
    PostgresJobsRepository,
)
from retriva.logger import get_logger

_log = get_logger(__name__)


def reconcile(service, *, tenant_id: Optional[str],
              privileged: bool, batch: int, apply: bool,
              stale_threshold_seconds: Optional[int] = None,
              settings: Optional[JobsSettings] = None,
) -> Dict[str, Any]:
    """Run one reconciliation sweep.

    Returns a bounded report:
      {applied, scope, counts: {classification: n},
       items: [{job_id, classification, action, note}],
       manual_review_pending: n}
    """
    settings = settings or JobsSettings()
    threshold = _now() - timedelta(
        seconds=(stale_threshold_seconds
                 if stale_threshold_seconds is not None
                 else settings.reconcile_stale_threshold_seconds))
    repo: PostgresJobsRepository = service.repo
    counts: Dict[str, int] = {}
    items: List[Dict[str, Any]] = []
    manual_review_pending = 0

    def note(job, classification: str, action: str,
             note_text: str = "") -> None:
        counts[classification] = counts.get(classification, 0) + 1
        items.append({
            "job_id": job.id,
            "classification": classification,
            "action": action,
            "note": note_text,
        })

    def log_action(job, classification: str, action: str,
                   detail: Optional[Dict[str, Any]] = None) -> None:
        if apply:
            repo.record_reconciliation(
                tenant_id=job.tenant_id, job_id=job.id,
                classification=classification, action=action,
                detail=detail)

    # -- R1: stale pending / stale dispatching / stale queued -------------
    for job in repo.stale_pending(tenant_id=tenant_id, threshold=threshold,
                                  batch=batch, privileged=privileged):
        if apply:
            attempt_id, token, task_id = _new_identity()
            attempt = repo.prepare_dispatch(
                tenant_id=job.tenant_id, job_id=job.id,
                dispatch_token=token, celery_task_id=task_id,
                attempt_id=attempt_id)
            if attempt is not None:
                _publish(service, job, attempt, tenant_id=job.tenant_id)
                log_action(job, "R1", "re_dispatched")
        note(job, "R1", "re_dispatch" if apply else "would_re_dispatch")
    for job in repo.stale_dispatching(
            tenant_id=tenant_id, threshold=threshold, batch=batch,
            privileged=privileged):
        if _cancel_intent(job):
            note(job, "R1", "cancel_intent_kept_for_cancelling_rules")
            continue
        if apply:
            attempts = repo.attempts_for_job(
                tenant_id=job.tenant_id, job_id=job.id,
                privileged=privileged)
            live = [a for a in attempts
                    if a.publication_state.value in
                    ("prepared", "publishing", "unknown")
                    and a.status == AttemptStatus.QUEUED]
            if live:
                attempt = live[0]
                if (attempt.publication_tries
                        < settings.publication_tries_max):
                    service.republish_dispatch(job, attempt)
                    log_action(job, "R1", "republished_same_generation")
                else:
                    note(job, "R1", "publication_tries_exhausted")
                    continue
        note(job, "R1", "republish" if apply else "would_republish")
    for job in repo.stale_queued(tenant_id=tenant_id, threshold=threshold,
                                 batch=batch, privileged=privileged):
        # R1: publication confirmed but never claimed (message lost /
        # worker restart): re-publish the SAME generation (bounded).
        if apply:
            attempts = repo.attempts_for_job(
                tenant_id=job.tenant_id, job_id=job.id,
                privileged=privileged)
            live = [a for a in attempts
                    if a.status == AttemptStatus.QUEUED
                    and a.publication_state.value == "published"]
            if live and live[0].publication_tries < \
                    settings.publication_tries_max:
                service.republish_dispatch(job, live[0])
                log_action(job, "R1", "republished_same_generation")
        note(job, "R1", "republish" if apply else "would_republish")

    # -- R2: dispatch_unknown (evidence resolution; never blind revert) --
    for job in repo.dispatch_unknown_jobs(tenant_id=tenant_id,
                                          batch=batch,
                                          privileged=privileged):
        if _cancel_intent(job):
            if apply:
                _resolve_cancel_intent(service, repo, job)
                log_action(job, "R2", "cancel_intent_resolved")
            note(job, "R2",
                 "cancel_intent_resolution"
                 if apply else "would_resolve_cancel_intent")
            continue
        if apply:
            attempts = repo.attempts_for_job(
                tenant_id=job.tenant_id, job_id=job.id,
                privileged=privileged)
            live = [a for a in attempts
                    if a.status == AttemptStatus.QUEUED
                    and a.publication_state.value == "unknown"]
            if live and live[0].publication_tries < \
                    settings.publication_tries_max:
                service.republish_dispatch(job, live[0])
                log_action(job, "R2", "republished_same_generation")
        note(job, "R2", "republish" if apply else "would_republish")

    # -- R3/R4/R5: cancelling states ---------------------------------------
    for job in repo.cancelling_jobs(tenant_id=tenant_id, batch=batch,
                                    privileged=privileged):
        attempts = repo.attempts_for_job(
            tenant_id=job.tenant_id, job_id=job.id,
            privileged=privileged)
        unclaimed = [a for a in attempts
                     if a.status == AttemptStatus.QUEUED]
        running = [a for a in attempts
                   if a.status == AttemptStatus.RUNNING]
        if not apply:
            classification = ("R3" if unclaimed
                              else "R4" if running else "R5")
            note(job, classification, "would_resolve")
            continue
        if unclaimed and repo.cancel_unclaimed_attempt(
                tenant_id=job.tenant_id, job_id=job.id,
                attempt_id=unclaimed[0].id):
            log_action(job, "R3", "cancelled_never_executed")
            note(job, "R3", "cancel_unclaimed")
        elif running:
            # No terminal evidence: NO false terminal claim, NO
            # automatic replay (cancel intent is authoritative).
            repo.mark_execution_lost(
                tenant_id=job.tenant_id, job_id=job.id,
                attempt_id=running[0].id,
                reason="cancelling_without_terminal_evidence",
                to_manual_review=True)
            manual_review_pending += 1
            log_action(job, "R4", "parked_for_manual_review")
            note(job, "R4", "parked_for_manual_review")
        else:
            manual_review_pending += 1
            log_action(job, "R5", "parked_for_manual_review")
            note(job, "R5", "parked_for_manual_review")

    # -- R6: due retry_wait ------------------------------------------------
    for job in repo.due_retry_wait(tenant_id=tenant_id, batch=batch,
                                   privileged=privileged):
        if apply:
            service.reschedule_due(job.id, job.tenant_id)
            log_action(job, "R6", "rescheduled")
        note(job, "R6", "reschedule" if apply else "would_reschedule")

    # -- R7: stale running (restart-safe gate; conservative default) -------
    for job in repo.stale_running(tenant_id=tenant_id,
                                  threshold=threshold, batch=batch,
                                  privileged=privileged):
        spec = _spec_for(service, job)
        restart_safe = bool(spec and spec.restart_safe)
        if apply:
            attempts = repo.attempts_for_job(
                tenant_id=job.tenant_id, job_id=job.id,
                privileged=privileged)
            running = [a for a in attempts
                       if a.status == AttemptStatus.RUNNING]
            attempt_id = running[0].id if running else None
            if attempt_id is None:
                continue
            if not restart_safe:
                # Artifact-specific evidence adapter (Spec 026 §17):
                # a finalized artifact with PROVEN provenance
                # (tenant/artifact/job/checksum/size) may be adopted
                # from the finalized→crash window; any uncertainty
                # stays on the conservative path (manual_review, no
                # automatic provider-cost replay).
                adopted = _adopt_from_finalization_evidence(
                    service, job, running[0])
                if adopted:
                    log_action(job, "R7",
                               "adopted_finalization_provenance")
                    note(job, "R7", "adopted_finalization_provenance")
                    continue
            repo.mark_execution_lost(
                tenant_id=job.tenant_id, job_id=job.id,
                attempt_id=attempt_id,
                reason="stale_running_no_callback",
                redispatch=restart_safe,
                to_manual_review=not restart_safe)
            if restart_safe:
                attempt_id2, token, task_id = _new_identity()
                attempt2 = repo.prepare_dispatch(
                    tenant_id=job.tenant_id, job_id=job.id,
                    dispatch_token=token, celery_task_id=task_id,
                    attempt_id=attempt_id2)
                if attempt2 is not None:
                    _publish(service, job, attempt2,
                             tenant_id=job.tenant_id)
            else:
                manual_review_pending += 1
            log_action(
                job, "R7",
                "redispatched_restart_safe" if restart_safe
                else "parked_for_manual_review")
        note(job, "R7",
             ("redispatch" if restart_safe else "manual_review")
             if apply else "would_classify",
             note_text="restart_safe" if restart_safe else "")

    # -- R8/R9: divergence (attempt terminal, job not) -----------------------
    for job in repo.divergent_jobs(tenant_id=tenant_id, batch=batch,
                                   privileged=privileged):
        if apply:
            _resolve_divergence(service, repo, job)
            log_action(job, "R9", "divergence_resolved")
        note(job, "R9", "resolve_divergence"
             if apply else "would_resolve_divergence")

    return {
        "applied": bool(apply),
        "scope": tenant_id or "all_tenants",
        "counts": counts,
        "items": items,
        "manual_review_pending": manual_review_pending,
    }


def _manual_review_marker(job) -> bool:
    return job.status == JobStatus.MANUAL_REVIEW


def _cancel_intent(job) -> bool:
    return job.cancel_requested_at is not None or \
        job.status == JobStatus.CANCELLING


def _publish(service, job, attempt, tenant_id: str) -> None:
    try:
        service.republish_dispatch(job, attempt)
    except Exception as exc:  # noqa: BLE001 - classified per action
        _log.warning(
            "reconcile publication failed (classified by the "
            "publication-state model): job=%s exception=%s",
            job.id, exc.__class__.__name__)


def _resolve_cancel_intent(service, repo, job) -> None:
    """Cancel-intent resolution for a dispatch_unknown job: the
    dispatch generation is invalidated (never re-published under
    cancel intent); never-executed evidence → cancelled; execution
    that cannot be excluded → manual_review."""
    attempts = repo.attempts_for_job(
        tenant_id=job.tenant_id, job_id=job.id, privileged=True)
    unclaimed = [a for a in attempts
                 if a.status == AttemptStatus.QUEUED]
    if unclaimed and repo.cancel_unclaimed_attempt(
            tenant_id=job.tenant_id, job_id=job.id,
            attempt_id=unclaimed[0].id,
            evidence="cancel_intent_never_executed"):
        return
    running = [a for a in attempts
               if a.status == AttemptStatus.RUNNING]
    if running:
        repo.mark_execution_lost(
            tenant_id=job.tenant_id, job_id=job.id,
            attempt_id=running[0].id,
            reason="cancel_intent_with_ambiguous_execution",
            to_manual_review=True)
        return
    # No claimable attempt: job may be parked directly for review
    # (bounded classification).
    job = repo.get_job_unscoped(job_id=job.id, privileged=True)
    if job is not None and job.status == JobStatus.DISPATCH_UNKNOWN:
        repo.record_anomaly(
            tenant_id=job.tenant_id, job_id=job.id,
            detail={"reason": "cancel_intent_no_attempt_evidence"})


def _resolve_divergence(service, repo, job) -> None:
    """R9: adopt the durable attempt outcome as the job outcome
    (evidence-based, never a blind guess).

    The CONSERVATIVE job-side state wins (spec §3.9 R9): a job the
    operator (or R7/R8) parked in ``manual_review`` stays there — a
    lost attempt has no definitive outcome to adopt and un-parking
    would re-enter the re-dispatch path that restart-safety forbids
    for non-restart-safe types."""
    attempts = repo.attempts_for_job(
        tenant_id=job.tenant_id, job_id=job.id, privileged=True)
    if not attempts:
        return
    if job.status == JobStatus.MANUAL_REVIEW:
        return  # conservative operator-owned state; not adoptable
    latest = attempts[-1]
    outcome = latest.status
    resolution = {
        AttemptStatus.SUCCEEDED: "succeeded",
        AttemptStatus.FAILED: "failed",
    }.get(outcome)
    if resolution:
        repo.adopt_divergence_outcome(
            tenant_id=job.tenant_id, job_id=job.id,
            resolution=resolution,
            reason="attempt_outcome_adopted")
        return
    # lost / cancelled / dispatch_failed: cancel intent → cancelled;
    # otherwise restart-safety gates the re-dispatch entry (R1):
    # restart-safe types return to pending; others park for the
    # operator (never an automatic re-execution of unproven work).
    if _cancel_intent(job):
        repo.adopt_divergence_outcome(
            tenant_id=job.tenant_id, job_id=job.id,
            resolution="cancelled",
            reason="divergence_with_cancel_intent")
    elif _spec_for(service, job) and _spec_for(
            service, job).restart_safe:
        repo.adopt_divergence_outcome(
            tenant_id=job.tenant_id, job_id=job.id,
            resolution="pending",
            reason="divergence_attempt_lost_restart_safe")
    else:
        repo.adopt_divergence_outcome(
            tenant_id=job.tenant_id, job_id=job.id,
            resolution="manual_review",
            reason="divergence_attempt_lost_not_restart_safe")


def _spec_for(service, job):
    try:
        return service.require_spec(job.job_type)
    except Exception:  # noqa: BLE001 - unknown type: conservative
        return None


def _adopt_from_finalization_evidence(service, job, attempt) -> bool:
    """Spec 026 §17: adopt a finalized artifact whose provenance
    sidecar proves it belongs to this tenant/artifact/job and whose
    checksum/size match.  Anything uncertain returns False (the
    conservative manual_review path follows); provider work is never
    replayed.  Lazy import: only the artifact workflow provides this
    evidence adapter today."""
    try:
        from retriva.ingestion_api.artifact_store import (
            adopt_finalized_artifact,
        )
        return adopt_finalized_artifact(service, job, attempt)
    except Exception:  # noqa: BLE001 - evidence must be provable
        return False


def _new_identity() -> tuple:
    from retriva.jobs.dispatch import new_dispatch_identity
    return new_dispatch_identity()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# CLI (python -m retriva.jobs.reconcile)
# ---------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    from retriva.jobs import cli as cli_plumbing

    parser = argparse.ArgumentParser(
        prog="python -m retriva.jobs.reconcile",
        description="Manual reconciliation for the durable job "
                    "lifecycle (operator command; dry-run by default)")
    cli_plumbing.add_common_arguments(parser)
    args = parser.parse_args(argv)

    from retriva.ingestion_api.durable_jobs import jobs_service
    from retriva.jobs.config import JobsSettings

    try:
        tenant_id, privileged, _scope = cli_plumbing.resolve_scope(args)
        settings = JobsSettings()
        batch = cli_plumbing.resolve_batch(
            args, settings.reconcile_batch_size)
        report = reconcile(
            jobs_service(), tenant_id=tenant_id,
            privileged=privileged, batch=batch, apply=args.apply,
            settings=settings)
        return cli_plumbing.summarize("jobs.reconcile", report)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"ERROR [{exc.__class__.__name__}]: {exc}")
        return cli_plumbing.EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
