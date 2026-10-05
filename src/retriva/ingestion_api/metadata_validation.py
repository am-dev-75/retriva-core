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
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.  See the License for the specific language governing
# permissions and limitations under the License.

"""User-metadata validation shared by the supported v2 ingestion
surfaces (relocated from the retired API v1 ``schemas`` module by
Spec 027 / ADR-032)."""

import json
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# User-metadata validation constants
# ---------------------------------------------------------------------------

MAX_METADATA_KEYS = 20
MAX_METADATA_VALUE_LENGTH = 256
MAX_METADATA_SERIALIZED_BYTES = 4096


class UserMetadataValidationError(ValueError):
    """Raised when user_metadata violates hard limits.

    Carries a structured ``details`` list so FastAPI can return a
    descriptive 422 response.
    """

    def __init__(self, details: List[Dict[str, str]]):
        self.details = details
        msgs = "; ".join(d["msg"] for d in details)
        super().__init__(msgs)


def validate_user_metadata(
    value: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Validate user_metadata against hard limits.

    Returns the value unchanged if valid, or raises
    ``UserMetadataValidationError`` with structured details.
    """
    if value is None:
        return value

    errors: List[Dict[str, str]] = []

    # --- type check ---------------------------------------------------------
    for k, v in value.items():
        if not isinstance(k, str):
            errors.append({
                "field": "user_metadata",
                "msg": f"Key {k!r} is not a string",
            })
        # ``kb_ids`` is the only key that may carry a list of strings;
        # all other values must be scalar strings.
        if k == "kb_ids":
            if not isinstance(v, list):
                errors.append({
                    "field": "user_metadata",
                    "msg": f"Value for key {k!r} must be a list of strings (got {type(v).__name__})",
                })
            elif not all(isinstance(item, str) for item in v):
                errors.append({
                    "field": "user_metadata",
                    "msg": f"Value for key {k!r} contains non-string items",
                })
        elif not isinstance(v, str):
            errors.append({
                "field": "user_metadata",
                "msg": f"Value for key {k!r} is not a string (got {type(v).__name__})",
            })

    # --- key count ----------------------------------------------------------
    if len(value) > MAX_METADATA_KEYS:
        errors.append({
            "field": "user_metadata",
            "msg": (
                f"Too many keys: {len(value)} exceeds maximum of "
                f"{MAX_METADATA_KEYS}"
            ),
        })

    # --- per-value length ---------------------------------------------------
    for k, v in value.items():
        if k == "kb_ids" and isinstance(v, list):
            for item in v:
                if isinstance(item, str) and len(item) > MAX_METADATA_VALUE_LENGTH:
                    errors.append({
                        "field": "user_metadata",
                        "msg": (
                            f"Value for key {k!r} item {item!r} is {len(item)} characters, "
                            f"exceeding maximum of {MAX_METADATA_VALUE_LENGTH}"
                        ),
                    })
        elif isinstance(v, str) and len(v) > MAX_METADATA_VALUE_LENGTH:
            errors.append({
                "field": "user_metadata",
                "msg": (
                    f"Value for key {k!r} is {len(v)} characters, "
                    f"exceeding maximum of {MAX_METADATA_VALUE_LENGTH}"
                ),
            })

    # --- total serialized size ----------------------------------------------
    try:
        serialized = json.dumps(value)
        if len(serialized.encode("utf-8")) > MAX_METADATA_SERIALIZED_BYTES:
            errors.append({
                "field": "user_metadata",
                "msg": (
                    f"Serialized metadata is {len(serialized.encode('utf-8'))} bytes, "
                    f"exceeding maximum of {MAX_METADATA_SERIALIZED_BYTES}"
                ),
            })
    except (TypeError, ValueError):
        errors.append({
            "field": "user_metadata",
            "msg": "Metadata is not JSON-serializable",
        })

    if errors:
        raise UserMetadataValidationError(errors)

    return value
