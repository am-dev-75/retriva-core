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

"""Server-side job-type registry (Spec 025 §3.1/§3.13).

Persisted rows never invoke code by string lookup outside this
registry: a job type resolves to its handler descriptor only through
this server-side map (Constitution §34 posture; no client-controlled
code resolution).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from retriva.jobs.errors import JobsError


@dataclass(frozen=True)
class JobTypeSpec:
    """One registered job type."""

    job_type: str
    task_name: str
    restart_safe: bool = False
    max_attempts_default: int = 3
    queue: str = "ingestion"
    description: str = ""


class JobTypeRegistry:
    """Bounded registry of job types; unknown types fail clearly."""

    def __init__(self) -> None:
        self._types: Dict[str, JobTypeSpec] = {}

    def register(self, spec: JobTypeSpec) -> None:
        if not spec.job_type or " " in spec.job_type:
            raise JobsError(
                f"invalid job type {spec.job_type!r}")
        if spec.job_type in self._types:
            raise JobsError(
                f"job type {spec.job_type!r} is already registered")
        self._types[spec.job_type] = spec

    def get(self, job_type: str) -> Optional[JobTypeSpec]:
        return self._types.get(job_type)

    def require(self, job_type: str) -> JobTypeSpec:
        spec = self._types.get(job_type)
        if spec is None:
            raise JobsError(f"job type {job_type!r} is not registered")
        return spec

    def known_types(self) -> tuple:
        return tuple(sorted(self._types))


_registry: Optional[JobTypeRegistry] = None


def job_type_registry() -> JobTypeRegistry:
    """Process-wide registry with the Core-builtin job types."""
    global _registry
    if _registry is None:
        registry = JobTypeRegistry()
        registry.register(JobTypeSpec(
            job_type="v2_document",
            task_name="retriva.ingestion_api.tasks.process_document_task",
            restart_safe=True,
            description="v2 document ingestion pipeline",
        ))
        registry.register(JobTypeSpec(
            job_type="v2_mediawiki",
            task_name=(
                "retriva.ingestion_api.tasks.process_mediawiki_task"),
            restart_safe=True,
            description="v2 MediaWiki export ingestion pipeline",
        ))
        registry.register(JobTypeSpec(
            job_type="v2_upload",
            task_name="retriva.ingestion_api.tasks.process_document_task",
            restart_safe=False,
            description="v2 file upload ingestion pipeline",
        ))
        registry.register(JobTypeSpec(
            job_type="v2_artifact",
            task_name="retriva.ingestion_api.tasks.process_artifact_task",
            restart_safe=False,
            description=(
                "v2 artifact generation (document_list/basic_report "
                "renderers; basic_report may incur LLM provider cost "
                "— never replayed automatically; Spec 026)"),
        ))
        _registry = registry
    return _registry
