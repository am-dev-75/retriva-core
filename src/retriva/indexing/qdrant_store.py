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
from typing import Callable, List, Optional, Dict, Any, Union
from qdrant_client.models import (
    VectorParams, Distance, PointStruct, Filter, FieldCondition, 
    MatchValue, MatchExcept, MatchAny, IsEmptyCondition, 
    HasIdCondition, MatchText, Prefetch, QueryRequest
)
from qdrant_client.http.exceptions import ResponseHandlingException
from retriva.config import settings
from retriva.domain.models import Chunk
from retriva.indexing.embeddings import get_embeddings
from retriva.logger import get_logger
from retriva.profiler import Profiler

logger = get_logger(__name__)

COLLECTION_NAME = settings.qdrant_collection_name
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # seconds

def _get_req_id():
    p = Profiler.get_current()
    return p.request_id if p else "no-req"

def get_client() -> QdrantClient:
    return QdrantClient(url=settings.qdrant_url)

def init_collection(client: QdrantClient, vector_size: int = None):
    if vector_size is None:
        vector_size = settings.embedding_dimension
        
    if not client.collection_exists(COLLECTION_NAME):
        logger.info(f"[{_get_req_id()}] Creating collection '{COLLECTION_NAME}' with dimension {vector_size}...")
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )
    else:
        logger.debug(f"[{_get_req_id()}] Collection '{COLLECTION_NAME}' already exists.")

def _upsert_with_retry(client: QdrantClient, points: List[PointStruct], batch_num: int):
    """Upsert points to Qdrant with retry logic."""
    rid = _get_req_id()
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
                    f"[{rid}] Upsert batch {batch_num} attempt {attempt}/{MAX_RETRIES} failed: {e}. "
                    f"Retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
            else:
                raise RuntimeError(
                    f"[{rid}] Upsert batch {batch_num} failed after {MAX_RETRIES} attempts: {e}"
                ) from e

def upsert_chunks(client: QdrantClient, chunks: List[Chunk], cancel_check: Optional[Callable[[], bool]] = None):
    if not chunks:
        return
        
    init_collection(client)
    rid = _get_req_id()
    logger.info(f"[{rid}] Indexing {len(chunks)} chunks in batches of {settings.indexing_batch_size}...")
    
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
                    **c.metadata.model_dump(exclude={"content_hash", "content_hash_algorithm", "source_paths"}),
                    # Dedup fields — present on new v2 ingestions, None on legacy chunks
                    "content_hash": c.metadata.content_hash,
                    "content_hash_algorithm": c.metadata.content_hash_algorithm,
                    # source_paths is the authoritative multi-path field
                    "source_paths": c.metadata.source_paths or [c.metadata.source_path],
                    # source_path kept for backward compatibility
                    "source_path": (
                        c.metadata.source_paths[0]
                        if c.metadata.source_paths
                        else c.metadata.source_path
                    ),
                }
            )
            for c, embedding in zip(batch_chunks, embeddings)
        ]
        
        logger.debug(f"[{rid}] Upserting batch {batch_num} ({len(points)} points) to '{COLLECTION_NAME}'...")
        _upsert_with_retry(client, points, batch_num)


def build_qdrant_filter(filters: List[Dict[str, Any]]) -> Optional[Filter]:
    """Build a Qdrant Filter from a list of metadata filter dicts."""
    if not filters:
        return None
        
    must_conditions = []
    for f in filters:
        field = f.get("field")
        op = f.get("operator", "eq")
        val = f.get("value")
        
        if op == "eq":
            must_conditions.append(FieldCondition(key=field, match=MatchValue(value=val)))
        elif op == "exists":
            must_conditions.append(Filter(must_not=[IsEmptyCondition(key=field)]))
        elif op == "neq":
            must_conditions.append(Filter(must_not=[FieldCondition(key=field, match=MatchValue(value=val))]))
        elif op == "contains":
            must_conditions.append(FieldCondition(key=field, match=MatchText(text=str(val))))
        elif op == "in":
            if isinstance(val, list):
                must_conditions.append(FieldCondition(key=field, match=MatchAny(any=val)))
            elif isinstance(val, str):
                must_conditions.append(FieldCondition(key=field, match=MatchAny(any=[val])))
                
    return Filter(must=must_conditions) if must_conditions else None


