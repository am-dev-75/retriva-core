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

VERSION = "1.2.2"

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
    # Resilience controls for the VLM image-description calls. Vision models
    # routed through OpenRouter (e.g. qwen/qwen3-vl-*) are frequently
    # rate-limited upstream (HTTP 429). Without retries a transient 429
    # silently discards a figure's description; without pacing the tight
    # enrichment loop *triggers* the throttling. These settings make the
    # behaviour explicit and tunable.
    visual_max_retries: int = 5            # extra attempts on 429 / transient errors
    visual_retry_base_delay: float = 2.0   # seconds; doubled each retry (capped)
    visual_retry_max_delay: float = 60.0   # seconds; ceiling for backoff
    visual_inter_call_delay: float = 0.0   # seconds to sleep between images (pacing)
    
    # Chat model
    chat_base_url: str = "https://openrouter.ai/api/v1"
    chat_model: str = "qwen/qwen3.5-27b"
    # chat_model: str = "minimax/minimax-m2.7"
    chat_openai_api_key: str = ""
    chat_temperature: float = 0.0
    chat_top_p: float = 0.9
    chat_max_tokens: int = 4096
    # Reasoning effort for models that support it (e.g. OpenAI o-series,
    # DeepSeek-R1 via OpenRouter).  Values: "low", "medium", "high".
    # None/empty = not sent (model default).
    chat_reasoning_effort: Optional[str] = None
    
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
    # Skip the per-document diversity cap when the candidate pool contains only
    # ONE distinct document. With a single-document corpus the cap has nothing
    # to diversify against and merely truncates the reranker's best chunks to
    # ``retrieval_max_chunks_per_doc`` (e.g. 3), which starves the context and
    # can let marginally-higher-scoring but off-topic chunks crowd out the
    # relevant section. When True, single-doc queries keep up to ``top_k``
    # reranked chunks instead.
    retrieval_single_doc_bypass_diversity: bool = True
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
    ocrmypdf_force_ocr: bool = True
    # Re-OCR PDFs whose embedded text layer is present but garbled (low
    # quality). Some PDFs ship a corrupt/lossy text layer (e.g. exported from
    # imaging software or a prior bad OCR pass) that Tika still reports as
    # "text", so the scanned heuristic alone never triggers OCR. When enabled,
    # the DETECTING stage samples the embedded text and flags it for re-OCR if
    # its quality score falls below ``ocr_text_quality_threshold``.
    ocr_redo_low_quality: bool = True
    # Fraction (0..1) of sampled tokens that must look like plausible words for
    # the embedded text layer to be considered "good". Below this, the PDF is
    # re-OCR'd. Lower = more permissive (re-OCR less often).
    ocr_text_quality_threshold: float = 0.55
    # Minimum number of characters Tika must return before the quality
    # heuristic runs; below this we defer to the scanned/charsPerPage signal.
    ocr_text_quality_min_chars: int = 200
    v2_primary_parser: str = "docling"
    accelerator_device: str = "cpu"  # cpu, cuda, mps, auto
    # Docling ships with its own built-in OCR engine (PaddleOCR / PP-OCRv*).
    # In Retriva, OCR is performed UPSTREAM by OCRmyPDF (PREPROCESSING stage),
    # so Docling's internal OCR is redundant. Worse, some Docling builds ship
    # an OCR model config that is unavailable at runtime (e.g.
    # "Unsupported configuration: torch.PP-OCRv6.det.small"), which makes
    # ``convert()`` raise and yields zero records. Disabling Docling's OCR
    # avoids both the redundancy and that crash; the (already-OCR'd) text
    # layer is read directly. Set True only if you intentionally rely on
    # Docling's OCR instead of OCRmyPDF.
    docling_do_ocr: bool = False

    # Docling image extraction (enables VLM enrichment of figures/diagrams).
    # When enabled, Docling rasterizes page pictures so they can be persisted
    # to disk and described by the visual model. Disable to save CPU/time on
    # text-only corpora.
    docling_generate_picture_images: bool = True
    # Upscale factor for rasterized images (1.0 = native). Higher values give
    # the VLM more legible figures at the cost of memory/latency.
    docling_images_scale: float = 2.0
    # Skip describing tiny images (icons, bullets, rule lines) that carry no
    # retrievable content. Value is the minimum width*height in pixels.
    docling_min_picture_area_px: int = 64 * 64
    # When a PDF (typically the OCR'd intermediate) exceeds this many pages,
    # the PARSING stage splits it into smaller chunks and parses each
    # separately. This prevents OOM-kills when Docling loads the entire PDF
    # into memory. 0 = never split.
    docling_pdf_split_page_threshold: int = 200
    # Number of pages per chunk when splitting. Each chunk is parsed
    # independently by Docling; records are then merged.
    docling_pdf_split_chunk_size: int = 100

    # OpenAI-compatible API (for Open WebUI)
    openai_api_port: int = 8001

    # Citation metadata limits
    citation_snippet_size: int = 2000
    max_citations: int = 25
    max_chars_per_source: int = 24000
    max_metadata_per_citation: int = 0

    # Legacy Injection API
    ingestion_api_port: int = 8000
    
    # User Interface
    ui_port: int = 3000
    
    # Internal Request Profiler
    enable_internal_profiler: bool = False

    # ── Asynchronous job queue (Celery + Redis) ──────────────────────────
    # When ``celery_broker_url`` is set (non-empty), the v2 ingestion pipeline
    # dispatches work to a Celery worker instead of using FastAPI
    # BackgroundTasks.  This makes long-running ingestions (e.g. OCRmyPDF on a
    # 1400-page scanned PDF) survive API restarts.  When unset, the system
    # falls back to the original in-memory BackgroundTasks path.
    celery_broker_url: str = ""        # e.g. redis://redis:6379/0
    celery_result_backend: str = ""    # e.g. redis://redis:6379/1
    celery_task_max_retries: int = 3
    # Soft time limit per task in seconds (0 = no limit).  OCR on huge PDFs
    # can take 30+ minutes, so the default is generous.
    celery_task_soft_time_limit: int = 0
    celery_task_time_limit: int = 0
    # Number of worker processes.  Each can consume significant CPU/memory
    # during OCR/Docling.  Defaults to 1 for predictability.
    celery_worker_concurrency: int = 1
    # When True, the worker re-queues a task whose process is killed (SIGKILL /
    # OOM).  Requires ``task_acks_late = True``.
    celery_worker_prefetch_multiplier: int = 1

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
