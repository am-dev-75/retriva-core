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
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from retriva.openai_api.routers import chat_completions, models, internal
from retriva.openai_api.routers import intent_classification
from retriva.ingestion_api.routers import v2_sessions
# Register parser capabilities (parser:default / parser:docling) needed by
# the session attachment upload/parse endpoints.
import retriva.ingestion.parser_router  # noqa: F401
import retriva.ingestion.docling_parser  # noqa: F401
from retriva.indexing.qdrant_store import init_collection, get_client
from retriva.logger import get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing Retriva OpenAI-compatible API...")
    try:
        client = get_client()
        init_collection(client)
    except Exception as e:
        logger.error(f"Failed to initialize Qdrant during startup: {e}")

    # Load extensions (no-op if RETRIVA_EXTENSIONS is empty)
    from retriva.registry import CapabilityRegistry
    CapabilityRegistry().load_extensions()

    # Validate the global reranking configuration. Non-strict mode (default):
    # log-and-continue, matching the Qdrant init convention; strict mode
    # (RETRIEVAL_RERANK_STRICT_STARTUP_VALIDATION=true) FAILS startup — the
    # raise must happen outside the log-and-continue handler below.
    try:
        from retriva.qa.reranking import refresh_reranker_health, validate_rerank_config
        reranker_issues = validate_rerank_config()
        refresh_reranker_health()
        for issue in reranker_issues:
            message = f"Reranker config issue: {issue['message']}"
            if issue["level"] == "error":
                logger.error(message)
            else:
                logger.warning(message)
    except Exception as e:
        logger.error(f"Reranker validation failed during startup: {e}")
    # Strict-mode enforcement (raises RerankStartupError → startup fails).
    from retriva.qa.reranking import enforce_rerank_startup
    enforce_rerank_startup()

    # Mount extension-provided API routers (e.g. CRM Assistant).
    try:
        from retriva.registry import CapabilityRegistry as _Reg
        reg = _Reg()
        for cap_name in list(reg.list_capabilities().keys()):
            if cap_name.endswith("_api_router"):
                try:
                    provider = reg.get(cap_name)
                    router = getattr(provider, "router", None)
                    if router is not None:
                        app.include_router(router)
                        logger.info(f"Mounted extension router: {cap_name}")
                except Exception as e:
                    logger.error(f"Failed to mount extension router {cap_name}: {e}")
    except Exception as e:
        logger.error(f"Extension router discovery failed: {e}")

    # Best-effort expiration sweep for session attachments/artifacts.
    try:
        from retriva.config import settings as _settings
        if getattr(_settings, "session_sweep_on_startup", True):
            from retriva.session.lifecycle import sweep_expired
            sweep_expired()
    except Exception as e:
        logger.error(f"Session expiration sweep failed on startup: {e}")

    yield
    # Shutdown
    logger.info("Shutting down OpenAI-compatible API...")


from retriva.config import VERSION
app = FastAPI(
    title="Retriva OpenAI-Compatible API",
    version=VERSION,
    description=(
        "OpenAI-compatible chat completions and model listing for "
        "Open WebUI integration."
    ),
    lifespan=lifespan,
)

from retriva.middleware.collection import CollectionMiddleware
app.add_middleware(CollectionMiddleware)

@app.get("/")
async def root():
    """Returns basic API information."""
    return {
        "app": "Retriva OpenAI-Compatible API",
        "version": VERSION,
        "api_v1": "/v1",
        "status": "ready"
    }

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}

# Allow cross-origin requests — Open WebUI may run on a different host/port.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_completions.router)
app.include_router(models.router)
app.include_router(internal.router)
app.include_router(internal.reranker_router)
# Spec 001 Phase D: internal service-to-service intent classification
# (C6 trust boundary — service credential + purpose marker + limits;
# intentionally NOT part of the public OpenAPI contract; the Gateway
# does not proxy it; obscurity is not the control).
app.include_router(intent_classification.router)
app.include_router(v2_sessions.router)