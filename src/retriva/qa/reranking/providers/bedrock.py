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

"""
Amazon Bedrock rerank provider.

Uses the Bedrock Rerank API (``bedrock-runtime`` ``rerank`` command) via
``boto3``. ``boto3`` is imported lazily so deployments that never select
``RETRIEVAL_RERANK_PROVIDER=bedrock`` do not require it at runtime.

Secret handling: credentials are NOT part of Retriva settings. The
standard AWS credential chain applies — ``AWS_ACCESS_KEY_ID`` /
``AWS_SECRET_ACCESS_KEY`` / ``AWS_SESSION_TOKEN`` environment variables,
shared credentials/config files, or the workload IAM role (recommended).
Region precedence: ``RETRIEVAL_RERANK_AWS_REGION`` > ``AWS_REGION`` >
``AWS_DEFAULT_REGION``.

``RETRIEVAL_RERANK_MODEL`` accepts either a bare reranking model id
(e.g. ``amazon.rerank-v1:0``, resolved to the region's foundation-model
ARN) or a full model ARN (used verbatim).
"""

import os
import time
from typing import Any, Dict, List, Optional

from retriva.logger import get_logger
from retriva.qa.reranking.base import (
    RerankProvider,
    RerankProviderConfig,
    RerankProviderError,
)

logger = get_logger(__name__)


def effective_aws_region(config: RerankProviderConfig) -> Optional[str]:
    """Resolve the AWS region for Bedrock: setting > AWS_REGION > AWS_DEFAULT_REGION."""
    return (
        (config.aws_region or "").strip()
        or os.environ.get("AWS_REGION", "").strip()
        or os.environ.get("AWS_DEFAULT_REGION", "").strip()
        or None
    )


def model_arn(model: str, region: str) -> str:
    """Return *model* as a full Bedrock model ARN (passthrough for ARNs)."""
    model = (model or "").strip()
    if model.startswith("arn:aws:bedrock"):
        return model
    return f"arn:aws:bedrock:{region}::foundation-model/{model}"


class BedrockRerankProvider(RerankProvider):
    """Amazon Bedrock Rerank transport (``bedrock-runtime`` client)."""

    name = "bedrock"

    def __init__(self, config: RerankProviderConfig, client: Optional[Any] = None):
        """
        Args:
            config: Effective provider configuration.
            client: Pre-built boto3 client — testing hook; when ``None``
                a client is created lazily on first use.
        """
        self.config = config
        self._client = client
        self._arn_cache: Optional[str] = None

    # -- client management ---------------------------------------------------

    def _region(self) -> str:
        region = effective_aws_region(self.config)
        if not region:
            raise RerankProviderError(
                "Bedrock reranker has no AWS region: set "
                "RETRIEVAL_RERANK_AWS_REGION, AWS_REGION or AWS_DEFAULT_REGION."
            )
        return region

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:
                raise RerankProviderError(
                    "The bedrock rerank provider requires 'boto3'. "
                    "Install it (pip install boto3) or pick another provider."
                ) from exc

            region = self._region()
            timeout = max(1.0, float(self.config.timeout))
            self._client = boto3.client(
                "bedrock-runtime",
                region_name=region,
                config=Config(
                    read_timeout=timeout,
                    connect_timeout=min(10.0, timeout),
                    # max_attempts counts the initial attempt, matching the
                    # HTTPX provider's semantics (default 2 total attempts).
                    retries={"max_attempts": max(1, self.config.max_retries), "mode": "adaptive"},
                ),
            )
            logger.debug(f"Bedrock rerank client initialized (region={region}).")
        return self._client

    # -- provider SPI ----------------------------------------------------------

    def rank(self, query: str, documents: List[str], top_n: int) -> List[Dict]:
        if not documents:
            # Deterministic no-op: never send an empty sources list to the API.
            return []

        client = self._get_client()
        region = self._region()

        if self._arn_cache is None:
            self._arn_cache = model_arn(self.config.model, region)

        payload = {
            "queries": [{"type": "TEXT", "textQuery": {"text": query}}],
            "sources": [
                {
                    "type": "INLINE",
                    "inlineDocumentSource": {
                        "type": "TEXT",
                        "textDocument": {"text": doc},
                    },
                }
                for doc in documents
            ],
            "rerankingConfiguration": {
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {"modelArn": self._arn_cache},
                    "numberOfResults": max(1, min(int(top_n), len(documents))),
                },
            },
        }

        started = time.perf_counter()
        try:
            response = client.rerank(**payload)
        except Exception as exc:
            raise RerankProviderError(
                f"Bedrock rerank failed ({type(exc).__name__}): {exc}"
            ) from exc

        duration_ms = (time.perf_counter() - started) * 1000
        raw_results = response.get("results", []) if isinstance(response, dict) else []
        results = []
        for r in raw_results:
            if not isinstance(r, dict):
                raise RerankProviderError(
                    f"Bedrock rerank returned malformed result {r!r}: "
                    f"expected an object."
                )
            idx = r.get("index")
            # bool is an int subclass — reject it explicitly (same policy
            # as DefaultReranker._coerce_result).
            if isinstance(idx, bool) or not isinstance(idx, int):
                raise RerankProviderError(
                    f"Bedrock rerank returned malformed result {r!r}: "
                    f"'index' must be an integer."
                )
            try:
                relevance = float(r.get("relevanceScore", 0.0))
            except (TypeError, ValueError) as exc:
                raise RerankProviderError(
                    f"Bedrock rerank returned malformed result {r!r}: "
                    f"'relevanceScore' must be numeric."
                ) from exc
            results.append({"index": idx, "relevance_score": relevance})

        logger.debug(
            f"Bedrock rerank: {len(documents)} docs → {len(results)} results "
            f"in {duration_ms:.0f}ms (model={self._arn_cache})."
        )
        return results


def _build(config: RerankProviderConfig) -> BedrockRerankProvider:
    return BedrockRerankProvider(config)
