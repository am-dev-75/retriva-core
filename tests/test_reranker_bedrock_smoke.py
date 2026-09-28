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
Opt-in live AWS Bedrock rerank smoke test (requirement 10).

NEVER runs in normal CI: the test is skipped unless the operator sets
RETRIVA_RUN_BEDROCK_SMOKE_TEST=1. It exercises the NORMAL Retriva provider
path (DefaultReranker + factory + real env settings) against the real
eu-central-1 endpoint with synthetic documents, expecting the Qdrant
document to rank first. Pending AWS account verification is reported
clearly (the classified error category is account_verification_pending).
"""

import os

import pytest

from retriva.qa.reranker import DefaultReranker
from retriva.qa.reranking.factory import reset_provider_cache
from retriva.qa.reranking.health import reranker_health

pytestmark = pytest.mark.skipif(
    os.environ.get("RETRIVA_RUN_BEDROCK_SMOKE_TEST") != "1",
    reason="Opt-in live AWS test — skipped by default (never runs in CI).",
)

SMOKE_REGION = "eu-central-1"
SMOKE_MODEL = "cohere.rerank-v3-5:0"

SYNTHETIC_CHUNKS = [
    {
        "text": "Qdrant is an open-source vector database designed for "
        "high-performance similarity search over embedding vectors. It "
        "supports payload filtering, hybrid search and distributed "
        "deployments.",
        "page_title": "Qdrant overview",
        "source_path": "/docs/qdrant",
    },
    {
        "text": "Sourdough bread is made by fermenting dough using naturally "
        "occurring lactobacilli and yeast over a long, slow process that "
        "develops the crumb structure.",
        "page_title": "Baking basics",
        "source_path": "/baking/sourdough",
    },
    {
        "text": "The 1974 FIFA World Cup final was played in Munich, where "
        "West Germany defeated the Netherlands 2-1.",
        "page_title": "Football history",
        "source_path": "/football/1974",
    },
]

QUERY = "What is the Qdrant vector database used for?"


def _settings():
    from retriva.config import settings

    return settings


def test_bedrock_rerank_smoke():
    """Live call against the configured AWS account (eu-central-1,
    cohere.rerank-v3-5:0) using the normal DefaultReranker pipeline."""
    s = _settings()
    if not s.enable_retrieval_reranking:
        pytest.fail("ENABLE_RETRIEVAL_RERANKING must be true for the smoke test")

    region = (
        (s.retrieval_rerank_aws_region or "").strip().lower()
        or os.environ.get("AWS_REGION", "").strip().lower()
        or os.environ.get("AWS_DEFAULT_REGION", "").strip().lower()
    )
    if region != SMOKE_REGION:
        pytest.fail(
            f"Smoke test requires RETRIEVAL_RERANK_AWS_REGION={SMOKE_REGION} "
            f"(got: '{region or ''}')."
        )

    model = (s.retrieval_rerank_model or "").strip()
    if model != SMOKE_MODEL:
        pytest.fail(
            f"Smoke test requires RETRIEVAL_RERANK_MODEL={SMOKE_MODEL} "
            f"(got: '{model}')."
        )

    reset_provider_cache()
    reranker_health.reset()

    chunks = [dict(c, _score=0.1 * (len(SYNTHETIC_CHUNKS) - i)) for i, c in enumerate(SYNTHETIC_CHUNKS)]
    result = DefaultReranker().rerank(QUERY, chunks, top_n=3)

    snap = reranker_health.snapshot()
    if snap["status"] == "error":
        if snap["last_error_category"] == "account_verification_pending":
            pytest.fail(
                "AWS account verification is pending: the Bedrock Rerank API "
                "returned 'Your account is currently being verified'. The "
                "smoke test cannot proceed until AWS completes verification. "
                "(category=account_verification_pending)"
            )
        if snap["last_error_category"] in (
            "model_not_found", "model_access_error", "marketplace_entitlement_error"
        ):
            pytest.fail(
                f"Model access problem for {SMOKE_MODEL} in {SMOKE_REGION} "
                f"(category={snap['last_error_category']}): subscribe/enable "
                "the model in the Bedrock console (model entitlement) and "
                "verify IAM allows bedrock:Rerank."
            )
        pytest.fail(
            f"Bedrock smoke test failed "
            f"(category={snap['last_error_category']}): {snap['last_error']}"
        )

    assert snap["status"] == "ok"
    assert len(result) == 3
    # The Qdrant document must rank first.
    assert result[0]["page_title"] == "Qdrant overview" or "Qdrant" in result[0]["text"]
    assert result[0]["_rerank_score"] >= 0.0
    assert result[0]["_score"] == result[0]["_rerank_score"]
    assert result[0]["_retrieval_score"] == pytest.approx(0.3)  # original _score preserved