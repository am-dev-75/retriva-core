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

"""Rendering services — provides data fetching and preparation for artifacts."""

from contextvars import ContextVar
from pathlib import Path
from typing import Dict, List, Any, Optional
from retriva.logger import get_logger
from retriva.qa.answerer import _retrieve_and_select, ask_question
from retriva.config import settings

logger = get_logger(__name__)

#: Task-scoped artifact progress callback (Spec 026): set by the
#: durable artifact handler so the data-fetch boundary can advance
#: the bounded progress phases without touching any renderer.
#: Context-scoped: concurrent executions never observe each other's
#: callbacks; execution context is never read from process-global
#: mutable state.
_artifact_progress_cb: ContextVar = ContextVar(
    "artifact_progress_cb", default=None)


def _notify_artifact_progress(phase: str) -> None:
    callback = _artifact_progress_cb.get()
    if callback is None:
        return
    try:
        callback(phase)
    except Exception as exc:  # noqa: BLE001 - progress is best-effort
        logger.warning(
            "artifact progress callback failed (best-effort): "
            "exception=%s", exc.__class__.__name__)


def fetch_artifact_data(artifact_type: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch and prepare data for the given artifact type."""
    _notify_artifact_progress("fetching_data")
    data = _fetch_artifact_data_impl(artifact_type, parameters)
    _notify_artifact_progress("rendering")
    return data


def _fetch_artifact_data_impl(artifact_type: str,
                              parameters: Dict[str, Any]) -> Dict[str, Any]:
    query = parameters.get("query", "")
    
    if artifact_type == "document_list":
        return _prepare_document_list(query, parameters)
    elif artifact_type == "basic_report":
        return _prepare_basic_report(query, parameters)
    
    # Fallback to parameters
    return {
        "title": parameters.get("title", "Retriva Artifact"),
        "content": parameters.get("content", "No content available.")
    }

def _prepare_document_list(query: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
    """Perform a search and return a list of unique documents."""
    if not query:
        return {
            "title": "Document List",
            "content": "No query provided for document list."
        }
    
    logger.info(f"Fetching document list for query: {query}")
    
    # We use a larger top_k to find more documents
    chunks = _retrieve_and_select(query, retriever_top_k=20, profiler=None)
    
    # Deduplicate by source_path
    seen_paths = set()
    documents = []
    
    for chunk in chunks:
        path = chunk.get("source_path")
        if path and path not in seen_paths:
            seen_paths.add(path)
            title = chunk.get("page_title") or Path(path).name
            documents.append({
                "title": title,
                "path": path
            })
    
    if not documents:
        content = f"No documents found matching: {query}"
    else:
        lines = [f"- {doc['title']} ({doc['path']})" for doc in documents]
        content = "\n".join(lines)
        
    return {
        "title": f"Documents matching: {query}",
        "content": content,
        "documents": documents
    }

def _prepare_basic_report(query: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
    """Ask a question and return the answer as a report."""
    if not query:
        return {
            "title": "Basic Report",
            "content": "No topic provided for the report."
        }
    
    logger.info(f"Generating basic report for topic: {query}")
    
    # Use ask_question to get a grounded answer
    result = ask_question(query)
    answer = result.get("answer", "No answer generated.")
    
    # Extract sources for the report
    grounding = result.get("grounding", [])
    sources_text = ""
    if grounding:
        sources_text = "\n\n### Sources\n"
        seen_sources = set()
        for g in grounding:
            source = g.get("source", "Unknown Source")
            if source not in seen_sources:
                seen_sources.add(source)
                sources_text += f"- {source}\n"

    return {
        "title": f"Report: {query}",
        "content": f"{answer}{sources_text}"
    }
