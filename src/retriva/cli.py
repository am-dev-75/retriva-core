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

"""Retriva CLI — supported v2 surfaces only.

Retriva API v1 was removed by Spec 027 / ADR-032. Every command
routes to the supported v2 API; retired operations (standalone image
ingestion, collection clear / reindex) fail locally with bounded
guidance and never make an HTTP request. Standalone image ingestion
has no v2 equivalent yet — v2-native image ingestion is deferred
governed work.
"""

import argparse
from pathlib import Path
from typing import Callable, Dict, Set

import requests

from retriva import config
from retriva.ingestion.discover import discover_files, FILE_TYPE_REGISTRY
from retriva.logger import setup_logging, get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Per-type ingestion handlers
# ---------------------------------------------------------------------------
# To support a new format, add a handler here and a matching entry
# in discover.py's FILE_TYPE_REGISTRY.
# ---------------------------------------------------------------------------

def ingest_html_file(path: str, api_url: str) -> None:
    """Read an HTML file and POST it to the supported v2 ingestion API."""
    payload = {"source_uri": str(Path(path).resolve()), "content_type": "text/html"}
    try:
        r = requests.post(f"{api_url}/api/v2/documents", json=payload)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Error uploading HTML via v2 {path}: {e}")


def ingest_image_file(path: str, api_url: str) -> None:
    """Standalone image ingestion is retired (Spec 027 / ADR-032).

    Retriva API v1 — which carried image ingestion — was removed, and
    no v2-native image surface exists yet (deferred governed work).
    This handler fails locally with a bounded message and makes NO
    HTTP request."""
    logger.error(
        f"Image ingestion retired: standalone image ingestion was "
        f"removed together with Retriva API v1 (Spec 027 / ADR-032) "
        f"and no v2-native image surface exists yet. File skipped: "
        f"{path}")


def ingest_text_file(path: str, api_url: str) -> None:
    """Read a plain-text file and POST it to the supported v2 ingestion API."""
    payload = {"source_uri": str(Path(path).resolve()), "content_type": "text/plain"}
    try:
        r = requests.post(f"{api_url}/api/v2/documents", json=payload)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Error uploading text via v2 {path}: {e}")


def ingest_pdf_file(path: str, api_url: str) -> None:
    """Wrapper to integrate PDF ingestion into the generic discovery flow."""
    run_pdf_ingest(Path(path), api_url, limit=0)

def ingest_markdown_file(path: str, api_url: str) -> None:
    """Wrapper to integrate Markdown ingestion into the generic discovery flow."""
    run_markdown_ingest(Path(path), api_url, limit=0)

# Maps file type keys (from FILE_TYPE_REGISTRY) to handler functions.
INGEST_HANDLERS: Dict[str, Callable[[str, str], None]] = {
    "html": ingest_html_file,
    "image": ingest_image_file,
    "text": ingest_text_file,
    "pdf": ingest_pdf_file,
    "markdown": ingest_markdown_file,
}


# ---------------------------------------------------------------------------
# Core ingestion logic
# ---------------------------------------------------------------------------

def run_ingest(
    target: Path,
    api_url: str,
    limit: int = 0,
    exclude: Set[str] | None = None,
) -> None:
    """
    Discover and ingest all supported files under *target* via the
    supported v2 API (Retriva API v1 was removed by Spec 027).
    *target* may be a single file or a directory.

    Args:
        exclude: File-type keys to skip (e.g. {"image"}).
    """
    logger.info(f"Discovering files in '{target}'...")
    discovered = discover_files(target)

    # Remove excluded types before processing
    if exclude:
        for type_key in exclude:
            removed = discovered.pop(type_key, [])
            if removed:
                logger.info(f"Excluding {len(removed)} {type_key} file(s).")

    if not any(discovered.values()):
        logger.warning("No supported files found.")
        return

    total = 0
    for file_type, files in discovered.items():
        handler = INGEST_HANDLERS.get(file_type)
        if handler is None:
            logger.warning(f"No handler for type '{file_type}' — skipping {len(files)} file(s).")
            continue

        for path in files:
            if 0 < limit <= total:
                logger.info(f"Reached limit ({limit}). Stopping.")
                return
            logger.info(f"[{file_type}] Uploading {path}...")
            handler(path, api_url)
            total += 1

    logger.info(f"Ingestion complete — {total} file(s) processed.")


# ---------------------------------------------------------------------------
# MediaWiki export injector
# ---------------------------------------------------------------------------

