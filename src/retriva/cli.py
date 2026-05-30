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

import argparse
from pathlib import Path
from typing import Callable, Dict, List, Set

import requests

from retriva import config
from retriva.ingestion.discover import classify_file, discover_files, FILE_TYPE_REGISTRY
from retriva.ingestion.mirror import source_to_canonical
from retriva.ingestion.html_parser import extract_title
from retriva.logger import setup_logging, get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Per-type ingestion handlers
# ---------------------------------------------------------------------------
# To support a new format, add a handler here and a matching entry
# in discover.py's FILE_TYPE_REGISTRY.
# ---------------------------------------------------------------------------

def ingest_html_file(path: str, api_url: str, api_version: str = "v2") -> None:
    """Read an HTML file and POST it to the ingestion API."""
    if api_version == "v2":
        payload = {"source_uri": str(Path(path).resolve()), "content_type": "text/html"}
        try:
            r = requests.post(f"{api_url}/api/v2/documents", json=payload)
            r.raise_for_status()
        except Exception as e:
            logger.error(f"Error uploading HTML via v2 {path}: {e}")
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
    except Exception as e:
        logger.error(f"Error reading {path}: {e}")
        return

    canonical = source_to_canonical(path)
    title = extract_title(html)

    payload = {
        "source_path": canonical,
        "page_title": title,
        "html_content": html,
        "origin_file_path": path,
    }

    try:
        r = requests.post(f"{api_url}/api/v1/ingest/html", json=payload)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Error uploading HTML {path}: {e}")


def ingest_image_file(path: str, api_url: str, api_version: str = "v2") -> None:
    """POST a standalone image file to the ingestion API for VLM processing."""
    if api_version == "v2":
        logger.warning(f"Images are not natively supported by v2 yet. Falling back to v1 for {path}")

    payload = {
        "source_path": path,
        "page_title": Path(path).stem,
        "file_path": path,
    }

    try:
        r = requests.post(f"{api_url}/api/v1/ingest/image", json=payload)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Error uploading image {path}: {e}")


def ingest_text_file(path: str, api_url: str, api_version: str = "v2") -> None:
    """Read a plain-text file and POST it to the ingestion API."""
    if api_version == "v2":
        payload = {"source_uri": str(Path(path).resolve()), "content_type": "text/plain"}
        try:
            r = requests.post(f"{api_url}/api/v2/documents", json=payload)
            r.raise_for_status()
        except Exception as e:
            logger.error(f"Error uploading text via v2 {path}: {e}")
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logger.error(f"Error reading {path}: {e}")
        return

    payload = {
        "source_path": path,
        "page_title": Path(path).stem,
        "content_text": content,
    }

    try:
        r = requests.post(f"{api_url}/api/v1/ingest/text", json=payload)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Error uploading text {path}: {e}")


def ingest_pdf_file(path: str, api_url: str, api_version: str = "v2") -> None:
    """Wrapper to integrate PDF ingestion into the generic discovery flow."""
    run_pdf_ingest(Path(path), api_url, limit=0, api_version=api_version)

def ingest_markdown_file(path: str, api_url: str, api_version: str = "v2") -> None:
    """Wrapper to integrate Markdown ingestion into the generic discovery flow."""
    run_markdown_ingest(Path(path), api_url, limit=0, api_version=api_version)

