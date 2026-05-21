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

from fastapi import FastAPI
from contextlib import asynccontextmanager
from retriva.ingestion_api.routers import ingest, ingest_HTML, ingest_image, ingest_text, ingest_mediawiki, ingest_pdf, ingest_markdown, jobs, documents
from retriva.ingestion_api.routers import v2_documents, v2_jobs, v2_artifacts, v2_discovery, v2_metadata, v2_retrieval, v2_kbs
from retriva.indexing.qdrant_store import init_collection, get_client
from retriva.domain.kb import seed_default_kb
from retriva.logger import get_logger

logger = get_logger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing Modular Injection API...")
    try:
        client = get_client()
        init_collection(client)
    except Exception as e:
        logger.error(f"Failed to initialize Qdrant during startup: {e}")

    # Seed the KB registry (idempotent). Logged-and-continued on failure so
    # startup behavior is preserved; the Phase 2 KB API surface will report a
    # clear error if the registry is unavailable at request time.
    try:
        seed_default_kb()
    except Exception as e:
        logger.error(f"Failed to seed default KB during startup: {e}")

    # Load extensions (no-op if RETRIVA_EXTENSIONS is empty)
    from retriva.registry import CapabilityRegistry
    CapabilityRegistry().load_extensions()

    yield
    # Shutdown
    logger.info("Shutting down API...")

from retriva.config import VERSION

app = FastAPI(
    title="Retriva Modular Injection API",
    version=VERSION,
    description="REST API for injecting documents into the Retriva RAG pipeline.",
    lifespan=lifespan
)

@app.get("/")
async def root():
    """Returns basic API information."""
    return {
        "app": "Retriva Modular Injection API",
        "version": VERSION,
        "api_v1": "/api/v1",
        "api_v2": "/api/v2"
    }

app.include_router(ingest.router)
app.include_router(ingest_HTML.router)
app.include_router(ingest_image.router)
app.include_router(ingest_text.router)
app.include_router(ingest_mediawiki.router)
app.include_router(ingest_pdf.router)
app.include_router(ingest_markdown.router)
app.include_router(jobs.router)
app.include_router(documents.router)
app.include_router(v2_discovery.router)
app.include_router(v2_documents.router)
app.include_router(v2_jobs.router)
app.include_router(v2_artifacts.router)
app.include_router(v2_metadata.router)
app.include_router(v2_retrieval.router)
app.include_router(v2_kbs.router)