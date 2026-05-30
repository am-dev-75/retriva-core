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

from openai import OpenAI, AsyncOpenAI
from retriva.config import settings
from retriva.qa.grounding import validate_grounding
from retriva.registry import CapabilityRegistry
from retriva.logger import get_logger
from retriva.profiler import Profiler
from typing import Any, Dict, List, Optional

# Import modules to trigger default registrations
import retriva.qa.retriever        # noqa: F401 — registers DefaultRetriever
import retriva.qa.prompting        # noqa: F401 — registers DefaultPromptBuilder
import retriva.qa.reranker         # noqa: F401 — registers DefaultReranker
import retriva.qa.hybrid_selector  # noqa: F401 — registers DefaultHybridSelector

logger = get_logger(__name__)

def _limit_chunks_by_citations(chunks: list[dict], max_citations: int) -> list[dict]:
    """
    Limits the number of unique sources (titles) in the context.
    Also applies a per-source character limit to prevent context explosion
    from highly descriptive vision model chunks.
    """
    if max_citations <= 0:
        return chunks
        
    seen_titles = {} # title -> char_count
    limited_chunks = []
    
    # 1. Title-based filtering
    for chunk in chunks:
        title = chunk.get("page_title", "Unknown Page")
        if title not in seen_titles:
            if len(seen_titles) >= max_citations:
                continue
            seen_titles[title] = 0
            
        # 2. Per-source size budgeting (prevent context explosion)
        text = chunk.get("text", "")
        max_chars = getattr(settings, "max_chars_per_source", 24000)
        if seen_titles[title] + len(text) > max_chars:
            if seen_titles[title] < 2000: # Ensure at least some text per source
                 truncated = text[:2000] + " [TRUNCATED]"
                 limited_chunks.append({**chunk, "text": truncated})
                 seen_titles[title] += len(truncated)
            continue
            
        limited_chunks.append(chunk)
        seen_titles[title] += len(text)
        
    return limited_chunks




def _retrieve_and_select(query: str, retriever_top_k: int, profiler, metadata_filters: Optional[List[Dict[str, Any]]] = None, metadata_filter_mode: str = "soft", kb_ids: Optional[List[str]] = None) -> list[dict]:
    """
    Run the full retrieval pipeline: vector search → rerank → hybrid select.
    """
    registry = CapabilityRegistry()
    retriever = registry.get_instance("retriever")
    
    # Use the new centralized retrieve method
    chunks = retriever.retrieve(
        query=query, 
        top_k=retriever_top_k, 
        metadata_filters=metadata_filters,
        metadata_filter_mode=metadata_filter_mode,
        kb_ids=kb_ids
    )

    if profiler:
        profiler.mark_phase("retrieval_complete")

    return chunks


def ask_question(question: str, retriever_top_k: int = 20, metadata_filters: Optional[List[Dict[str, Any]]] = None, metadata_filter_mode: str = "soft", kb_ids: Optional[List[str]] = None) -> dict:
    logger.info(f"Processing question: {question}")
    sanitized_question = question.replace('"', '').replace("'", "").strip()

    profiler = Profiler.get_current()
    chunks = _retrieve_and_select(sanitized_question, retriever_top_k, profiler, metadata_filters, metadata_filter_mode, kb_ids=kb_ids)

    chunks = _limit_chunks_by_citations(chunks, settings.max_citations)
    logger.info(f"Final context: {len(chunks)} chunks from up to {settings.max_citations} sources.")

    registry = CapabilityRegistry()
    prompt_builder = registry.get_instance("prompt_builder")
    system_prompt = prompt_builder.build_prompt(question, chunks)
    
    client = OpenAI(api_key=settings.chat_openai_api_key, base_url=settings.chat_base_url)
    response = client.chat.completions.create(
        model=settings.chat_model,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": question}],
        temperature=settings.chat_temperature,
        top_p=settings.chat_top_p
    )
    
    if not response.choices:
        logger.error("LLM returned an empty response (no choices).")
        return {"answer": "Error: LLM returned an empty response.", "retrieved_chunks": chunks, "grounding": []}
        
    answer_text = response.choices[0].message.content
    if answer_text is None:
        answer_text = ""
        
    logger.debug(f"LLM Answer: {answer_text}")
    grounding = validate_grounding(answer_text, chunks)
    return {"answer": answer_text, "retrieved_chunks": chunks, "grounding": grounding}


