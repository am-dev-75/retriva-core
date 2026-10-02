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

"""Intent classification transport (Spec 001 Phase D; ADR-0002).

Provider-neutral, immutable-target classifier transport owned by Core.
The Gateway sends a closed classification request to the internal
endpoint; the selected provider adapter performs the provider-specific
invocation; Core returns the strict C2 classification record or a
typed, sanitized error.

Model transport belongs in Core (Spec 001 accepted responsibility
split); Core NEVER selects RAG, the agent loop, clarification, a
workflow, a tool, a confirmation, or an application route — it returns
classification only, and the Gateway owns application workflow policy.

Canonical provider names: ``openrouter`` | ``bedrock`` (aliases
``aws_bedrock`` / ``aws-bedrock`` normalize to ``bedrock``; the
reranker's ``cohere`` alias is domain-specific and is NOT accepted
here).  Aliases normalize before configuration snapshots, adapter
construction, logging, status reporting, and testing.
"""

from .base import (
    ClassifierErrorCode,
    ClassifierRequest,
    IntentClassifierError,
    IntentClassifierTarget,
    IntentClassification,
    canonical_provider_name,
    effective_aws_region,
    build_target_from_settings,
    validate_classifier_settings,
    validate_classification_payload,
)
from .factory import get_intent_classifier
from .prompt import (
    PROMPT_ID,
    PROMPT_VERSION,
    classification_system_prompt,
)

__all__ = [
    "ClassifierErrorCode",
    "ClassifierRequest",
    "IntentClassifierError",
    "IntentClassifierTarget",
    "IntentClassification",
    "canonical_provider_name",
    "effective_aws_region",
    "build_target_from_settings",
    "validate_classifier_settings",
    "validate_classification_payload",
    "get_intent_classifier",
    "PROMPT_ID",
    "PROMPT_VERSION",
    "classification_system_prompt",
]
