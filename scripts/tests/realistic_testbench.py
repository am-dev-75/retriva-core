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

"""
Realistic Testbench: Simulates concurrent ingestion and chat traffic against Retriva.
"""

import argparse
import logging
import mimetypes
import random
import subprocess
import threading
import time
import sys
from pathlib import Path
from typing import List, Tuple
import requests

# Ensure mimetypes recognizes markdown
mimetypes.add_type("text/markdown", ".md")
mimetypes.add_type("text/markdown", ".markdown")

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("testbench")

SMOKE_TEST_QA_PATH = Path(__file__).resolve().parent / "smoke_test_qa.py"

def get_all_files(folder: Path) -> List[Path]:
    files = [p for p in folder.rglob("*") if p.is_file()]
    return files

def ingestion_worker(
    worker_id: int, 
    folder: Path, 
    api_url: str, 
    stop_event: threading.Event, 
    results: List[float], 
    files: List[Path]
):
    while not stop_event.is_set():
        if not files:
            time.sleep(1)
            continue
            
        file_to_ingest = random.choice(files)
        content_type, _ = mimetypes.guess_type(str(file_to_ingest))
        if not content_type:
            content_type = "application/octet-stream"

        payload = {
            "source_uri": str(file_to_ingest.resolve()),
            "content_type": content_type
        }

        start_time = time.time()
        try:
            r = requests.post(f"{api_url}/api/v2/documents", json=payload, timeout=60)
            r.raise_for_status()
        except Exception as e:
            logger.debug(f"Ingestion worker {worker_id} failed: {e}")
        finally:
            elapsed = time.time() - start_time
            results.append(elapsed)

def chat_worker(
    worker_id: int, 
    stop_event: threading.Event, 
    results: List[float]
):
    while not stop_event.is_set():
        start_time = time.time()
        try:
            subprocess.run(
                [sys.executable, str(SMOKE_TEST_QA_PATH), "-r"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True
            )
        except Exception as e:
            logger.debug(f"Chat worker {worker_id} failed: {e}")
        finally:
            elapsed = time.time() - start_time
            results.append(elapsed)

def print_stats(name: str, times: List[float]):
    count = len(times)
    if count == 0:
        print(f"{name:<20} | {0:<8} | {'N/A':<10} | {'N/A':<10} | {'N/A':<10} | {'N/A':<10}")
        return
    
    t_min = min(times)
    t_max = max(times)
    t_avg = sum(times) / count
    t_mse = sum((t - t_avg) ** 2 for t in times) / count

    print(f"{name:<20} | {count:<8} | {t_min:<10.3f} | {t_max:<10.3f} | {t_avg:<10.3f} | {t_mse:<10.3f}")

def main():
    parser = argparse.ArgumentParser(description="Retriva Realistic Testbench")
    parser.add_argument("-i", "--ingestion-threads", type=int, default=1, help="Number of concurrent ingestion threads (default 1)")
    parser.add_argument("-c", "--chat-threads", type=int, default=10, help="Number of concurrent chatting threads (default 10)")
    parser.add_argument("-t", "--timeout", type=int, default=30, help="How long the testbench has to run in seconds (default 30)")
    parser.add_argument("-d", "--docs-dir", type=str, required=True, help="Folder from which to pick random files for ingestion")
    parser.add_argument("--api-url", type=str, default="http://127.0.0.1:8000", help="Retriva API URL (default: http://127.0.0.1:8000)")

    args = parser.parse_args()

    docs_dir = Path(args.docs_dir)
    if not docs_dir.is_dir():
        logger.error(f"Directory {docs_dir} does not exist or is not a directory.")
        return

    files = get_all_files(docs_dir)
    if not files:
        logger.warning(f"No files found in {docs_dir}. Ingestion threads will do nothing.")

    logger.info(f"Starting testbench with {args.ingestion_threads} ingestion threads and {args.chat_threads} chat threads.")
    logger.info(f"Running for {args.timeout} seconds...")

    stop_event = threading.Event()
    ingestion_results: List[float] = []
    chat_results: List[float] = []

    threads = []

    # Start ingestion threads
    for i in range(args.ingestion_threads):
        t = threading.Thread(
            target=ingestion_worker, 
            args=(i, docs_dir, args.api_url, stop_event, ingestion_results, files)
        )
        t.start()
        threads.append(t)

    # Start chat threads
    for i in range(args.chat_threads):
        t = threading.Thread(
            target=chat_worker, 
            args=(i, stop_event, chat_results)
        )
        t.start()
        threads.append(t)

    # Wait for timeout
    time.sleep(args.timeout)
    
    logger.info("Timeout expired. Waiting for existing threads to complete...")
    stop_event.set()

    for t in threads:
        t.join()

    logger.info("All threads completed. Generating report...\n")

    print("-" * 85)
    print(f"{'Worker Type':<20} | {'Executed':<8} | {'Min (s)':<10} | {'Max (s)':<10} | {'Avg (s)':<10} | {'MSE':<10}")
    print("-" * 85)
    print_stats("Ingestion Threads", ingestion_results)
    print_stats("Chatting Threads", chat_results)
    print("-" * 85)

if __name__ == "__main__":
    main()
