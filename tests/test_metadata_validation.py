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

"""
Unit tests for the shared user-metadata validation surface
(``retriva.ingestion_api.metadata_validation``), relocated from the
retired API v1 ``schemas`` module by Spec 027. The v2 ingestion
validators (``schemas_v2``) call this same validator, so these tests
cover the hard-limit behavior exercised by every supported surface.

Covers:
- Backward compatibility (no metadata → None, no crash)
- Hard-limit validation (key count, value length, serialized size)
- Non-string rejection and structured error details
"""

import pytest

from retriva.ingestion_api.metadata_validation import (
    MAX_METADATA_KEYS,
    MAX_METADATA_SERIALIZED_BYTES,
    MAX_METADATA_VALUE_LENGTH,
    UserMetadataValidationError,
    validate_user_metadata,
)

SAMPLE_METADATA = {"author": "Alice", "version": "2.0"}


class TestBackwardCompatibility:
    def test_none_passthrough(self):
        assert validate_user_metadata(None) is None

    def test_empty_dict_passthrough(self):
        assert validate_user_metadata({}) == {}


class TestAcceptsValidMetadata:
    def test_simple_valid_metadata(self):
        assert validate_user_metadata(SAMPLE_METADATA) == SAMPLE_METADATA

    def test_kb_ids_list_accepted(self):
        md = {"kb_ids": ["kb-a", "kb-b"]}
        assert validate_user_metadata(md) == md

    def test_max_values_accepted(self):
        # Just inside every limit: 20 keys, 256-char values, serialized
        # size below the 4096-byte cap.
        md = {f"k{i}": "v" * MAX_METADATA_VALUE_LENGTH for i in range(15)}
        validate_user_metadata(md)  # must not raise


class TestValidationRejection:
    def _validate(self, md):
        return validate_user_metadata(md)

    def test_non_string_value_rejected(self):
        with pytest.raises(UserMetadataValidationError):
            self._validate({"key": 123})

    def test_non_string_key_rejected(self):
        with pytest.raises(UserMetadataValidationError):
            self._validate({1: "v"})

    def test_kb_ids_non_list_rejected(self):
        with pytest.raises(UserMetadataValidationError):
            self._validate({"kb_ids": "not-a-list"})

    def test_kb_ids_non_string_items_rejected(self):
        with pytest.raises(UserMetadataValidationError):
            self._validate({"kb_ids": [1, 2]})

    def test_too_many_keys_rejected(self):
        metadata = {f"k{i}": "v" for i in range(MAX_METADATA_KEYS + 5)}
        with pytest.raises(UserMetadataValidationError) as exc:
            self._validate(metadata)
        msgs = "; ".join(d["msg"] for d in exc.value.details)
        assert "exceeds maximum" in msgs

    def test_value_too_long_rejected(self):
        with pytest.raises(UserMetadataValidationError) as exc:
            self._validate({"k": "v" * (MAX_METADATA_VALUE_LENGTH + 1)})
        assert any("exceeding maximum" in d["msg"] for d in exc.value.details)

    def test_kb_ids_item_too_long_rejected(self):
        with pytest.raises(UserMetadataValidationError):
            self._validate({"kb_ids": ["x" * (MAX_METADATA_VALUE_LENGTH + 1)]})

    def test_serialized_size_rejected(self):
        metadata = {f"k{i}": "v" * 900 for i in range(6)}
        assert len(__import__("json").dumps(metadata).encode("utf-8")) > MAX_METADATA_SERIALIZED_BYTES
        with pytest.raises(UserMetadataValidationError) as exc:
            self._validate(metadata)
        assert any("Serialized metadata" in d["msg"] for d in exc.value.details)

    def test_unserializable_value_rejected(self):
        with pytest.raises(UserMetadataValidationError):
            self._validate({"k": object()})
