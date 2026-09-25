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
Documentation-validation tests for IAM action names (requirement 5).

The reranking documentation's IAM policies are parsed and validated
against the installed botocore service models:

* every documented action uses the canonical `bedrock:` IAM prefix (never
  an SDK client name like `bedrock-agent-runtime:`);
* every action name exists as an operation in one of the Bedrock-family
  service models (grounded in the SDK, not hardcoded);
* the runtime policy grants only `bedrock:Rerank`;
* `bedrock:InvokeModel` is retained (documented for
  InvokeModel-based transports only);
* Marketplace provisioning permissions are separated from runtime
  permissions.
"""

import json
import re
from pathlib import Path

import pytest
from botocore.session import get_session

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
RERANKING_DOC = DOCS_DIR / "reranking.md"

#: IAM actions are namespaced by the `bedrock` service prefix for ALL
#: Bedrock-family services; SDK client names are NOT IAM namespaces.
IAM_PREFIX = "bedrock"

#: Bedrock-family services whose operation names double as IAM action names.
_BEDROCK_FAMILY_SERVICES = (
    "bedrock",
    "bedrock-runtime",
    "bedrock-agent",
    "bedrock-agent-runtime",
)

_PROVISIONING_ACTIONS = (
    "ListFoundationModelAgreementOffers",
    "CreateFoundationModelAgreement",
    "DeleteFoundationModelAgreement",
    "GetFoundationModelAvailability",
    "PutUseCaseForModelAccess",
    "GetUseCaseForModelAccess",
)


def _known_bedrock_actions() -> set:
    """PascalCase operation names across the Bedrock-family service models."""
    session = get_session()
    known = set()
    for service in _BEDROCK_FAMILY_SERVICES:
        model = session.get_service_model(service)
        known.update(model.operation_names)
    return known


def _iam_actions_in_text(text: str) -> list:
    """Find PascalCase `bedrock:<Action>` occurrences (excludes ARN fields
    like arn:aws:bedrock:REGION)."""
    return re.findall(rf"(?<![\w-]){IAM_PREFIX}:[A-Z][a-z]+(?:[A-Z][a-z]*)*", text)


def _json_policy_blocks(text: str) -> list:
    """Parse every ```json fenced block that looks like an IAM policy."""
    policies = []
    for block in re.findall(r"```json\n(.*?)```", text, flags=re.S):
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "Statement" in parsed:
            policies.append(parsed)
    return policies


@pytest.fixture(scope="module")
def doc_text() -> str:
    return RERANKING_DOC.read_text()


@pytest.fixture(scope="module")
def known_actions() -> set:
    return _known_bedrock_actions()


class TestIamActionNaming:
    def test_all_documented_actions_use_bedrock_prefix(self, doc_text):
        """No SDK client name may leak into an IAM action
        (e.g. bedrock-agent-runtime:Rerank is invalid)."""
        bad = re.findall(r"bedrock[a-z-]*:[A-Z][a-z]+(?:[A-Z][a-z]*)*", doc_text)
        offenders = [a for a in bad if not a.startswith("bedrock:")]
        assert not offenders, f"invalid IAM action prefix(es): {offenders}"

    def test_every_documented_action_exists_in_the_sdk(self, doc_text, known_actions):
        actions = set(_iam_actions_in_text(doc_text))
        assert actions, "no IAM actions found in the reranking doc"
        unknown = sorted(a.split(":", 1)[1] for a in actions if a.split(":", 1)[1] not in known_actions)
        assert not unknown, (
            f"documented IAM action(s) not found in any Bedrock service "
            f"model: {unknown}"
        )


class TestRuntimeVsProvisioningSeparation:
    def test_runtime_policy_grants_only_rerank(self, doc_text, known_actions):
        policies = _json_policy_blocks(doc_text)
        runtime = [p for p in policies if any(
            s.get("Sid") == "RerankRuntime" for s in p["Statement"]
        )]
        assert runtime, "the runtime policy (Sid=RerankRuntime) is missing"
        actions = [
            a for p in runtime for s in p["Statement"] for a in s["Action"]
        ]
        assert actions == ["bedrock:Rerank"]
        assert "bedrock:Rerank" in _iam_actions_in_text(doc_text)

    def test_invoke_model_retained_and_documented(self, doc_text, known_actions):
        """bedrock:InvokeModel must remain documented (for InvokeModel-based
        transports), clearly scoped away from the runtime policy."""
        assert "bedrock:InvokeModel" in _iam_actions_in_text(doc_text)
        # It must NOT appear in the runtime policy statement.
        policies = _json_policy_blocks(doc_text)
        runtime = [p for p in policies if any(
            s.get("Sid") == "RerankRuntime" for s in p["Statement"]
        )]
        runtime_actions = [
            a for p in runtime for s in p["Statement"] for a in s["Action"]
        ]
        assert "bedrock:InvokeModel" not in runtime_actions

    def test_provisioning_permissions_separate_from_runtime(self, doc_text, known_actions):
        policies = _json_policy_blocks(doc_text)
        provisioning = [p for p in policies if any(
            s.get("Sid") == "MarketplaceProvisioningOneTime" for s in p["Statement"]
        )]
        assert provisioning, "the marketplace provisioning policy is missing"
        actions = [
            a for p in provisioning for s in p["Statement"] for a in s["Action"]
        ]
        assert set(a.split(":", 1)[1] for a in actions) == set(_PROVISIONING_ACTIONS)
        assert "bedrock:Rerank" not in actions
        assert "bedrock:InvokeModel" not in actions
        # Runtime actions must not appear in the provisioning policy.
        assert "RerankRuntime" not in str(provisioning)

    def test_provisioning_actions_exist_in_sdk(self, known_actions):
        for action in _PROVISIONING_ACTIONS:
            assert action in known_actions, action