def run_mediawiki_ingest(
    target: Path,
    api_url: str,
    limit: int = 0,
) -> None:
    """
    Discover and ingest MediaWiki XML export files under *target* via
    the supported v2 staged-dir submission (Retriva API v1 — the
    page-by-page path — was removed by Spec 027).

    1. Walk *target* for ``*.xml`` files validated as MediaWiki exports.
    2. Submit the staged directory to the durable v2 mediawiki flow and
       poll the durable job to completion.
    """
    from retriva.ingestion.mediawiki_export_parser import is_mediawiki_export
    import time

    # --- 1. Discover XML files ---
    xml_files: list[Path] = []
    if target.is_file():
        if target.suffix.lower() == ".xml" and is_mediawiki_export(target):
            xml_files.append(target)
        else:
            logger.error(f"'{target}' is not a valid MediaWiki XML export.")
            return
    else:
        for path in sorted(target.rglob("*.xml")):
            if is_mediawiki_export(path):
                xml_files.append(path)

    if not xml_files:
        logger.warning(f"No MediaWiki XML export files found under '{target}'.")
        return

    logger.info(f"Found {len(xml_files)} MediaWiki XML export file(s).")

    # --- 2. Submit the staged directory to the durable v2 flow ---
    logger.info(f"Submitting MediaWiki export directory to v2 API: {target.absolute()}")
    payload = {"staged_dir": str(target.absolute())}
    try:
        r = requests.post(f"{api_url}/api/v2/documents/mediawiki", json=payload)
        r.raise_for_status()
        resp = r.json()
        job_id = resp["job_id"]
        logger.info(f"[mediawiki] v2 Job accepted: {job_id}")

        # Poll job status
        while True:
            time.sleep(2)
            r_status = requests.get(f"{api_url}/api/v2/jobs/{job_id}")
            if not r_status.ok:
                logger.warning("Could not fetch job status. Stopping polling.")
                break
            status_data = r_status.json()
            state = status_data["status"]
            stage = status_data.get("current_stage", "unknown")
            logger.info(f"Job {job_id} status: {state} (stage: {stage})")
            if state in ("completed", "failed", "cancelled"):
                break
    except Exception as e:
        logger.error(f"Error submitting to v2 API: {e}")


# ---------------------------------------------------------------------------
# PDF injector
# ---------------------------------------------------------------------------

def run_pdf_ingest(
    target: Path,
    api_url: str,
    limit: int = 0,
) -> None:
    """
    Discover and ingest PDF files under *target* via the supported v2
    API (Retriva API v1 was removed by Spec 027).

    1. Walk *target* for ``*.pdf`` files.
    2. POST each file to the durable v2 document surface by
       ``source_uri``.
    """
    # --- 1. Discover PDF files ---
    pdf_files: list[Path] = []
    if target.is_file():
        if target.suffix.lower() == ".pdf":
            pdf_files.append(target)
        else:
            logger.error(f"'{target}' is not a PDF file.")
            return
    else:
        pdf_files = sorted(target.rglob("*.pdf"))

    if not pdf_files:
        logger.warning(f"No PDF files found under '{target}'.")
        return

    logger.info(f"Found {len(pdf_files)} PDF file(s).")

    # --- 2. Upload via the durable v2 document surface ---
    total = 0
    for pdf_path in pdf_files:
        if 0 < limit <= total:
            logger.info(f"Reached limit ({limit}). Stopping.")
            return

        payload = {"source_uri": str(pdf_path.resolve()), "content_type": "application/pdf"}
        try:
            r = requests.post(f"{api_url}/api/v2/documents", json=payload)
            r.raise_for_status()
            total += 1
            logger.info(f"[pdf] Uploaded '{pdf_path.stem}' via v2")
        except Exception as e:
            logger.error(f"Error uploading '{pdf_path.stem}' via v2: {e}")

    logger.info(f"PDF ingestion complete — {total} doc(s) processed.")


# ---------------------------------------------------------------------------
# Markdown injector
# ---------------------------------------------------------------------------

