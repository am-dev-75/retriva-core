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

import time
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct, Filter, FieldCondition, MatchValue
from qdrant_client.http.exceptions import ResponseHandlingException
from retriva.config import settings
from retriva.domain.models import Chunk
from retriva.indexing.embeddings import get_embeddings
from retriva.logger import get_logger
from typing import Callable, List, Optional, Dict

logger = get_logger(__name__)

COLLECTION_NAME = settings.qdrant_collection_name
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # seconds

def get_client() -> QdrantClient:
    return QdrantClient(url=settings.qdrant_url)

def init_collection(client: QdrantClient, vector_size: int = None):
    if vector_size is None:
        vector_size = settings.embedding_dimension
        
    if not client.collection_exists(COLLECTION_NAME):
        logger.info(f"Creating collection '{COLLECTION_NAME}' with dimension {vector_size}...")
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )
    else:
        logger.debug(f"Collection '{COLLECTION_NAME}' already exists.")

def _upsert_with_retry(client: QdrantClient, points: List[PointStruct], batch_num: int):
    """Upsert points to Qdrant with retry logic."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            client.upsert(
                collection_name=COLLECTION_NAME,
                points=points
            )
            return
        except (ResponseHandlingException, Exception) as e:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            if attempt < MAX_RETRIES:
                logger.warning(
                    f"Upsert batch {batch_num} attempt {attempt}/{MAX_RETRIES} failed: {e}. "
                    f"Retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
            else:
                raise RuntimeError(
                    f"Upsert batch {batch_num} failed after {MAX_RETRIES} attempts: {e}"
                ) from e

def upsert_chunks(client: QdrantClient, chunks: List[Chunk], cancel_check: Optional[Callable[[], bool]] = None):
    if not chunks:
        return
        
    init_collection(client)
    logger.info(f"Indexing {len(chunks)} chunks in batches of {settings.indexing_batch_size}...")
    
    for i in range(0, len(chunks), settings.indexing_batch_size):
        # Cancellation checkpoint — check before each batch
        if cancel_check and cancel_check():
            from retriva.ingestion_api.job_manager import CancellationError
            raise CancellationError("Job cancelled during upsert")

        batch_chunks = chunks[i : i + settings.indexing_batch_size]
        batch_num = i // settings.indexing_batch_size + 1
        texts = [c.text for c in batch_chunks]
        embeddings = get_embeddings(texts, cancel_check=cancel_check)
        
        points = [
            PointStruct(
                id=c.metadata.chunk_id,
                vector=embedding,
                payload={
                    "text": c.text,
                    **c.metadata.model_dump()
                }
            )
            for c, embedding in zip(batch_chunks, embeddings)
        ]
        
        logger.debug(f"Upserting batch {batch_num} ({len(points)} points) to '{COLLECTION_NAME}'...")
        _upsert_with_retry(client, points, batch_num)


def search_chunks(client: QdrantClient, query_vector: List[float], retriever_top_k: int = 5, metadata_filter: Optional[Dict[str, str]] = None) -> List[dict]:
    logger.debug(f"Searching top_{retriever_top_k} chunks in '{COLLECTION_NAME}'...")
    
    query_filter = None
    if metadata_filter:
        must_conditions = [
            FieldCondition(
                key=f"user_metadata.{k}",
                match=MatchValue(value=v),
            )
            for k, v in metadata_filter.items()
        ]
        query_filter = Filter(must=must_conditions)

    results = client.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        query_filter=query_filter,
        limit=retriever_top_k
    )
    return [hit.payload for hit in results.points]


def delete_chunks_by_source_path(client: QdrantClient, source_path: str):
    """
    Delete all chunks (points) in Qdrant that belong to the given source_path.
    """
    logger.info(f"Deleting chunks for source_path: {source_path}")
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=Filter(
            must=[
                FieldCondition(
                    key="source_path",
                    match=MatchValue(value=source_path),
                )
            ]
        ),
    )


def delete_chunks_by_metadata(client: QdrantClient, metadata_filter: Dict[str, str]):
    """
    Delete all chunks (points) in Qdrant that match the given user_metadata filter.
    """
    logger.info(f"Deleting chunks for metadata_filter: {metadata_filter}")
    if not metadata_filter:
        return
        
    must_conditions = [
        FieldCondition(
            key=f"user_metadata.{k}",
            match=MatchValue(value=v),
        )
        for k, v in metadata_filter.items()
    ]
    
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=Filter(must=must_conditions),
    )
