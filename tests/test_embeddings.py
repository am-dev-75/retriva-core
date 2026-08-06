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

"""Tests for embedding resilience and configuration-error propagation.

These cover the per-purpose provider override feature: when the embedding
API key / base URL is misconfigured (e.g. a wrong EMBEDDING_API_KEY), the
provider returns HTTP 401 and the ingestion must FAIL loudly instead of
silently storing zero-vector chunks and reporting success.
"""

from unittest.mock import MagicMock, patch

import pytest

from retriva.indexing import embeddings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_openai_error(exc_cls, status_code, message="upstream error"):
    """Build an OpenAI status error without a live HTTP call.

    Mirrors tests/test_vlm_describer.py: APIStatusError.__init__ requires a
    real httpx response, so we bypass it and set status_code manually.
    """
    cls = type(
        "Fake" + exc_cls.__name__,
        (exc_cls,),
        {},
    )
    inst = cls.__new__(cls)
    Exception.__init__(inst, message)
    inst.status_code = status_code
    inst.response = MagicMock()
    return inst


def _make_embedding_response(vectors):
    """Build a fake OpenAI embeddings response object."""
    resp = MagicMock()
    resp.data = []
    for vec in vectors:
        item = MagicMock()
        item.embedding = vec
        resp.data.append(item)
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEmbeddingConfigurationErrors:
    def test_invalid_api_key_propagates_and_fails(self):
        """A 401 (bad/invalid embedding API key) must fail, not zero-vector."""
        fake_client = MagicMock()
        fake_client.embeddings.create.side_effect = _fake_openai_error(
            embeddings.AuthenticationError, 401, "Incorrect API key provided"
        )

        with patch.object(embeddings, "OpenAI", return_value=fake_client), \
             patch.object(embeddings.settings, "indexing_batch_size", 50), \
             patch.object(embeddings.settings, "embedding_dimension", 1024), \
             patch.object(embeddings.time, "sleep"):
            with pytest.raises(embeddings.EmbeddingConfigurationError):
                embeddings.get_embeddings(["chunk one", "chunk two"])

        # The configuration error must NOT be turned into a zero-vector.
        assert fake_client.embeddings.create.call_count >= 1

    def test_permission_denied_propagates_and_fails(self):
        """A 403 (insufficient permissions) must also fail loudly."""
        fake_client = MagicMock()
        fake_client.embeddings.create.side_effect = _fake_openai_error(
            embeddings.PermissionDeniedError, 403, "Permission denied"
        )

        with patch.object(embeddings, "OpenAI", return_value=fake_client), \
             patch.object(embeddings.settings, "indexing_batch_size", 50), \
             patch.object(embeddings.settings, "embedding_dimension", 1024), \
             patch.object(embeddings.time, "sleep"):
            with pytest.raises(embeddings.EmbeddingConfigurationError):
                embeddings.get_embeddings(["chunk one"])

    def test_transient_runtime_error_still_zero_vectors(self):
        """Backward compatibility: isolated per-chunk failures keep the
        zero-vector fallback instead of failing the whole ingestion."""
        fake_client = MagicMock()
        # A plain per-text failure (not an auth/permission error).
        fake_client.embeddings.create.side_effect = RuntimeError("bad content")

        with patch.object(embeddings, "OpenAI", return_value=fake_client), \
             patch.object(embeddings.settings, "indexing_batch_size", 50), \
             patch.object(embeddings.settings, "embedding_dimension", 1024), \
             patch.object(embeddings.time, "sleep"):
            result = embeddings.get_embeddings(["chunk one", "chunk two"])

        # Every text is replaced by a zero vector, and no exception escapes.
        assert len(result) == 2
        assert all(all(v == 0.0 for v in vec) for vec in result)
