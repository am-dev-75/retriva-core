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

import re

COLLECTION_NAME_REGEX = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,62}$")

def validate_collection_name(name: str) -> str:
    """Validate a collection name for use in Qdrant and filesystem paths.

    Raises ValueError if the name is unsafe.
    """
    if not COLLECTION_NAME_REGEX.match(name):
        raise ValueError(
            f"Invalid collection name: {name!r}. "
            "Must match ^[a-zA-Z0-9][a-zA-Z0-9_-]{1,62}$"
        )
    return name
