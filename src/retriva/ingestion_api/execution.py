# Copyright (C) 2026 Andrea Marson (am.dev.75@gmail.com)
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

"""Shared execution symbols relocated from the retired legacy
``JobManager`` module (Spec 027 / ADR-032).

``CancellationError`` is raised at cooperative cancellation
checkpoints by the shared ingestion internals (chunking, embedding,
upsert, parsing) and caught by durable job handlers.

``JobStatus`` / ``TERMINAL_STATES`` are the legacy projection enum
values retained ONLY for interface compatibility with the durable
recorder projection (Spec 025) and the v2 upload finalization
checks. They are NOT an independent state machine: the durable Core
jobs subsystem (``retriva.jobs``) owns the authoritative lifecycle.
"""

from enum import Enum


class CancellationError(Exception):
    """Raised when a cancellation checkpoint detects a pending cancel request."""


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


TERMINAL_STATES = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
