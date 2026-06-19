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

import sys
import os
sys.path.append(os.path.join(os.getcwd(), 'src'))
from retriva.indexing.qdrant_store import get_client, list_documents, COLLECTION_NAME
from qdrant_client.models import Filter, FieldCondition, MatchValue

client = get_client()

hits, _ = client.scroll(
    collection_name=COLLECTION_NAME,
    scroll_filter=Filter(
        must=[
            FieldCondition(key="filename", match=MatchValue(value="A17C5_IT_UG_V2_20250724.pdf"))
        ]
    ),
    limit=5,
    with_payload=True
)

for i, h in enumerate(hits):
    print(f"--- Chunk {i} ---")
    print(h.payload.get('text', '')[:500])
    print("Contains 'apollo'?", 'apollo' in h.payload.get('text', '').lower())

