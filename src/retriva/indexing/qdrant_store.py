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


def list_documents(client: QdrantClient, metadata_filter: Optional[Dict[str, str]] = None, doc_id: Optional[str] = None) -> List[dict]:
    """
    List unique documents from Qdrant matching the given metadata filter and/or doc_id.
    Deduplicates by doc_id in memory since Qdrant does not natively support 
    document-level distinct queries without vectors.
    """
    logger.debug(f"Listing documents in '{COLLECTION_NAME}'...")
    
    must_conditions = []
    if doc_id:
        must_conditions.append(
            FieldCondition(
                key="doc_id",
                match=MatchValue(value=doc_id),
            )
        )
    if metadata_filter:
        must_conditions.extend([
            FieldCondition(
                key=f"user_metadata.{k}",
                match=MatchValue(value=v),
            )
            for k, v in metadata_filter.items()
        ])
        
    scroll_filter = Filter(must=must_conditions) if must_conditions else None
    
    unique_docs = {}
    next_offset = None
    
    while True:
        points, next_offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=scroll_filter,
            limit=1000,
            offset=next_offset,
            with_payload=["doc_id", "source_path", "page_title", "user_metadata"],
            with_vectors=False
        )
        
        for point in points:
            d_id = point.payload.get("doc_id")
            if d_id and d_id not in unique_docs:
                unique_docs[d_id] = {
                    "doc_id": d_id,
                    "source_path": point.payload.get("source_path", ""),
                    "page_title": point.payload.get("page_title", ""),
                    "user_metadata": point.payload.get("user_metadata", None),
                }
                
        if next_offset is None:
            break
            
    return list(unique_docs.values())


def count_documents(client: QdrantClient, metadata_filter: Optional[Dict[str, str]] = None) -> int:
    """
    Count unique documents in Qdrant matching the given metadata filter.
    """
    logger.debug(f"Counting documents in '{COLLECTION_NAME}'...")
    return len(list_documents(client, metadata_filter))


def get_metadata_schema(client: QdrantClient) -> List[str]:
    """
    Get all unique metadata keys present across all documents.
    """
    logger.debug(f"Getting metadata schema in '{COLLECTION_NAME}'...")
    docs = list_documents(client)
    keys = set()
    for doc in docs:
        if doc.get("user_metadata"):
            keys.update(doc["user_metadata"].keys())
    return sorted(list(keys))


def get_metadata_values(client: QdrantClient, key: str) -> List[str]:
    """
    Get all unique values for a specific metadata key across all documents.
    """
    logger.debug(f"Getting metadata values for key '{key}' in '{COLLECTION_NAME}'...")
    # Only fetch documents that have this key
    metadata_filter = {key: ""}  # We can't filter purely by key existence easily without a value in Qdrant's basic API
    # Actually, we can just fetch all documents and filter in memory since list_documents caches/scrolls efficiently
    docs = list_documents(client)
    values = set()
    for doc in docs:
        if doc.get("user_metadata") and key in doc["user_metadata"]:
            val = doc["user_metadata"][key]
            if isinstance(val, str):
                values.add(val)
    return sorted(list(values))

