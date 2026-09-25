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
Cache, credential and client lifecycle tests (requirements 7 and 12).

Covers: alias → single cache identity, fingerprint coverage (behavioral
non-secret settings included, credentials excluded), AWS credentials never
frozen into clients (botocore owns temporary credential refresh),
thread-safe provider reconstruction with instance reuse, boto3 client
construction parameters (timeouts, bounded botocore retries, no manually
passed credentials), and the OpenRouter httpx per-call lifecycle decision.
"""

import threading
import time
from unittest.mock import MagicMock

import httpx
import pytest

from retriva.qa.reranker import DefaultReranker
from retriva.qa.reranking.base import RerankProviderConfig
from retriva.qa.reranking.factory import (
    get_reranker_provider,
    register_rerank_provider,
    reset_provider_cache,
)
from retriva.qa.reranking.health import reranker_health
from retriva.qa.reranking.metrics import reranker_metrics
from retriva.qa.reranking.providers.bedrock import (
    BEDROCK_SERVICE_NAME,
    BedrockRerankProvider,
)


@pytest.fixture(autouse=True)
def _clean_state():
    reset_provider_cache()
    yield
    reset_provider_cache()


def _settings(**overrides):
    from types import SimpleNamespace

    base = dict(
        enable_retrieval_reranking=True,
        retrieval_rerank_provider="",
        retrieval_rerank_model="cohere/rerank-v3.5",
        retrieval_rerank_base_url="https://openrouter.ai/api/v1",
        retrieval_rerank_api_key="k",
        retrieval_rerank_aws_region="eu-central-1",
        retrieval_rerank_timeout=30.0,
        retrieval_rerank_max_retries=2,
        retrieval_rerank_retry_base_delay=1.0,
        retrieval_rerank_enforce_eu_region=False,
        retrieval_rerank_allowed_aws_regions="",
        retrieval_rerank_batch_size=100,
        retrieval_rerank_max_length=4096,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _StubProvider:
    name = "stub"

    def rank(self, query, documents, top_n):
        return [{"index": 0, "relevance_score": 1.0}]


# ---------------------------------------------------------------------------
# Cache identity and thread safety (req 7)
# ---------------------------------------------------------------------------

class TestCacheLifecycle:
    def test_aliases_share_one_cache_identity(self):
        p1 = get_reranker_provider(_settings(retrieval_rerank_provider="bedrock"))
        p2 = get_reranker_provider(_settings(retrieval_rerank_provider="AWS_BEDROCK"))
        p3 = get_reranker_provider(_settings(retrieval_rerank_provider="aws-bedrock"))
        assert p1 is p2 is p3

    def test_provider_instances_are_reused_across_calls(self, monkeypatch):
        monkeypatch.setattr("retriva.qa.reranker.settings", _settings(), raising=False)
        monkeypatch.setattr(
            "retriva.qa.reranker.get_reranker_provider",
            lambda: get_reranker_provider(_settings()),
        )
        reranker = DefaultReranker()
        chunks = [{"text": "a", "page_title": "P"}]

        first = get_reranker_provider(_settings())
        reranker.rerank("q", chunks, 1)
        second = get_reranker_provider(_settings())
        reranker.rerank("q", chunks, 1)
        assert first is second

    def test_reconstruction_is_thread_safe_single_build(self):
        """Concurrent calls after a config change must produce exactly ONE
        build and share one instance."""
        builds = []
        instances = []
        lock = threading.Lock()

        def slow_factory(cfg):
            time.sleep(0.05)
            with lock:
                builds.append(1)
            return _StubProvider()

        register_rerank_provider("threadstub", slow_factory)
        get_reranker_provider(_settings(retrieval_rerank_provider="threadstub"))  # seed
        seeded_builds = len(builds)
        assert seeded_builds == 1

        barrier = threading.Barrier(8)
        instances = []

        def worker():
            barrier.wait()
            p = get_reranker_provider(
                _settings(retrieval_rerank_provider="threadstub", retrieval_rerank_model="changed")
            )
            with lock:
                instances.append(id(p))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(builds) - seeded_builds == 1, (
            f"expected single rebuild, got {len(builds) - seeded_builds}"
        )
        assert len(set(instances)) == 1

    def test_fingerprint_covers_behavioral_settings(self):
        cfg = RerankProviderConfig.from_settings(_settings())
        variants = [
            RerankProviderConfig(provider="bedrock", model="m"),
            RerankProviderConfig(provider="openrouter", model="other"),
            RerankProviderConfig(provider="openrouter", model="m", base_url="https://other"),
            RerankProviderConfig(provider="openrouter", model="m", aws_region="us-east-1"),
            RerankProviderConfig(provider="openrouter", model="m", timeout=1.0),
            RerankProviderConfig(provider="openrouter", model="m", max_retries=9),
            RerankProviderConfig(provider="openrouter", model="m", retry_base_delay=9.0),
            RerankProviderConfig(provider="openrouter", model="m", enforce_eu_region=True),
            RerankProviderConfig(provider="openrouter", model="m", allowed_aws_regions=("eu-central-1",)),
        ]
        for v in variants:
            assert v.fingerprint() != cfg.fingerprint(), v

    def test_fingerprint_excludes_credentials(self):
        cfg = RerankProviderConfig.from_settings(_settings())
        assert cfg.fingerprint() == RerankProviderConfig.from_settings(
            _settings(retrieval_rerank_api_key="rotated-key")
        ).fingerprint()


# ---------------------------------------------------------------------------
# Bedrock client construction and credential lifecycle (req 7, 12)
# ---------------------------------------------------------------------------

class TestBedrockClientLifecycle:
    def test_client_targets_correct_service_without_credentials(self, monkeypatch):
        captured = {}

        def fake_boto3_client(service_name, **kwargs):
            captured["service"] = service_name
            captured.update(kwargs)
            return MagicMock()

        import boto3

        monkeypatch.setattr(boto3, "client", fake_boto3_client)
        provider = BedrockRerankProvider(
            RerankProviderConfig(
                provider="bedrock", model="amazon.rerank-v1:0",
                aws_region="eu-central-1", timeout=7.5, max_retries=3,
            )
        )
        provider._get_client()

        assert captured["service"] == BEDROCK_SERVICE_NAME
        assert captured["region_name"] == "eu-central-1"
        cfg = captured["config"]
        assert cfg.read_timeout == 7.5
        assert cfg.connect_timeout == min(10.0, 7.5)
        # Bounded botocore retries; no multiplying application retry loop.
        assert cfg.retries == {"max_attempts": 3, "mode": "adaptive"}
        # Credentials are NEVER frozen manually — botocore resolves/refreshes.
        assert not any(
            k in captured for k in ("aws_access_key_id", "aws_secret_access_key", "aws_session_token")
        )

    def test_client_reused_across_calls(self, monkeypatch):
        import boto3

        built = []

        def fake_boto3_client(service_name, **kwargs):
            built.append(MagicMock())
            return built[-1]

        monkeypatch.setattr(boto3, "client", fake_boto3_client)
        provider = BedrockRerankProvider(
            RerankProviderConfig(
                provider="bedrock", model="amazon.rerank-v1:0", aws_region="eu-central-1"
            )
        )
        c1 = provider._get_client()
        c2 = provider._get_client()
        assert c1 is c2 and len(built) == 1

    def test_credential_env_change_rebuilds_client(self, monkeypatch):
        import boto3

        built = []

        def fake_boto3_client(service_name, **kwargs):
            built.append(MagicMock())
            return built[-1]

        monkeypatch.setattr(boto3, "client", fake_boto3_client)
        provider = BedrockRerankProvider(
            RerankProviderConfig(
                provider="bedrock", model="amazon.rerank-v1:0", aws_region="eu-central-1"
            )
        )
        provider._get_client()
        monkeypatch.setenv("AWS_SESSION_TOKEN", "rotated-token")
        provider._get_client()
        assert len(built) == 2  # rebuilt after credential rotation


# ---------------------------------------------------------------------------
# OpenRouter httpx lifecycle (req 12)
# ---------------------------------------------------------------------------

class TestOpenRouterClientLifecycle:
    def test_httpx_client_gets_configured_timeout(self, monkeypatch):
        """Documented decision: OpenRouter keeps a per-call httpx.Client
        (created with the configured timeout, closed after each call).
        Rationale in docs/reranking.md 'Client lifecycle'."""
        from retriva.qa.reranker import _call_rerank_api

        s = _settings(retrieval_rerank_timeout=12.5)
        monkeypatch.setattr("retriva.qa.reranker.settings", s, raising=False)

        created = []
        outer = MagicMock()
        inner = outer.__enter__.return_value
        inner.post.return_value.json.return_value = {"results": []}

        def fake_client(timeout=None):
            created.append(timeout)
            return outer

        monkeypatch.setattr("retriva.qa.reranker.httpx.Client", fake_client)
        _call_rerank_api("q", ["doc"], 1)

        assert created == [12.5]
        assert inner.post.call_count == 1

    def test_no_unbounded_thread_pool_in_reranking(self):
        """The reranking path introduces no threads/thread pools: retrieval
        runs synchronously on the caller's (Starlette-bounded) thread."""
        import inspect

        import retriva.qa.reranker as module

        source = inspect.getsource(module)
        assert "ThreadPool" not in source
        assert "Thread(" not in source