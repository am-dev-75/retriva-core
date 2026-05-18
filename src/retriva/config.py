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

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

VERSION = "0.27.1"

class Settings(BaseSettings):
    mirror_base_path: str = str((Path(__file__).resolve().parent / "mirror").resolve())
    canonical_base_url: str = "https://wiki.dave.eu"
    
    qdrant_url: str = "http://192.168.1.64:6333"
    qdrant_collection_name: str = "retriva_chunks"
    local_openai_api_key: str = "sk-mock-key"
    openrouter_openai_api_key: str = ""
    
    # Embedding model
    embedding_base_url: str = "https://openrouter.ai/api/v1"
    embedding_model: str = "baai/bge-m3"
    embedding_dimension: int = 1024
    embedding_openai_api_key: Optional[str] = None

    # Visual model
    visual_base_url: str = "https://openrouter.ai/api/v1"
    visual_model: str = "qwen/qwen3-vl-32b-instruct"
    visual_openai_api_key: Optional[str] = None
    visual_max_tokens: int = 2048
    visual_temperature: float = 0.0
    
    # Chat model
    chat_base_url: str = "https://openrouter.ai/api/v1"
    chat_model: str = "qwen/qwen3.5-27b"
    # chat_model: str = "minimax/minimax-m2.7"
    chat_openai_api_key: str = ""
    chat_temperature: float = 0.0
    chat_top_p: float = 0.9
    
    # Storage and Persistence
    storage_path: str = str((Path(__file__).resolve().parent.parent.parent / "storage").resolve())
    kb_mapping_db: str = str((Path(__file__).resolve().parent.parent.parent / "storage" / "kb_mappings.db").resolve())
    artifacts_path: str = str((Path(__file__).resolve().parent.parent.parent / "storage" / "artifacts").resolve())
    
    # Retriva constitution
    retriva_constitution: str = str((Path(__file__).resolve().parent.parent.parent / ".agent" / "rules" / "retriva-constitution.md").resolve())

    # Retrieval
    retriever_top_k: int = 20
    retrieval_fetch_k_multiplier: int = 5
    retrieval_max_chunks_per_doc: int = 3
    retrieval_metadata_boost: float = 0.1
    
    # Retrieval re-ranking (two-stage)
    enable_retrieval_reranking: bool = True
    retrieval_rerank_model: str = "cohere/rerank-v3.5"
    retrieval_rerank_base_url: str = "https://openrouter.ai/api/v1"
    retrieval_rerank_api_key: Optional[str] = None
    retrieval_rerank_candidates: int = 100
    retrieval_rerank_top_n: int = 30
    retrieval_rerank_batch_size: int = 100
    retrieval_rerank_max_length: int = 4096

    # Hybrid retrieval selection
    enable_hybrid_retrieval_selection: bool = True
    hybrid_rerank_keep_top_m: int = 4
    hybrid_vector_keep_top_l: int = 2

    # Indexing
    indexing_batch_size: int = 100
    max_chunk_chars: int = 2000
    chunk_overlap: int = 200

    # Extension discovery (comma-separated dotted module paths)
    retriva_extensions: str = ""

    # v2 Ingestion Pipeline
    tika_server_url: str = "http://localhost:9998"
    ocrmypdf_enabled: bool = True
    ocrmypdf_language: str = "eng+ita"
    ocrmypdf_deskew: bool = True
    ocrmypdf_rotate_pages: bool = True
    v2_primary_parser: str = "docling"
    accelerator_device: str = "cpu"  # cpu, cuda, mps, auto

    # OpenAI-compatible API (for Open WebUI)
    openai_api_port: int = 8001

    # Citation metadata limits
    citation_snippet_size: int = 2000
    max_citations: int = 25
    max_metadata_per_citation: int = 0

    # Legacy Injection API
    ingestion_api_port: int = 8000
    
    # User Interface
    ui_port: int = 3000
    
    # Internal Request Profiler
    enable_internal_profiler: bool = False

    # Pydantic Settings
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    def model_post_init(self, __context):
        """Handle API key fallback to OPENROUTER_OPENAI_API_KEY."""
        if self.openrouter_openai_api_key:
            if not self.embedding_openai_api_key:
                self.embedding_openai_api_key = self.openrouter_openai_api_key
            if not self.chat_openai_api_key:
                self.chat_openai_api_key = self.openrouter_openai_api_key
            if not self.visual_openai_api_key:
                self.visual_openai_api_key = self.openrouter_openai_api_key
            if not self.retrieval_rerank_api_key:
                self.retrieval_rerank_api_key = self.openrouter_openai_api_key

settings = Settings()
