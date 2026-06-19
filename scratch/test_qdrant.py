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
docs = list_documents(client)
for d in docs:
    print(f"Doc: {d.get('filename')}, User metadata: {d.get('user_metadata')}")

# Try filtering
hits, _ = client.scroll(
    collection_name=COLLECTION_NAME,
    scroll_filter=Filter(
        must=[
            FieldCondition(key="user_metadata.project", match=MatchValue(value="apollo"))
        ]
    ),
    limit=10,
    with_payload=True
)
print(f"\nFiltered hits: {len(hits)}")
for h in hits:
    print(h.payload.get('filename'))

