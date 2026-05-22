#!/usr/bin/env python3
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

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Ensure src is in the python path
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root / "src"))

# Force load the .env from the project root BEFORE importing settings
load_dotenv(project_root / ".env")

from qdrant_client import QdrantClient
from retriva.config import settings

def main():
    print(f"Connecting to Qdrant at {settings.qdrant_url}...")
    client = QdrantClient(url=settings.qdrant_url)
    
    collection_name = settings.qdrant_collection_name
    print(f"Deleting Qdrant collection: {collection_name}")
    try:
        client.delete_collection(collection_name=collection_name)
        print("Collection deleted successfully (if it existed).")
    except Exception as e:
        print(f"Failed to delete collection: {e}")

    storage_dir = Path(settings.storage_path)
    files_to_delete = [
        storage_dir / "dedup_catalog.json",
        storage_dir / "registry.db",
    ]

    for f in files_to_delete:
        if f.exists():
            f.unlink()
            print(f"Deleted {f}")
        else:
            print(f"{f} not found.")

if __name__ == "__main__":
    main()
