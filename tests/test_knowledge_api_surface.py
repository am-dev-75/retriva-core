# Copyright (C) 2026 Retriva.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.  See the License for the specific language governing
# permissions and limitations under the License.

"""Spec 028 P9: public API surface and OpenAPI synchronization."""

from __future__ import annotations

import pytest

from retriva.ingestion_api.main import app


@pytest.fixture(scope="module")
def spec():
    return app.openapi()


def test_ingest_response_v2_additive_optional_fields(spec):
    props = spec["components"]["schemas"]["IngestResponseV2"]["properties"]
    for field in ("document_id", "version_id", "ingestion_id",
                  "sync_state"):
        assert field in props
    required = spec["components"]["schemas"]["IngestResponseV2"].get(
        "required", [])
    for field in ("document_id", "version_id", "ingestion_id",
                  "sync_state"):
        assert field not in required  # optional during transition


def test_capabilities_readiness_object(spec):
    cap = spec["paths"]["/api/v2/capabilities"]["get"]
    ref = cap["responses"]["200"]["content"]["application/json"][
        "schema"]["$ref"]
    name = ref.split("/")[-1]
    props = spec["components"]["schemas"][name]["properties"]
    assert "knowledge_metadata" in props
    km = props["knowledge_metadata"]
    if "$ref" in km:
        km_name = km["$ref"].split("/")[-1]
        km_props = spec["components"]["schemas"][km_name]["properties"]
    else:
        km_props = km["properties"]
    assert set(km_props) == {"state", "authoritative",
                             "native_ingestion_available"}


def test_no_operator_commands_or_v1_public(spec):
    paths = spec["paths"]
    for path in paths:
        lowered = path.lower()
        assert "/api/v1" not in lowered
        assert "knowledge/adopt" not in lowered
        assert "purge" not in lowered
        assert "reconcile" not in lowered


def test_readiness_model_rejects_invalid_combinations():
    from retriva.ingestion_api.routers.v2_discovery import (
        KnowledgeMetadataReadiness,
    )

    ok = KnowledgeMetadataReadiness(
        state="authoritative", authoritative=True,
        native_ingestion_available=True)
    assert ok.authoritative is True
    with pytest.raises(ValueError):
        KnowledgeMetadataReadiness(
            state="authoritative", authoritative=False,
            native_ingestion_available=False)
    with pytest.raises(ValueError):
        KnowledgeMetadataReadiness(
            state="suspended", authoritative=False,
            native_ingestion_available=True)
