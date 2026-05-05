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

@app.get("/")
async def root():
    """Returns basic API information."""
    return {
        "app": "Retriva OpenAI-Compatible API",
        "version": VERSION,
        "api_v1": "/v1",
        "status": "ready"
    }

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