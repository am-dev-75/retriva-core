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

import asyncio
from retriva.indexing.qdrant_store import get_client, search_chunks
from retriva.indexing.embeddings import get_embeddings
from retriva.qa.answerer import _limit_chunks_by_citations

async def main():
    client = get_client()
    
    en_query = "What is the maximum power consumption of AURA SOM?"
    en_vec = get_embeddings([en_query])[0]
    en_chunks = search_chunks(client, en_vec, retriever_top_k=20)

    limited = _limit_chunks_by_citations(en_chunks, 25)
    
    for i, c in enumerate(limited):
        title = c.get('page_title', '')
        if "Operational characteristics" in title:
            text = c.get('text', '')
            if "1.75" in text: print("1.75W chunk survived!")
            if "2.75" in text: print("2.75W chunk survived!")
            print(f"Survived chunk length: {len(text)}")

if __name__ == "__main__":
    asyncio.run(main())
