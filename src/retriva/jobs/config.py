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

"""Durable-jobs settings (Spec 025 §3.9, §3.12, §3.8).

Environment variables (``RETRIVA_JOBS_*``) are read at startup and
validated by :class:`TenantResolver` / the service setup; no setting
is read per-request except through the resolver.

Development retention defaults (owner-approved, revision 2):
succeeded 30 days, failed 90 days, cancelled 90 days.  Retention
configuration is applied when a job becomes terminal (``purge_after``
snapshotted at the terminal transition); later configuration changes
do NOT silently rewrite existing values.
"""

from __future__ import annotations

import os

from retriva.jobs.errors import JobsError


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise JobsError(f"{name} must be an integer") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise JobsError(f"{name} must be a number") from exc


class JobsSettings:
    """Runtime settings for the durable job subsystem."""

    def __init__(self) -> None:
        self.default_tenant: str = (
            os.environ.get("RETRIVA_JOBS_DEFAULT_TENANT", "").strip())
        self.tenant_resolver: str = (
            os.environ.get("RETRIVA_JOBS_TENANT_RESOLVER", "")
            .strip().lower() or "fixed")
        self.tenant_header_override: bool = (
            os.environ.get("RETRIVA_JOBS_TENANT_HEADER_OVERRIDE", "")
            .strip().lower() in ("1", "true", "yes", "on"))
        self.tenant_header_name: str = (
            os.environ.get("RETRIVA_JOBS_TENANT_HEADER_NAME", "")
            .strip() or "x-retriva-tenant")
        self.retention_succeeded_days: int = _env_int(
            "RETRIVA_JOBS_RETENTION_SUCCEEDED_DAYS", 30)
        self.retention_failed_days: int = _env_int(
            "RETRIVA_JOBS_RETENTION_FAILED_DAYS", 90)
        self.retention_cancelled_days: int = _env_int(
            "RETRIVA_JOBS_RETENTION_CANCELLED_DAYS", 90)
        self.max_attempts_default: int = _env_int(
            "RETRIVA_JOBS_MAX_ATTEMPTS", 3)
        self.progress_throttle_seconds: float = _env_float(
            "RETRIVA_JOBS_PROGRESS_THROTTLE_SECONDS", 5.0)
        self.reconcile_stale_threshold_seconds: int = _env_int(
            "RETRIVA_JOBS_RECONCILE_STALE_THRESHOLD_SECONDS", 1800)
        self.reconcile_batch_size: int = _env_int(
            "RETRIVA_JOBS_RECONCILE_BATCH_SIZE", 200)
        self.cleanup_batch_size: int = _env_int(
            "RETRIVA_JOBS_CLEANUP_BATCH_SIZE", 500)
        self.publication_tries_max: int = _env_int(
            "RETRIVA_JOBS_PUBLICATION_TRIES_MAX", 5)

    def retention_days_for(self, status_value: str) -> int:
        """Retention days for a terminal status (owner-approved
        defaults; Spec 025 §3.9)."""
        if status_value == "succeeded":
            return self.retention_succeeded_days
        if status_value == "failed":
            return self.retention_failed_days
        if status_value == "cancelled":
            return self.retention_cancelled_days
        raise JobsError(
            f"no retention policy for non-terminal status "
            f"{status_value!r}")


_settings: JobsSettings | None = None


def jobs_settings() -> JobsSettings:
    global _settings
    if _settings is None:
        _settings = JobsSettings()
    return _settings


def reset_jobs_settings() -> None:
    """Test hook: re-read environment on next access."""
    global _settings
    _settings = None