def search_chunks(
    client: QdrantClient, 
    query_vector: List[float], 
    retriever_top_k: int = 20, 
    metadata_filters: Optional[List[Dict[str, Any]]] = None,
    metadata_filter_mode: str = "soft",
    query_text: Optional[str] = None
) -> List[dict]:
    """
    Search for chunks with vector similarity and metadata filtering.
    
    Hard Mode: Strict pre-filtering.
    Soft Mode: Multi-recall merge (Semantic + Metadata + Keyword) with boosting.
    """
    rid = _get_req_id()
    logger.info(f"[{rid}] search_chunks_started: mode={metadata_filter_mode}, k={retriever_top_k}")
    
    qdrant_filter = build_qdrant_filter(metadata_filters)
    
    if metadata_filter_mode == "hard":
        results = client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            query_filter=qdrant_filter,
            limit=retriever_top_k,
            with_payload=True
        )
        
        # In hard mode, all results passed metadata filters and are ranked semantically
        reasons = ["semantic"]
        if metadata_filters:
            for f in metadata_filters:
                reasons.append(f"metadata:{f['field']}")
        
        output = []
        for hit in results.points:
            payload = hit.payload.copy()
            payload["_score"] = hit.score
            payload["_match_reasons"] = reasons
            output.append(payload)
            
        logger.info(f"[{rid}] hard_recall_completed: results={len(output)}")
        return output
    else:
        # Soft mode: Multi-recall merge
        
        # 1. Semantic Recall
        semantic_results = client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            limit=retriever_top_k * 2,
            with_payload=True
        )
        
        # 2. Metadata Recall (if filters exist)
        metadata_results = []
        if qdrant_filter:
            meta_res = client.query_points(
                collection_name=COLLECTION_NAME,
                query_filter=qdrant_filter,
                limit=retriever_top_k,
                with_payload=True
            )
            metadata_results = meta_res.points

        # 3. Keyword/Field Recall (Title/Path match)
        keyword_results = []
        if query_text:
            # We look for keywords in page_title and source_path
            keyword_filter = Filter(
                should=[
                    FieldCondition(key="page_title", match=MatchText(text=query_text)),
                    FieldCondition(key="source_path", match=MatchText(text=query_text)),
                    FieldCondition(key="text", match=MatchText(text=query_text)),
                ]
            )
            kw_res = client.query_points(
                collection_name=COLLECTION_NAME,
                query_filter=keyword_filter,
                limit=retriever_top_k,
                with_payload=True
            )
            keyword_results = kw_res.points

        # 4. Merge and deduplicate
        all_points = {} # id -> (point, reasons)
        for hit in semantic_results.points:
            all_points[hit.id] = {"hit": hit, "reasons": ["semantic"]}
            
        for hit in metadata_results:
            if hit.id in all_points:
                if "metadata" not in all_points[hit.id]["reasons"]:
                    all_points[hit.id]["reasons"].append("metadata")
            else:
                all_points[hit.id] = {"hit": hit, "reasons": ["metadata"]}
                
        for hit in keyword_results:
            if hit.id in all_points:
                if "keyword" not in all_points[hit.id]["reasons"]:
                    all_points[hit.id]["reasons"].append("keyword")
            else:
                all_points[hit.id] = {"hit": hit, "reasons": ["keyword"]}

        # 5. Scoring and match reasons
        scored_results = []
        for p_id, info in all_points.items():
            hit = info["hit"]
            reasons = info["reasons"]
            
            # Base score is semantic similarity if available, else 0
            score = hit.score if "semantic" in reasons else 0.0
            
            # Apply boosts
            if "metadata" in reasons:
                score += 2.0
            if "keyword" in reasons:
                score += 0.1
                
            # Store match reasons and score in payload for propagation
            payload = hit.payload.copy()
            payload["_score"] = score
            payload["_match_reasons"] = reasons
            
            scored_results.append({
                "payload": payload,
                "score": score,
                "match_reasons": reasons
            })
            
        # 6. Sort and limit
        scored_results.sort(key=lambda x: x["score"], reverse=True)
        final_results = scored_results[:retriever_top_k]
        
        logger.info(
            f"[{rid}] soft_recall_completed: semantic={len(semantic_results.points)}, "
            f"metadata={len(metadata_results)}, keyword={len(keyword_results)}, "
            f"merged={len(scored_results)}, final={len(final_results)}"
        )
        
        return [res["payload"] for res in final_results]


def delete_chunks_by_source_path(client: QdrantClient, source_path: str):
    """
    Delete all chunks (points) in Qdrant that belong to the given source_path.
    """
    rid = _get_req_id()
    logger.info(f"[{rid}] Deleting chunks for source_path: {source_path}")
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

def update_payload_by_doc_id(
    client: QdrantClient,
    doc_id: str,
    payload_patch: Dict[str, Any],
) -> int:
    """Update selected payload fields for all chunks belonging to *doc_id*.

    Only the fields listed in *payload_patch* are touched; all chunk-specific
    fields (chunk_id, chunk_index, chunk_type, text, embeddings, etc.) are
    left unchanged.

    Returns the number of points updated (best-effort scroll count).
    """
    rid = _get_req_id()
    doc_filter = Filter(
        must=[
            FieldCondition(key="doc_id", match=MatchValue(value=doc_id))
        ]
    )

    # Qdrant set_payload is a bulk operation — it patches all matching points
    client.set_payload(
        collection_name=COLLECTION_NAME,
        payload=payload_patch,
        points=doc_filter,
    )

    # Count updated points for logging (scroll is cheap here)
    hits, _ = client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=doc_filter,
        limit=1,
        with_payload=False,
        with_vectors=False,
    )
    count = 1 if hits else 0  # at least 1 if exists; exact count not critical
    logger.info(
        f"[{rid}] duplicate_document_qdrant_payload_updated: "
        f"doc_id={doc_id}, patched_fields={list(payload_patch.keys())}"
    )
    return count


