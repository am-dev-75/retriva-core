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
from retriva.openai_api.schemas import ModelInfo, ListModelsResponse

router = APIRouter(tags=["models"])

# Fixed model entries — Retriva exposes itself as a single unified model.
_RETRIVA_MODEL = ModelInfo(id="retriva", owned_by="retriva")
_RETRIVA_V2_MODEL = ModelInfo(id="retriva-v2", owned_by="retriva")


@router.get("/v1/models", response_model=ListModelsResponse)
async def list_models():
    """
    Returns the list of available models.

    Open WebUI calls this on connection to discover which models are
    available.  Retriva always returns a single model: ``retriva``.
    """
    return ListModelsResponse(data=[_RETRIVA_MODEL, _RETRIVA_V2_MODEL])