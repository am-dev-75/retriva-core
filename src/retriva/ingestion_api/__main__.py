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

import uvicorn
import argparse
from retriva.logger import setup_logging
from retriva import config

def main():
    setup_logging()
    print(f"##### Retriva RAG backend ({config.VERSION}) #####\n")
    parser = argparse.ArgumentParser(description="Retriva RAG backend")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Binding host")
    parser.add_argument("--port", type=int, default=config.settings.ingestion_api_port, help="Binding port (default: %(default)s)")
    args = parser.parse_args()
    
    s = config.settings
    print("Active settings:")
    print(f"  Qdrant URL:           {s.qdrant_url}")
    print(f"  Qdrant Collection:    {s.qdrant_collection_name}")
    print(f"  Storage path:         {s.storage_path}")
    print(f"  Primary parser:       {s.v2_primary_parser}")
    print(f"  Embedding model:      {s.embedding_model}")
    print(f"  Embedding dimension:  {s.embedding_dimension}")
    print(f"  Embedding base URL:   {s.embedding_base_url}")
    print(f"  Max chunk chars:      {s.max_chunk_chars}")
    print(f"  Chunk overlap:        {s.chunk_overlap}")
    print(f"  Indexing batch size:  {s.indexing_batch_size}")
    print()
    print(f"Starting API server on {args.host}:{args.port}...")
    uvicorn.run(
        "retriva.ingestion_api.main:app",
        host=args.host,
        port=args.port,
        reload=False,
        log_config=None,
    )

if __name__ == "__main__":
    main()