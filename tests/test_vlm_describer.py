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

"""Tests for the VLM describer's resilience to upstream rate limits.

The 100-page scanned-manual ingestion failed because OpenRouter/Alibaba
returned HTTP 429 for the Qwen vision model. The describer must now retry
transient errors with backoff instead of silently dropping the figure.
"""

import io
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from retriva.ingestion import vlm_describer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_png(path):
    """Write a small valid PNG so magic-byte validation passes."""
    img = Image.new("RGB", (32, 32), color=(120, 80, 200))
    img.save(str(path), format="PNG")
    return path


def _make_response(text):
    """Build a fake OpenAI chat-completions response object."""
    msg = MagicMock()
    msg.content = text
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


class _FakeRateLimitError(vlm_describer.APIStatusError):
    """Construct a RateLimitError-like exception without a live HTTP call."""

    def __init__(self, retry_after=None):
        headers = {}
        if retry_after is not None:
            headers["retry-after"] = str(retry_after)
        response = MagicMock()
        response.headers = headers
        # Bypass APIStatusError.__init__ (needs a real httpx response).
        Exception.__init__(self, "429 Too Many Requests")
        self.status_code = 429
        self.response = response


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestVlmRetry:
    def test_succeeds_first_try(self, tmp_path):
        img = _write_png(tmp_path / "fig.png")
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = _make_response("A diagram.")

        with patch.object(vlm_describer, "OpenAI", return_value=fake_client):
            result = vlm_describer.describe_image(img)

        assert result == "A diagram."
        assert fake_client.chat.completions.create.call_count == 1

    def test_retries_then_succeeds_on_429(self, tmp_path):
        img = _write_png(tmp_path / "fig.png")
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = [
            _FakeRateLimitError(),
            _FakeRateLimitError(),
            _make_response("Recovered description."),
        ]

        with patch.object(vlm_describer, "OpenAI", return_value=fake_client), \
             patch.object(vlm_describer.time, "sleep") as mock_sleep, \
             patch.object(vlm_describer.settings, "visual_max_retries", 5), \
             patch.object(vlm_describer.settings, "visual_retry_base_delay", 0.01), \
             patch.object(vlm_describer.settings, "visual_retry_max_delay", 0.05):
            result = vlm_describer.describe_image(img)

        assert result == "Recovered description."
        assert fake_client.chat.completions.create.call_count == 3
        # Two failed attempts → two backoff sleeps.
        assert mock_sleep.call_count == 2

    def test_gives_up_after_max_retries(self, tmp_path):
        img = _write_png(tmp_path / "fig.png")
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = _FakeRateLimitError()

        with patch.object(vlm_describer, "OpenAI", return_value=fake_client), \
             patch.object(vlm_describer.time, "sleep"), \
             patch.object(vlm_describer.settings, "visual_max_retries", 3), \
             patch.object(vlm_describer.settings, "visual_retry_base_delay", 0.01), \
             patch.object(vlm_describer.settings, "visual_retry_max_delay", 0.05):
            result = vlm_describer.describe_image(img)

        # Graceful degradation: empty string, never raises.
        assert result == ""
        # 1 initial + 3 retries = 4 attempts.
        assert fake_client.chat.completions.create.call_count == 4

    def test_non_retryable_error_returns_immediately(self, tmp_path):
        img = _write_png(tmp_path / "fig.png")
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = ValueError("bad request")

        with patch.object(vlm_describer, "OpenAI", return_value=fake_client), \
             patch.object(vlm_describer.time, "sleep") as mock_sleep, \
             patch.object(vlm_describer.settings, "visual_max_retries", 5):
            result = vlm_describer.describe_image(img)

        assert result == ""
        # No retries for a non-retryable error.
        assert fake_client.chat.completions.create.call_count == 1
        assert mock_sleep.call_count == 0

    def test_retry_after_header_is_honoured(self, tmp_path):
        img = _write_png(tmp_path / "fig.png")
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = [
            _FakeRateLimitError(retry_after=30),
            _make_response("ok"),
        ]

        with patch.object(vlm_describer, "OpenAI", return_value=fake_client), \
             patch.object(vlm_describer.time, "sleep") as mock_sleep, \
             patch.object(vlm_describer.settings, "visual_max_retries", 5), \
             patch.object(vlm_describer.settings, "visual_retry_base_delay", 0.01), \
             patch.object(vlm_describer.settings, "visual_retry_max_delay", 0.05):
            result = vlm_describer.describe_image(img)

        assert result == "ok"
        # Backoff must respect the Retry-After hint (>= 30s) despite the tiny
        # base delay.
        slept = mock_sleep.call_args[0][0]
        assert slept >= 30


class TestRetryAfterParsing:
    def test_missing_response_returns_none(self):
        assert vlm_describer._retry_after_seconds(ValueError("x")) is None

    def test_parses_numeric_header(self):
        exc = _FakeRateLimitError(retry_after=12)
        assert vlm_describer._retry_after_seconds(exc) == 12.0

    def test_non_numeric_header_returns_none(self):
        exc = _FakeRateLimitError(retry_after="Wed, 21 Oct 2026 07:28:00 GMT")
        assert vlm_describer._retry_after_seconds(exc) is None