def run_markdown_ingest(
    target: Path,
    api_url: str,
    limit: int = 0,
) -> None:
    """
    Discover and ingest Markdown files under *target* via the
    supported v2 API (Retriva API v1 was removed by Spec 027).

    1. Walk *target* for ``*.md`` and ``*.markdown`` files.
    2. POST each file to the durable v2 document surface by
       ``source_uri``.
    """
    # --- 1. Discover Markdown files ---
    md_files: list[Path] = []
    if target.is_file():
        if target.suffix.lower() in (".md", ".markdown"):
            md_files.append(target)
        else:
            logger.error(f"'{target}' is not a Markdown file.")
            return
    else:
        md_files = sorted(
            [p for p in target.rglob("*") if p.suffix.lower() in (".md", ".markdown")]
        )

    if not md_files:
        logger.warning(f"No Markdown files found under '{target}'.")
        return

    logger.info(f"Found {len(md_files)} Markdown file(s).")

    # --- 2. Upload via the durable v2 document surface ---
    total = 0
    for md_path in md_files:
        if 0 < limit <= total:
            logger.info(f"Reached limit ({limit}). Stopping.")
            return

        payload = {"source_uri": str(md_path.resolve()), "content_type": "text/markdown"}
        try:
            r = requests.post(f"{api_url}/api/v2/documents", json=payload)
            r.raise_for_status()
            total += 1
            logger.info(f"[markdown] Uploaded '{md_path.stem}' via v2")
        except Exception as e:
            logger.error(f"Error uploading '{md_path.stem}' via v2: {e}")

    logger.info(f"Markdown ingestion complete — {total} document(s) processed.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    setup_logging()

    print(f"##### Retriva CLI ({config.VERSION}) #####\n")

    parser = argparse.ArgumentParser(description="Retriva CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ---- ingest: file or directory ----
    ingest_parser = subparsers.add_parser(
        "ingest", help="Ingest a file or directory into the index (v2 API)"
    )
    ingest_parser.add_argument(
        "--path", type=str, required=True,
        help="Path to a file or directory to ingest"
    )
    ingest_parser.add_argument(
        "--api-url", type=str, default="http://127.0.0.1:8000", help="API URL"
    )
    ingest_parser.add_argument(
        "--limit", type=int, default=0, help="Limit number of files"
    )
    ingest_parser.add_argument(
        "--exclude", type=str, action="append", default=[],
        metavar="FORMAT",
        help=(
            f"File type to exclude (repeatable). "
            f"Supported: {', '.join(sorted(FILE_TYPE_REGISTRY))}"
        ),
    )
    ingest_parser.add_argument(
        "--injector", type=str, default=None,
        choices=["mediawiki_export", "pdf", "markdown"],
        help="Use a specialised injector instead of the default discovery pipeline.",
    )

    # ---- reindex: retired with Retriva API v1 ----
    reindex_parser = subparsers.add_parser(
        "reindex",
        help=(
            "RETIRED (Spec 027): clearing the collection used the "
            "removed Retriva API v1 surface; fails locally without "
            "any HTTP request"
        ),
    )
    reindex_parser.add_argument(
        "--path", type=str, required=True,
        help="Accepted for interface compatibility; unused (command is retired)"
    )
    reindex_parser.add_argument(
        "--api-url", type=str, default="http://127.0.0.1:8000", help="API URL"
    )

    args = parser.parse_args()
    target = Path(args.path)

    # Validate --exclude values early
    exclude: set[str] = set()
    for fmt in getattr(args, "exclude", []):
        if fmt not in FILE_TYPE_REGISTRY:
            parser.error(
                f"Unknown format '{fmt}'. "
                f"Supported: {', '.join(sorted(FILE_TYPE_REGISTRY))}"
            )
        exclude.add(fmt)

    injector = getattr(args, 'injector', None)

    if args.command == "ingest":
        if not target.exists():
            logger.error(f"Path '{target}' does not exist.")
            return
        if injector == "mediawiki_export":
            run_mediawiki_ingest(target, args.api_url, args.limit)
        elif injector == "pdf":
            run_pdf_ingest(target, args.api_url, args.limit)
        elif injector == "markdown":
            run_markdown_ingest(target, args.api_url, args.limit)
        else:
            run_ingest(target, args.api_url, args.limit, exclude or None)

    elif args.command == "reindex":
        # Collection clear was served by the retired Retriva API v1.
        # No supported v2 equivalent exists, so reindex fails locally
        # BEFORE any HTTP request (Spec 027 / ADR-032).
        logger.error(
            "reindex is retired: clearing the collection used Retriva "
            "API v1, which was removed by Spec 027 / ADR-032. No v2 "
            "equivalent exists. Use v2 document deletion "
            "(DELETE /api/v2/documents/{id}) or manage the Qdrant "
            "collection directly, then run 'ingest'. No request was "
            "made.")
        return


if __name__ == "__main__":
    main()