def delete_chunks_by_metadata(client: QdrantClient, metadata_filter: Dict[str, str]):
    """
    Delete all chunks (points) in Qdrant that match the given user_metadata filter.
    """
    rid = _get_req_id()
    logger.info(f"[{rid}] Deleting chunks for metadata_filter: {metadata_filter}")
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
    """
    rid = _get_req_id()
    logger.debug(f"[{rid}] Listing documents in '{COLLECTION_NAME}'...")
    
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
    rid = _get_req_id()
    logger.debug(f"[{rid}] Counting documents in '{COLLECTION_NAME}'...")
    return len(list_documents(client, metadata_filter))


def get_metadata_schema(client: QdrantClient) -> List[str]:
    """
    Get all unique metadata keys present across all documents.
    """
    rid = _get_req_id()
    logger.debug(f"[{rid}] Getting metadata schema in '{COLLECTION_NAME}'...")
    docs = list_documents(client)
    keys = set()
    for doc in docs:
        if doc.get("user_metadata"):
            keys.update(doc["user_metadata"].keys())
    return sorted(list(keys))


def get_metadata_values(client: QdrantClient, key: str) -> List[Dict[str, Any]]:
    """
    Get all unique values and their document counts for a specific metadata key.
    """
    from collections import Counter
    rid = _get_req_id()
    logger.debug(f"[{rid}] Getting metadata values for key '{key}' in '{COLLECTION_NAME}'...")
    docs = list_documents(client)
    counts = Counter()
    for doc in docs:
        payload_val = doc
        for part in key.split("."):
            if isinstance(payload_val, dict):
                payload_val = payload_val.get(part)
            else:
                payload_val = None
                break
        if isinstance(payload_val, str):
            counts[payload_val] += 1
            
    # Return as list of dicts for the new schema
    return sorted(
        [{"value": v, "count": c} for v, c in counts.items()],
        key=lambda x: x["count"],
        reverse=True
    )


def search_documents(
    client: QdrantClient,
    query: str,
    limit: int = 50,
    metadata_filters: Optional[List[Dict[str, Any]]] = None,
    metadata_filter_mode: str = "soft"
) -> List[dict]:
    """
    Search for unique documents with metadata filtering.
    """
    rid = _get_req_id()
    logger.info(f"[{rid}] search_documents_started: query='{query}', mode={metadata_filter_mode}")
    query_vector = get_embeddings([query])[0]
    chunks = search_chunks(
        client=client,
        query_vector=query_vector,
        retriever_top_k=limit * 5,
        metadata_filters=metadata_filters,
        metadata_filter_mode=metadata_filter_mode,
        query_text=query
    )
    
    unique_docs = {}
    for chunk in chunks:
        doc_id = chunk.get("doc_id")
        if not doc_id:
            continue
            
        if doc_id not in unique_docs:
            # Match reasons from chunk
            chunk_reasons = chunk.get("_match_reasons", ["semantic"])
            
            unique_docs[doc_id] = {
                "doc_id": doc_id,
                "source_path": chunk.get("source_path", ""),
                "page_title": chunk.get("page_title", ""),
                "user_metadata": chunk.get("user_metadata", {}),
                "match_reasons": chunk_reasons
            }
            
            # Additional specific metadata reasons if filters provided
            if metadata_filters:
                for f in metadata_filters:
                    field = f.get("field")
                    val = f.get("value")
                    payload_val = chunk
                    for part in field.split("."):
                        if isinstance(payload_val, dict):
                            payload_val = payload_val.get(part)
                        else:
                            payload_val = None
                            break
                    if payload_val == val:
                        unique_docs[doc_id]["match_reasons"].append(f"metadata:{field}")
            
            # Clean match reasons
            unique_docs[doc_id]["match_reasons"] = sorted(list(set(unique_docs[doc_id]["match_reasons"])))

        if len(unique_docs) >= limit:
            break
            
    logger.info(f"[{rid}] search_documents_completed: unique_docs={len(unique_docs)}")
    return list(unique_docs.values())


def get_detailed_metadata_schema(client: QdrantClient) -> List[Dict[str, Any]]:
    """
    Get detailed metadata schema including field types and supported operators.
    """
    rid = _get_req_id()
    logger.debug(f"[{rid}] Getting detailed metadata schema in '{COLLECTION_NAME}'...")
    fields = [
        {"field": "chunk_type", "type": "string", "operators": ["eq", "exists"]},
        {"field": "language", "type": "string", "operators": ["eq", "exists"]},
        {"field": "source_path", "type": "string", "operators": ["eq", "exists", "contains"]},
        {"field": "page_title", "type": "string", "operators": ["eq", "exists", "contains"]},
        {"field": "doc_id", "type": "string", "operators": ["eq", "exists"]},
        {"field": "section_path", "type": "string", "operators": ["eq", "exists"]},
    ]
    user_keys = get_metadata_schema(client)
    for key in user_keys:
        fields.append({
            "field": f"user_metadata.{key}",
            "type": "string",
            "operators": ["eq", "exists"]
        })
    return fields
