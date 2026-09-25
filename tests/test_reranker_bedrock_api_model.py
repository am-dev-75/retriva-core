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
CI tests for the exact AWS API used by the Bedrock rerank provider.

These tests inspect the INSTALLED botocore service model (bundled JSON,
no network) and FAIL if the SDK does not expose the Rerank operation with
the shapes Retriva depends on. They are not mocked-client tests: a broken
or outdated SDK fails here before any runtime fallback can hide it.

Confirmed API (see the final report and docs/reranking.md):

* client:   bedrock-agent-runtime  (service ID 'Bedrock Agent Runtime')
* operation: Rerank
* request:  queries, sources, rerankingConfiguration (with an explicit
            numberOfResults inside bedrockRerankingConfiguration)
* response: results[].index, results[].relevanceScore

botocore 1.35.72 introduced the operation (botocore and boto3 are
released in lockstep, so boto3 >= 1.35.72 too).
"""

import re
from pathlib import Path

import boto3
import botocore
import pytest
from botocore.session import get_session

from retriva.qa.reranking.providers.bedrock import (
    BEDROCK_RERANK_MIN_SDK_VERSION,
    BEDROCK_SERVICE_NAME,
    verify_rerank_operation_available,
)

REQUIREMENTS_TXT = Path(__file__).resolve().parent.parent / "requirements.txt"


def _version_tuple(version: str):
    return tuple(int(p) for p in re.findall(r"\d+", version)[:3])


def test_rerank_operation_lives_in_bedrock_agent_runtime():
    """The Rerank operation must exist on bedrock-agent-runtime."""
    model = get_session().get_service_model(BEDROCK_SERVICE_NAME)
    assert BEDROCK_SERVICE_NAME == "bedrock-agent-runtime"
    assert "Rerank" in model.operation_names


def test_rerank_not_on_bedrock_runtime_client():
    """Guard the exact-client choice: bedrock-runtime has NO rerank op.

    An implementation targeting boto3.client('bedrock-runtime').rerank
    would crash at runtime (AttributeError) on every current SDK version.
    """
    model = get_session().get_service_model("bedrock-runtime")
    assert "Rerank" not in model.operation_names


def test_rerank_request_shapes():
    model = get_session().get_service_model(BEDROCK_SERVICE_NAME)
    op = model.operation_model("Rerank")
    members = op.input_shape.members
    assert set(members) >= {"queries", "sources", "rerankingConfiguration"}
    reranking = members["rerankingConfiguration"]
    assert "type" in reranking.members
    brc = reranking.members["bedrockRerankingConfiguration"]
    assert "numberOfResults" in brc.members  # set explicitly by the provider
    assert "modelConfiguration" in brc.members
    mc = brc.members["modelConfiguration"]
    assert "modelArn" in mc.members


def test_rerank_response_shapes():
    model = get_session().get_service_model(BEDROCK_SERVICE_NAME)
    op = model.operation_model("Rerank")
    output = op.output_shape.members
    assert "results" in output
    result_members = output["results"].member.members
    assert "index" in result_members
    assert "relevanceScore" in result_members


def test_declared_errors_include_throttling_and_access_denied():
    """The classifier's expected AWS error codes must be declared by the op."""
    model = get_session().get_service_model(BEDROCK_SERVICE_NAME)
    op = model.operation_model("Rerank")
    declared = {e.name for e in op.error_shapes}
    assert {"ThrottlingException", "AccessDeniedException",
            "ValidationException", "ResourceNotFoundException"} <= declared


def test_verify_helper_passes_on_installed_sdk():
    verify_rerank_operation_available()


def test_sdk_version_meets_declared_minimum():
    """The installed SDK must be at least the declared minimum (1.35.72),
    which is the first botocore containing Rerank."""
    installed = botocore.__version__
    assert _version_tuple(installed) >= _version_tuple(BEDROCK_RERANK_MIN_SDK_VERSION), (
        f"botocore {installed} predates the Rerank operation "
        f"(minimum {BEDROCK_RERANK_MIN_SDK_VERSION})"
    )
    # boto3 must not lag botocore (they release in lockstep).
    assert _version_tuple(boto3.__version__) >= _version_tuple(
        BEDROCK_RERANK_MIN_SDK_VERSION
    )


def test_requirements_declare_minimum_sdk():
    text = REQUIREMENTS_TXT.read_text()
    match = re.search(r"boto3>=([0-9.]+)", text)
    assert match, "requirements.txt must pin boto3>=<version>"
    declared = _version_tuple(match.group(1))
    assert declared >= _version_tuple(BEDROCK_RERANK_MIN_SDK_VERSION), (
        "requirements.txt boto3 floor must be at least the first SDK with "
        "the Rerank operation (1.35.72)"
    )