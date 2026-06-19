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
from retriva.qa.retriever import retrieve_top_chunks
from retriva.indexing.embeddings import get_embeddings
from retriva.indexing.qdrant_store import search_chunks, get_client

client = get_client()
query = "Elenca tutti i documenti che conosci che parlano di apollo."
q_vec = get_embeddings([query])[0]

chunks = search_chunks(client, q_vec, 20)
print(f"Total chunks retrieved: {len(chunks)}")

doc_counts = {}
for c in chunks:
    fname = c.get('filename', 'Unknown')
    doc_counts[fname] = doc_counts.get(fname, 0) + 1
    
for k, v in doc_counts.items():
    print(f"{k}: {v} chunks")
