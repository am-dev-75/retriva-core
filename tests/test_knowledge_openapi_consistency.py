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

"""Spec 028 §17: deterministic runtime-vs-tracked OpenAPI consistency.

Repository evidence (scripts/, Makefile, pyproject, CI) shows NO
canonical OpenAPI generator/export workflow: `docs/openapi.yaml` is a
hand-maintained platform document (a superset that also describes
Pro/CRM paths not present in the Core-only runtime app).  Per the
task's Disposition B, this test provides deterministic semantic
consistency checking without introducing a generator.

Scope: the Spec 028 public surface (the additive IngestResponseV2
fields and the `knowledge_metadata` readiness object) must be
identical in the runtime schema and the tracked document, no operator
command or API v1 may be public, and a second serialization must be
byte-identical.
"""

from __future__ import annotations

import json

import pytest
import yaml

from retriva.ingestion_api.main import app

# Runtime Core paths known to be absent from the hand-maintained
# platform document BEFORE Spec 028 (pre-existing drift, unrelated to
# this change).  Do not expand this list for Spec 028 work.
_PREEXISTING_UNTRACKED = {
    "/api/v2/jobs/{job_id}/cancel",
}

_KNOWLEDGE_OPTIONAL_FIELDS = (
    "document_id", "version_id", "ingestion_id", "sync_state")


def _runtime_spec():
    return app.openapi()


def _tracked_spec():
    with open("docs/openapi.yaml", "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_runtime_serialization_is_deterministic():
    first = json.dumps(_runtime_spec(), sort_keys=True)
    second = json.dumps(_runtime_spec(), sort_keys=True)
    assert first == second  # no dict-order or timestamp drift


def test_every_runtime_path_is_tracked_except_preexisting_drift():
    rt = set(_runtime_spec()["paths"])
    tracked = set(_tracked_spec()["paths"])
    missing = rt - tracked - _PREEXISTING_UNTRACKED
    assert not missing, f"runtime paths missing from tracked doc: {missing}"


def test_ingest_response_optional_fields_consistent():
    rt = _runtime_spec()["components"]["schemas"]["IngestResponseV2"]
    tracked = _tracked_spec()["components"]["schemas"]["IngestResponseV2"]
    for field in _KNOWLEDGE_OPTIONAL_FIELDS:
        assert field in rt["properties"]
        assert field in tracked["properties"]
        # Optional on both sides (compatible transition).
        assert field not in rt.get("required", [])
        assert field not in tracked.get("required", [])


def test_capabilities_readiness_object_consistent():
    rt = _runtime_spec()
    tracked = _tracked_spec()

    rt_cap = rt["paths"]["/api/v2/capabilities"]["get"]
    rt_ref = rt_cap["responses"]["200"]["content"]["application/json"][
        "schema"]["$ref"]
    rt_name = rt_ref.split("/")[-1]
    rt_props = rt["components"]["schemas"][rt_name]["properties"]

    t_cap = tracked["paths"]["/api/v2/capabilities"]["get"]
    t_schema = t_cap["responses"]["200"]["content"]["application/json"][
        "schema"]
    t_props = t_schema["properties"]

    assert "knowledge_metadata" in rt_props
    assert "knowledge_metadata" in t_props

    rt_km = rt_props["knowledge_metadata"]
    rt_km_name = rt_km["$ref"].split("/")[-1]
    rt_km_props = set(
        rt["components"]["schemas"][rt_km_name]["properties"])
    t_km = t_props["knowledge_metadata"]
    t_km_props = set(
        (t_km.get("properties")
         or tracked["components"]["schemas"][
             t_km["$ref"].split("/")[-1]]["properties"]))
    assert rt_km_props == {"state", "authoritative",
                           "native_ingestion_available"}
    assert t_km_props == rt_km_props


def test_no_operator_or_v1_surface_public():
    rt_paths = set(_runtime_spec()["paths"])
    for path in rt_paths:
        lowered = path.lower()
        assert "/api/v1" not in lowered
        assert "purge" not in lowered
        assert "reconcile" not in lowered
        assert "knowledge/adopt" not in lowered
