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
    print(f"##### Retriva OpenAI-compatible API ({config.VERSION}) #####\n")
    parser = argparse.ArgumentParser(
        description="Retriva OpenAI-compatible API for Open WebUI"
    )
    parser.add_argument(
        "--host", type=str, default="0.0.0.0", help="Binding host"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=config.settings.openai_api_port,
        help="Binding port (default: %(default)s)",
    )
    args = parser.parse_args()

    s = config.settings
    print("Active settings:")
    print(f"  Chat model:           {s.chat_model}")
    print(f"  Chat base URL:        {s.chat_base_url}")
    print(f"  Chat temperature:     {s.chat_temperature}")
    print(f"  Chat top_p:           {s.chat_top_p}")
    print(f"  Retriever top_k:      {s.retriever_top_k}")
    print(f"  Reranking enabled:    {s.enable_retrieval_reranking}")
    if s.enable_retrieval_reranking:
        print(f"  Rerank model:         {s.retrieval_rerank_model}")
    print(f"  Qdrant URL:           {s.qdrant_url}")
    print(f"  Qdrant Collection:    {s.qdrant_collection_name}")
    print(f"  Embedding model:      {s.embedding_model}")
    print(f"  Embedding dimension:  {s.embedding_dimension}")
    print(f"  Storage path:         {s.storage_path}")
    print(f"  Artifacts path:       {s.artifacts_path}")
    print()
    print(f"Starting OpenAI-compatible API on {args.host}:{args.port}...")
    uvicorn.run(
        "retriva.openai_api.main:app",
        host=args.host,
        port=args.port,
        reload=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()