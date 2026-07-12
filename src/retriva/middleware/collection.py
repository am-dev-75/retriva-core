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

from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request
from retriva.indexing.qdrant_store import set_collection_name, DEFAULT_COLLECTION_NAME
from retriva.domain.validation import validate_collection_name

class CollectionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Read the header injected by the Gateway.
        # Fall back to the deployment default if not present (e.g., local dev without Gateway).
        col = request.headers.get("X-Retriva-Collection", DEFAULT_COLLECTION_NAME)
        
        # Sanitize/validate before using it in ContextVar (which feeds Qdrant and FS paths).
        col = validate_collection_name(col)
        
        token = set_collection_name(col)
        try:
            return await call_next(request)
        finally:
            from retriva.indexing.qdrant_store import _collection_name_ctx
            _collection_name_ctx.reset(token)
