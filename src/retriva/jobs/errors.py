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

"""Errors for the durable job subsystem.

Error messages are actionable and bounded; they never contain
credentials, connection URLs, raw payload content, or unsanitized
exception text (Constitution §33/§34).
"""

from __future__ import annotations


class JobsError(RuntimeError):
    """Base error for the durable job subsystem."""


class InvalidTransitionError(JobsError):
    """A guarded transition was refused: the durable state did not
    match the transition's predicate (duplicate/late callbacks are
    normal idempotent no-ops, not errors — the repository signals them
    through result objects; this error surfaces actual contradictions
    to callers)."""


class JobNotFoundError(JobsError):
    """The referenced job does not exist (or is not visible to the
    tenant context)."""


class IdempotencyConflictError(JobsError):
    """A submission reused an idempotency key with a different input
    identity; the caller receives a clear 409, never a silent
    mismatch."""


class TenantContextMissingError(JobsError):
    """No trusted server-side tenant context was established; every
    repository/service access fails closed (Constitution §32)."""


class OperatorRetryRefusedError(JobsError):
    """An operator retry was refused (non-retryable state, attempts
    exhausted without a durably recorded override, or an unresolvable
    job state)."""


class ReconciliationRefusedError(JobsError):
    """A reconciliation action could not be applied safely and was
    surfaced for manual review instead."""
