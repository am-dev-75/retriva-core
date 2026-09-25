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
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Thread-safe reranker health tracking.

Complements ``retriva.profiler`` (per-request phase timings) with a
long-lived service-level view: is the configured reranker provider
currently working, degraded, or failing? Surfaced via the
``/internal/reranker/status`` endpoint.

Security posture of the health data it produces (see the status endpoint
audit): messages are sanitized and bounded (single last error, capped
length, no stack traces, no provider exception objects), errors carry a
stable ``category`` (never a raw provider message), and no query or
document text is ever recorded.
"""

import re
import threading
from datetime import datetime, timezone
from typing import Optional

#: Hard cap for any stored message — bounds the status payload.
_MAX_MESSAGE_LEN = 200

_CTRL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


def sanitize_message(message: str, max_len: int = _MAX_MESSAGE_LEN) -> str:
    """Collapse control characters, strip, and bound a message's length."""
    if not isinstance(message, str):
        message = str(message)
    message = _CTRL_CHARS.sub(" ", message)
    message = re.sub(r"\s+", " ", message).strip()
    if len(message) > max_len:
        message = message[: max_len - 3] + "..."
    return message


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class RerankerHealth:
    """Aggregated health state of the globally configured reranker."""

    def __init__(self):
        self._lock = threading.Lock()
        self._reset()

    def _reset(self) -> None:
        self.status = "unknown"  # ok | degraded | error | disabled | unknown
        self.provider: Optional[str] = None
        self.enabled: Optional[bool] = None
        self.last_success_at: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_error_category: Optional[str] = None
        self.last_error_at: Optional[str] = None
        self.last_fallback_reason: Optional[str] = None
        self.consecutive_failures: int = 0
        self.total_successes: int = 0
        self.total_failures: int = 0

    # -- lifecycle ---------------------------------------------------------

    def configure(self, enabled: bool, provider: Optional[str]) -> None:
        """Record static configuration (startup validation / reload)."""
        with self._lock:
            self.enabled = enabled
            self.provider = provider
            if not enabled:
                self.status = "disabled"

    def record_config_issue(self, reason: str) -> None:
        """Record a sanitized configuration problem (degraded status)."""
        with self._lock:
            if self.status not in ("error",):
                self.status = "degraded"
            self.last_fallback_reason = sanitize_message(
                f"config: {reason}", max_len=_MAX_MESSAGE_LEN
            )

    def record_success(self, provider: str) -> None:
        with self._lock:
            self.status = "ok"
            self.provider = provider
            self.last_success_at = _utcnow_iso()
            self.consecutive_failures = 0
            self.total_successes += 1
            self.last_fallback_reason = None

    def record_failure(
        self,
        provider: Optional[str],
        error: str,
        category: Optional[str] = None,
    ) -> None:
        with self._lock:
            self.status = "error"
            if provider:
                self.provider = provider
            # Sanitized + bounded: no raw provider payloads, no stack
            # traces, no unbounded history (only the single last error is
            # retained).
            self.last_error = sanitize_message(error)
            self.last_error_category = category
            self.last_error_at = _utcnow_iso()
            self.consecutive_failures += 1
            self.total_failures += 1

    def record_fallback(self, reason: str) -> None:
        """Provider responded but results were unusable → degraded."""
        with self._lock:
            if self.status != "error":
                self.status = "degraded"
            self.last_fallback_reason = sanitize_message(reason)

    # -- reporting ---------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "enabled": self.enabled,
                "provider": self.provider,
                "last_success_at": self.last_success_at,
                "last_error": self.last_error,
                "last_error_category": self.last_error_category,
                "last_error_at": self.last_error_at,
                "last_fallback_reason": self.last_fallback_reason,
                "consecutive_failures": self.consecutive_failures,
                "total_successes": self.total_successes,
                "total_failures": self.total_failures,
            }

    # -- testing -----------------------------------------------------------

    def reset(self) -> None:
        """Reset all state — for testing only."""
        with self._lock:
            self._reset()


# Module-level singleton (same convention as retriva.profiler's deque).
reranker_health = RerankerHealth()