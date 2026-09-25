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
Thread-safe in-process reranking metrics.

Counters are exposed through ``/internal/reranker/status``. Retriva has no
external metrics backend dependency, so this follows the same in-memory
convention as ``retriva.profiler.get_recent_logs()``.
"""

import threading
import time
from collections import deque
from typing import Deque, Dict, Optional


class RerankerMetrics:
    """Counters and latency samples for the globally configured reranker."""

    _MAX_LATENCY_SAMPLES = 256

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    # -- recording ---------------------------------------------------------

    def inc_call(self, provider: str) -> None:
        with self._lock:
            self.counters["calls_total"] += 1
            self.per_provider.setdefault(provider, self._empty_provider())[  # noqa: E501
                "calls_total"
            ] += 1

    def observe_latency(self, duration_ms: float) -> None:
        with self._lock:
            self.latencies_ms.append((time.time(), duration_ms))

    def inc_success(self, provider: Optional[str] = None) -> None:
        with self._lock:
            self.counters["success_total"] += 1
            self._bump_provider(provider, "success_total")

    def inc_failure(self, provider: Optional[str] = None) -> None:
        with self._lock:
            self.counters["failure_total"] += 1
            self._bump_provider(provider, "failure_total")

    def inc_fallback(self) -> None:
        with self._lock:
            self.counters["fallback_total"] += 1

    def add_documents(self, count: int) -> None:
        with self._lock:
            self.counters["documents_scored_total"] += max(0, count)

    def _bump_provider(self, provider: Optional[str], counter: str) -> None:
        """Attribute a global counter to a provider (best effort, in-lock).

        Attribution is explicit: callers pass the provider name of the
        attempt being recorded, so a failed provider *build* (where no
        name is known) is never misattributed to a previous provider.
        """
        if provider:
            self.per_provider.setdefault(provider, self._empty_provider())[
                counter
            ] += 1

    # -- reporting ---------------------------------------------------------

    def snapshot(self) -> Dict:
        with self._lock:
            latencies = [ms for _, ms in self.latencies_ms]
            return {
                "counters": dict(self.counters),
                "per_provider": {p: dict(v) for p, v in self.per_provider.items()},
                "latency_ms": {
                    "last": round(latencies[-1], 2) if latencies else None,
                    "avg": (
                        round(sum(latencies) / len(latencies), 2)
                        if latencies
                        else None
                    ),
                    "max": round(max(latencies), 2) if latencies else None,
                    "samples": len(latencies),
                },
            }

    # -- testing -----------------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            self.counters: Dict[str, int] = {
                "calls_total": 0,
                "success_total": 0,
                "failure_total": 0,
                "fallback_total": 0,
                "documents_scored_total": 0,
            }
            self.per_provider: Dict[str, Dict[str, int]] = {}
            self.latencies_ms: Deque = deque(maxlen=self._MAX_LATENCY_SAMPLES)

    @staticmethod
    def _empty_provider() -> Dict[str, int]:
        return {
            "calls_total": 0,
            "success_total": 0,
            "failure_total": 0,
        }


# Module-level singleton.
reranker_metrics = RerankerMetrics()