def ask_question_streaming(question: str, retriever_top_k: int = 20, metadata_filters: Optional[List[Dict[str, Any]]] = None, metadata_filter_mode: str = "soft", kb_ids: Optional[List[str]] = None):
    logger.info(f"Processing question (streaming): {question}")
    sanitized_question = question.replace('"', '').replace("'", "").strip()

    profiler = Profiler.get_current()
    chunks = _retrieve_and_select(sanitized_question, retriever_top_k, profiler, metadata_filters, metadata_filter_mode, kb_ids=kb_ids)

    chunks = _limit_chunks_by_citations(chunks, settings.max_citations)

    registry = CapabilityRegistry()
    prompt_builder = registry.get_instance("prompt_builder")
    system_prompt = prompt_builder.build_prompt(question, chunks)

    client = OpenAI(api_key=settings.chat_openai_api_key, base_url=settings.chat_base_url)
    stream = client.chat.completions.create(
        model=settings.chat_model,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": question}],
        temperature=settings.chat_temperature,
        top_p=settings.chat_top_p,
        stream=True,
    )

    def content_generator():
        for chunk in stream:
            if chunk.choices and len(chunk.choices) > 0:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield delta.content
    return chunks, content_generator()


def ask_question_without_retrieval(question: str) -> str:
    client = OpenAI(api_key=settings.chat_openai_api_key, base_url=settings.chat_base_url)
    response = client.chat.completions.create(
        model=settings.chat_model,
        messages=[{"role": "user", "content": question}],
        temperature=settings.chat_temperature,
    )
    if not response.choices:
        return "Error: LLM returned an empty response."
    answer_text = response.choices[0].message.content or ""
    logger.debug(f"LLM Answer: {answer_text}")
    return answer_text


def ask_question_streaming_without_retrieval(question: str):
    client = OpenAI(api_key=settings.chat_openai_api_key, base_url=settings.chat_base_url)
    stream = client.chat.completions.create(
        model=settings.chat_model,
        messages=[{"role": "user", "content": question}],
        temperature=settings.chat_temperature,
        stream=True,
    )

    def content_generator():
        for chunk in stream:
            if chunk.choices and len(chunk.choices) > 0:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield delta.content
    return [], content_generator()

async def ask_question_streaming_async(question: str, retriever_top_k: int = 20, metadata_filters: Optional[List[Dict[str, Any]]] = None, metadata_filter_mode: str = "soft", kb_ids: Optional[List[str]] = None):
    logger.info(f"Processing question (async streaming): {question}")
    sanitized_question = question.replace('"', '').replace("'", "").strip()
    
    from starlette.concurrency import run_in_threadpool

    profiler = Profiler.get_current()

    registry = CapabilityRegistry()
    retriever = registry.get_instance("retriever")
    
    # Use centralized retrieve method
    chunks = await run_in_threadpool(
        retriever.retrieve, 
        query=sanitized_question, 
        top_k=retriever_top_k, 
        metadata_filters=metadata_filters,
        metadata_filter_mode=metadata_filter_mode,
        kb_ids=kb_ids
    )

    if profiler:
        profiler.mark_phase("retrieval_complete")

    chunks = _limit_chunks_by_citations(chunks, settings.max_citations)

    prompt_builder = registry.get_instance("prompt_builder")
    system_prompt = prompt_builder.build_prompt(question, chunks)

    client = AsyncOpenAI(api_key=settings.chat_openai_api_key, base_url=settings.chat_base_url)
    stream = await client.chat.completions.create(
        model=settings.chat_model,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": question}],
        temperature=settings.chat_temperature,
        top_p=settings.chat_top_p,
        stream=True,
    )

    async def content_generator():
        async for chunk in stream:
            if chunk.choices and len(chunk.choices) > 0:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield delta.content
    return chunks, content_generator()


async def ask_question_streaming_without_retrieval_async(question: str):
    client = AsyncOpenAI(api_key=settings.chat_openai_api_key, base_url=settings.chat_base_url)
    stream = await client.chat.completions.create(
        model=settings.chat_model,
        messages=[{"role": "user", "content": question}],
        temperature=settings.chat_temperature,
        stream=True,
    )
    async def content_generator():
        async for chunk in stream:
            if chunk.choices and len(chunk.choices) > 0:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield delta.content
    return [], content_generator()
