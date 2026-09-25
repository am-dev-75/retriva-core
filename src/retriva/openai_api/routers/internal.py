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

from fastapi import APIRouter, HTTPException, status
from retriva.config import settings
from retriva.profiler import get_recent_logs

router = APIRouter(prefix="/internal/profiler", tags=["internal"])

@router.get("/log")
async def get_profiler_logs():
    """
    Expose recent structured profiler logs.
    Available only when ENABLE_INTERNAL_PROFILER=true.
    """
    if not settings.enable_internal_profiler:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profiler is disabled."
        )

    return get_recent_logs()


# ---------------------------------------------------------------------------
# Reranking status (global settings + provider health + metrics).
# Read-only observability surface; secret values are never included
# (RerankProviderConfig.public_dict() reports only api_key_set).
# ---------------------------------------------------------------------------

reranker_router = APIRouter(prefix="/internal/reranker", tags=["internal"])

@reranker_router.get("/status")
async def get_reranker_status():
    """
    Expose the effective global reranking configuration (secrets redacted),
    provider health and in-process metrics.
    """
    from retriva.qa.reranking import get_reranker_status as _status

    return _status()