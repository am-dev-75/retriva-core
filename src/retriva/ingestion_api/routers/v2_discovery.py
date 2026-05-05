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
from retriva.config import VERSION

router = APIRouter(prefix="/api/v2", tags=["v2-discovery"])

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

@router.get("/capabilities", summary="Global v2 capabilities")
async def get_v2_capabilities():
    """Returns a unified list of all v2 capabilities across documents and artifacts."""
    return {
        "ingestion": ["documents", "upload"],
        "jobs": ["stage_tracking"],
        "artifacts": ["pdf", "markdown", "docx", "xlsx", "odt", "ods", "odp"],
        "artifact_types": ["document_list", "basic_report"]
    }
