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

from fastapi import APIRouter
from pydantic import BaseModel, model_validator
from retriva.config import VERSION

router = APIRouter(prefix="/api/v2", tags=["v2-discovery"])


class KnowledgeMetadataReadiness(BaseModel):
    """Spec 028 additive public readiness object (bounded)."""

    state: str
    authoritative: bool
    native_ingestion_available: bool

    @model_validator(mode="after")
    def _validate(self):
        if self.authoritative != (self.state == "authoritative"):
            raise ValueError(
                "authoritative must be true iff state == 'authoritative'")
        if self.native_ingestion_available and not self.authoritative:
            raise ValueError(
                "native_ingestion_available requires authoritative state")
        return self


class V2Capabilities(BaseModel):
    ingestion: list[str]
    jobs: list[str]
    artifacts: list[str]
    artifact_types: list[str]
    knowledge_metadata: KnowledgeMetadataReadiness


@router.get("", summary="v2 discovery endpoint")
async def get_v2_info():
    """Returns version and capability information for the Retriva Core API v2."""
    return {
        "version": VERSION,
        "api_version": "v2",
        "status": "active",
        "features": ["documents", "jobs", "artifacts"],
        "message": "Retriva Core API v2 is active."
    }

@router.get("/capabilities", summary="Global v2 capabilities",
            response_model=V2Capabilities)
async def get_v2_capabilities() -> V2Capabilities:
    """Returns a unified list of all v2 capabilities across documents and artifacts."""
    return V2Capabilities(
        ingestion=["documents", "upload"],
        jobs=["stage_tracking"],
        artifacts=["pdf", "markdown", "docx", "xlsx", "odt", "ods", "odp"],
        artifact_types=["document_list", "basic_report"],
        knowledge_metadata=KnowledgeMetadataReadiness(
            **_knowledge_readiness()),
    )


def _knowledge_readiness() -> dict:
    """Additive public knowledge-metadata readiness object (Spec 028
    §11/§25).  Bounded fields only: state, authoritative,
    native_ingestion_available.  Never exposes counts, conflicts,
    tenant IDs, source references, or adoption reports.  Fail-closed:
    when the knowledge schema is unavailable the subsystem reports
    ``schema_ready`` with no native ingestion."""
    default = {
        "state": "schema_ready",
        "authoritative": False,
        "native_ingestion_available": False,
    }
    try:
        from retriva.infrastructure.postgres.config import (
            get_platform_settings,
        )
        from retriva.knowledge.authority import KnowledgeAuthority
        from retriva.knowledge.repository import (
            KnowledgeRepository,
            KnowledgeRepositoryError,
        )

        authority = KnowledgeAuthority(
            KnowledgeRepository(get_platform_settings()))
        return authority.public_readiness()
    except Exception:
        return default