# Maps file type keys (from FILE_TYPE_REGISTRY) to handler functions.
INGEST_HANDLERS: Dict[str, Callable[[str, str, str], None]] = {
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
    api_version: str = "v2",
) -> None:
    """
    Discover and ingest all supported files under *target*.
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
            handler(path, api_url, api_version)
            total += 1

    logger.info(f"Ingestion complete — {total} file(s) processed.")


# ---------------------------------------------------------------------------
# MediaWiki export injector
# ---------------------------------------------------------------------------

def run_mediawiki_ingest(
    target: Path,
    api_url: str,
    limit: int = 0,
    namespaces: Set[int] | None = None,
    api_version: str = "v2",
) -> None:
    """
    Discover and ingest MediaWiki XML export files under *target*.

    1. Walk *target* for ``*.xml`` files validated as MediaWiki exports.
    2. Find ``assets/`` directories and build a file index.
    3. Parse each XML, convert wikitext to plain text, and POST to the API.
    4. POST resolved image assets for VLM enrichment.
    """
    from retriva.ingestion.mediawiki_export_parser import (
        is_mediawiki_export,
        parse_export,
        wikitext_to_plaintext,
        DEFAULT_NAMESPACES,
    )
    from retriva.ingestion.mediawiki_assets import (
        build_asset_index,
        find_assets_dirs,
        resolve_file_reference,
        is_image_asset,
    )

    if namespaces is None:
        namespaces = DEFAULT_NAMESPACES

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

    # --- 2. Build asset index ---
    asset_index: dict[str, Path] = {}
    for assets_dir in find_assets_dirs(target):
        asset_index.update(build_asset_index(assets_dir))
    logger.info(f"Asset index: {len(asset_index)} file(s) total.")

    # --- 3. Parse and ingest pages ---
    if api_version == "v2":
        import time
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
        return

    total = 0
    for xml_path in xml_files:
        logger.info(f"Parsing {xml_path}...")
        for page in parse_export(xml_path, namespaces=namespaces):
            if 0 < limit <= total:
                logger.info(f"Reached limit ({limit}). Stopping.")
                return

            plaintext = wikitext_to_plaintext(page.text)
            if not plaintext.strip():
                logger.debug(f"Skipping empty page: {page.title}")
                continue

            # Resolve file references
            resolved_assets = []
            for ref in page.file_references:
                resolved = resolve_file_reference(ref, asset_index)
                if resolved:
                    resolved_assets.append(str(resolved))
                    logger.debug(f"Resolved asset: {ref} → {resolved}")
                else:
                    logger.debug(f"Unresolved asset: {ref}")

            # POST page text
            payload = {
                "source_path": str(xml_path),
                "page_title": page.title,
                "content_text": plaintext,
                "page_id": page.page_id,
                "namespace": page.namespace,
                "linked_assets": resolved_assets,
            }
            try:
                r = requests.post(f"{api_url}/api/v1/ingest/mediawiki", json=payload)
                r.raise_for_status()
                total += 1
                logger.info(f"[mediawiki] Uploaded page: {page.title}")
            except Exception as e:
                logger.error(f"Error uploading page '{page.title}': {e}")

            # POST resolved image assets for VLM enrichment
            for asset_path in resolved_assets:
                if is_image_asset(Path(asset_path)):
                    img_payload = {
                        "source_path": asset_path,
                        "page_title": Path(asset_path).stem,
                        "file_path": asset_path,
                    }
                    try:
                        r = requests.post(
                            f"{api_url}/api/v1/ingest/image", json=img_payload
                        )
                        r.raise_for_status()
                        logger.info(f"[image] Uploaded asset: {asset_path}")
                    except Exception as e:
                        logger.error(f"Error uploading image '{asset_path}': {e}")

    logger.info(f"MediaWiki ingestion complete — {total} page(s) processed.")


# ---------------------------------------------------------------------------
# PDF injector
# ---------------------------------------------------------------------------

def run_pdf_ingest(
    target: Path,
    api_url: str,
    limit: int = 0,
    api_version: str = "v2",
) -> None:
    """
    Discover and ingest PDF files under *target*.

    1. Walk *target* for ``*.pdf`` files.
    2. Parse each PDF page-by-page via the registry-resolved PdfExtractor.
    3. POST each page to ``/api/v1/ingest/pdf``.
    """
    from retriva.ingestion.pdf_parser import parse_pdf

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

    # --- 2. Parse and ingest ---
    total = 0
    for pdf_path in pdf_files:
        if 0 < limit <= total:
            logger.info(f"Reached limit ({limit}). Stopping.")
            return

        if api_version == "v2":
            payload = {"source_uri": str(pdf_path.resolve()), "content_type": "application/pdf"}
            try:
                r = requests.post(f"{api_url}/api/v2/documents", json=payload)
                r.raise_for_status()
                total += 1
                logger.info(f"[pdf] Uploaded '{pdf_path.stem}' via v2")
            except Exception as e:
                logger.error(f"Error uploading '{pdf_path.stem}' via v2: {e}")
            continue

        logger.info(f"Parsing {pdf_path}...")
        doc = parse_pdf(pdf_path)
        if doc is None:
            logger.warning(f"Skipping unreadable PDF: {pdf_path}")
            continue

        if not doc.pages:
            logger.warning(f"No extractable text in {pdf_path} — skipping.")
            continue

        if doc.skipped_pages > 0:
            logger.info(
                f"'{doc.title}': {doc.skipped_pages}/{doc.total_pages} "
                f"page(s) had no extractable text."
            )

        for page in doc.pages:
            if 0 < limit <= total:
                logger.info(f"Reached limit ({limit}). Stopping.")
                return

            payload = {
                "source_path": doc.source_path,
                "page_title": doc.title,
                "content_text": page.text,
                "page_number": page.page_number,
                "total_pages": doc.total_pages,
            }
            try:
                r = requests.post(f"{api_url}/api/v1/ingest/pdf", json=payload)
                r.raise_for_status()
                total += 1
                logger.info(
                    f"[pdf] Uploaded page {page.page_number}/{doc.total_pages} "
                    f"of '{doc.title}'"
                )
            except Exception as e:
                logger.error(
                    f"Error uploading page {page.page_number} "
                    f"of '{doc.title}': {e}"
                )

    logger.info(f"PDF ingestion complete — {total} page(s)/doc(s) processed.")


# ---------------------------------------------------------------------------
# Markdown injector
# ---------------------------------------------------------------------------

def run_markdown_ingest(
    target: Path,
    api_url: str,
    limit: int = 0,
    api_version: str = "v2",
) -> None:
    """
    Discover and ingest Markdown files under *target*.

    1. Walk *target* for ``*.md`` and ``*.markdown`` files.
    2. Parse each file locally to extract sections/headings.
    3. POST to ``/api/v1/ingest/markdown``.
    """
    from retriva.ingestion.markdown_parser import parse_markdown

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

    # --- 2. Parse and ingest ---
    total = 0
    for md_path in md_files:
        if 0 < limit <= total:
            logger.info(f"Reached limit ({limit}). Stopping.")
            return

        if api_version == "v2":
            payload = {"source_uri": str(md_path.resolve()), "content_type": "text/markdown"}
            try:
                r = requests.post(f"{api_url}/api/v2/documents", json=payload)
                r.raise_for_status()
                total += 1
                logger.info(f"[markdown] Uploaded '{md_path.stem}' via v2")
            except Exception as e:
                logger.error(f"Error uploading '{md_path.stem}' via v2: {e}")
            continue

        logger.info(f"Parsing {md_path}...")
        doc = parse_markdown(md_path)
        if doc is None:
            logger.warning(f"Skipping unreadable Markdown: {md_path}")
            continue

        payload = {
            "source_path": doc["source_path"],
            "page_title": doc["title"],
            "sections": doc["sections"],
        }
        try:
            r = requests.post(f"{api_url}/api/v1/ingest/markdown", json=payload)
            r.raise_for_status()
            total += 1
            logger.info(f"[markdown] Uploaded '{doc['title']}'")
        except Exception as e:
            logger.error(f"Error uploading '{doc['title']}': {e}")

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
        "ingest", help="Ingest a file or directory into the index"
    )
    ingest_parser.add_argument(
        "--path", type=str, required=True,
        help="Path to a file or directory to ingest"
    )
    ingest_parser.add_argument(
        "--api-url", type=str, default="http://127.0.0.1:8000", help="API URL"
    )
    ingest_parser.add_argument(
        "--api-version", type=str, default="v2", choices=["v1", "v2"],
        help="API version to use for ingestion (default: v2)"
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
    ingest_parser.add_argument(
        "--namespaces", type=str, default=None,
        help="Comma-separated MediaWiki namespace IDs to index (default: 0,6).",
    )

    # ---- reindex: directory only ----
    reindex_parser = subparsers.add_parser(
        "reindex", help="Clear the collection and re-ingest a directory"
    )
    reindex_parser.add_argument(
        "--path", type=str, required=True,
        help="Path to the directory to scan and re-ingest"
    )
    reindex_parser.add_argument(
        "--api-url", type=str, default="http://127.0.0.1:8000", help="API URL"
    )
    reindex_parser.add_argument(
        "--api-version", type=str, default="v2", choices=["v1", "v2"],
        help="API version to use for ingestion (default: v2)"
    )
    reindex_parser.add_argument(
        "--limit", type=int, default=0, help="Limit number of files"
    )
    reindex_parser.add_argument(
        "--exclude", type=str, action="append", default=[],
        metavar="FORMAT",
        help=(
            f"File type to exclude (repeatable). "
            f"Supported: {', '.join(sorted(FILE_TYPE_REGISTRY))}"
        ),
    )
    reindex_parser.add_argument(
        "--injector", type=str, default=None,
        choices=["mediawiki_export", "pdf", "markdown"],
        help="Use a specialised injector instead of the default discovery pipeline.",
    )
    reindex_parser.add_argument(
        "--namespaces", type=str, default=None,
        help="Comma-separated MediaWiki namespace IDs to index (default: 0,6).",
    )

    args = parser.parse_args()
    target = Path(args.path)

    # Validate --exclude values early
    exclude: set[str] = set()
    for fmt in args.exclude:
        if fmt not in FILE_TYPE_REGISTRY:
            parser.error(
                f"Unknown format '{fmt}'. "
                f"Supported: {', '.join(sorted(FILE_TYPE_REGISTRY))}"
            )
        exclude.add(fmt)

    # Parse --namespaces if provided
    ns_set = None
    if hasattr(args, 'namespaces') and args.namespaces:
        try:
            ns_set = {int(n.strip()) for n in args.namespaces.split(",")}
        except ValueError:
            parser.error(f"Invalid --namespaces value: '{args.namespaces}'. Use comma-separated integers.")

    injector = getattr(args, 'injector', None)

    if args.command == "ingest":
        if not target.exists():
            logger.error(f"Path '{target}' does not exist.")
            return
        if injector == "mediawiki_export":
            run_mediawiki_ingest(target, args.api_url, args.limit, ns_set, args.api_version)
        elif injector == "pdf":
            run_pdf_ingest(target, args.api_url, args.limit, args.api_version)
        elif injector == "markdown":
            run_markdown_ingest(target, args.api_url, args.limit, args.api_version)
        else:
            run_ingest(target, args.api_url, args.limit, exclude or None, args.api_version)

    elif args.command == "reindex":
        if not target.is_dir():
            logger.error(f"reindex requires a directory, got '{target}'")
            return
        logger.info("Reindexing (clearing and ingesting)...")
        try:
            r = requests.delete(f"{args.api_url}/api/v1/ingest/collection")
            r.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to clear collection: {e}")
            return
        if injector == "mediawiki_export":
            run_mediawiki_ingest(target, args.api_url, args.limit, ns_set, args.api_version)
        elif injector == "pdf":
            run_pdf_ingest(target, args.api_url, args.limit, args.api_version)
        elif injector == "markdown":
            run_markdown_ingest(target, args.api_url, args.limit, args.api_version)
        else:
            run_ingest(target, args.api_url, args.limit, exclude or None, args.api_version)


if __name__ == "__main__":
    main()
