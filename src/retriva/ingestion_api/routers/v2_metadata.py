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
v2 metadata endpoints.
"""

from fastapi import APIRouter, HTTPException, Query, status
from retriva.indexing.qdrant_store import get_client, get_metadata_schema, get_metadata_values
from retriva.ingestion_api.schemas_v2 import MetadataSchemaResponse, MetadataValuesResponse
from retriva.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v2/metadata", tags=["v2-metadata"])


@router.get("/schema", response_model=MetadataSchemaResponse)
async def get_schema():
    """Get all unique metadata keys present across all documents."""
    try:
        client = get_client()
        keys = get_metadata_schema(client)
        return MetadataSchemaResponse(keys=keys)
    except Exception as e:
        logger.error(f"Error getting metadata schema: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )


@router.get("/values", response_model=MetadataValuesResponse)
async def get_values(key: str = Query(..., description="The metadata key to query values for")):
    """Get all unique values for a specific metadata key across all documents."""
    try:
        client = get_client()
        values = get_metadata_values(client, key)
        return MetadataValuesResponse(key=key, values=values)
    except Exception as e:
        logger.error(f"Error getting metadata values for {key}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )
