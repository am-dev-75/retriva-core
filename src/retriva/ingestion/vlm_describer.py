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

import base64
import mimetypes
import random
import time
from pathlib import Path
from typing import Optional
from openai import OpenAI
from openai import APIStatusError, APIConnectionError, APITimeoutError, RateLimitError
from retriva.config import settings
from retriva.logger import get_logger

logger = get_logger(__name__)

# Magic byte signatures for supported image formats
_IMAGE_SIGNATURES = (
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"\xff\xd8\xff",        # JPEG
    b"GIF87a",              # GIF 87a
    b"GIF89a",              # GIF 89a
)


def _has_image_magic(raw: bytes) -> bool:
    """Return True if *raw* starts with a known image file signature."""
    if any(raw.startswith(sig) for sig in _IMAGE_SIGNATURES):
        return True
    # WebP: RIFF....WEBP
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return True
    return False


VLM_PROMPT = (
    "Describe this technical image in detail for an engineering knowledge base. "
    "Include: all visible components, labels, connections, pin names, "
    "measurements with their numerical values and units, "
    "axis labels and scales on charts or plots, data trends and key readings, "
    "instrument settings and display values, "
    "and any textual annotations. "
    "If it is a schematic or block diagram, describe the signal flow. "
    "If it is a chart or plot, describe what is being measured and the observed behavior."
)


def describe_image(image_path: Path) -> str:
    """
    Sends an image to the VLM and returns a detailed text description.

    On any failure (network, quota, unsupported format) the function
    logs a warning and returns an empty string so that the pipeline
    can fall back to HTML-metadata-only context.
    """
    if not image_path.is_file():
        logger.warning(f"Image file not found: {image_path}")
        return ""

    mime_type, _ = mimetypes.guess_type(str(image_path))
    if not mime_type or not mime_type.startswith("image/"):
        logger.warning(f"Unsupported MIME type for VLM: {mime_type} ({image_path})")
        return ""

    try:
        raw = image_path.read_bytes()
    except Exception as e:
        logger.warning(f"Failed to read image {image_path}: {e}")
        return ""

    # Validate actual file content — extension alone can be misleading
    # (e.g. wget-mirrored HTML pages whose filename ends in .png)
    if not _has_image_magic(raw):
        logger.warning(
            f"File content does not match any known image format "
            f"(MIME guessed: {mime_type}): {image_path}"
        )
        return ""

    try:
        b64 = base64.b64encode(raw).decode("utf-8")
        data_uri = f"data:{mime_type};base64,{b64}"
    except Exception as e:
        logger.warning(f"Failed to read/encode image {image_path}: {e}")
        return ""

    client = OpenAI(
        api_key=settings.visual_openai_api_key,
        base_url=settings.visual_base_url,
    )
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": VLM_PROMPT},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }
    ]

    max_retries = max(0, settings.visual_max_retries)
    base_delay = max(0.0, settings.visual_retry_base_delay)
    max_delay = max(base_delay, settings.visual_retry_max_delay)

    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=settings.visual_model,
                messages=messages,
                max_tokens=settings.visual_max_tokens,
                temperature=settings.visual_temperature,
            )
            description = response.choices[0].message.content.strip()
            logger.debug(f"VLM described {image_path.name} ({len(description)} chars)")
            return description
        except Exception as e:
            # Decide whether this error is worth retrying. Rate limits (429)
            # and transient connection/timeout errors are; anything else
            # (bad request, auth, unsupported image) is not.
            status_code = getattr(e, "status_code", None)
            retryable = isinstance(
                e, (RateLimitError, APIConnectionError, APITimeoutError)
            ) or (isinstance(e, APIStatusError) and status_code in (429, 500, 502, 503, 504))

            if retryable and attempt < max_retries:
                # Exponential backoff with jitter, honouring Retry-After when present.
                delay = min(base_delay * (2 ** attempt), max_delay)
                retry_after = _retry_after_seconds(e)
                if retry_after is not None:
                    delay = max(delay, retry_after)
                delay += random.uniform(0, base_delay)
                logger.warning(
                    f"VLM call rate-limited/transient error for {image_path.name} "
                    f"(attempt {attempt + 1}/{max_retries + 1}); retrying in {delay:.1f}s: {e}"
                )
                time.sleep(delay)
                continue

            logger.warning(f"VLM call failed for {image_path}: {e}")
            return ""

    return ""


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    """Extract a Retry-After hint (seconds) from an OpenAI/HTTP error, if any."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if not headers:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class DefaultVLMDescriber:
    """OSS default VLM describer — OpenAI-compatible vision model."""

    def describe(self, image_path: Path) -> str:
        return describe_image(image_path)


# Register as default implementation
from retriva.registry import CapabilityRegistry
CapabilityRegistry().register("vlm_describer", DefaultVLMDescriber, priority=100)
