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

from fastapi import APIRouter, status, Response
from retriva.indexing.qdrant_store import get_client, delete_chunks_by_doc_id, delete_chunks_by_metadata, COLLECTION_NAME
from qdrant_client.models import Filter, FieldCondition, MatchValue
from retriva.logger import get_logger
from retriva.ingestion_api.schemas import DeleteMetadataRequest

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


@router.delete("/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(doc_id: str):
    """
    Delete a document and all its chunks from the vector store.
    
    This endpoint is idempotent. If the document does not exist, it logs 
    an informative message and returns 204 No Content.
    """
    logger.debug(f"Received request to delete document: {doc_id}")
    client = get_client()
    
    try:
        # Check if any chunks exist for this doc_id
        # We use scroll with limit 1 as a lightweight existence check
        hits, _ = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(
                must=[
                    FieldCondition(
                        key="doc_id",
                        match=MatchValue(value=doc_id),
                    )
                ]
            ),
            limit=1,
            with_payload=False,
            with_vectors=False
        )
        
        if not hits:
            logger.info(f"document not present; skipping doc_id={doc_id}")
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        delete_chunks_by_doc_id(client, doc_id)
        logger.info(f"retriva_deleted doc_id={doc_id}")
        return Response(status_code=status.HTTP_204_NO_CONTENT)
        
    except Exception as e:
        logger.error(f"Error during document deletion for {doc_id}: {e}")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

@router.delete("/metadata/filter", status_code=status.HTTP_204_NO_CONTENT)
async def delete_documents_by_metadata(request: DeleteMetadataRequest):
    """
    Delete all chunks from the vector store that match the given user_metadata filter.
    """
    logger.debug(f"Received request to delete chunks by metadata: {request.user_metadata_filter}")
    client = get_client()
    
    try:
        delete_chunks_by_metadata(client, request.user_metadata_filter)
        logger.info(f"retriva_deleted chunks by metadata: {request.user_metadata_filter}")
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except Exception as e:
        logger.error(f"Error during chunk deletion by metadata {request.user_metadata_filter}: {e}")
        return Response(status_code=status.HTTP_204_NO_CONTENT)
