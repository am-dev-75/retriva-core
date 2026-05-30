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

import json
import uuid
import asyncio
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from retriva.openai_api.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionChunk,
    ChatChoice,
    ChatMessage,
    MessageMetadata,
    Citation,
    UsageInfo,
    DeltaContent,
    StreamingChoice,
    CitationRef,
    ToolCall,
    ToolCallFunction,
)
from retriva.qa.answerer import (
    ask_question, 
    ask_question_streaming,
    ask_question_streaming_async,
    ask_question_without_retrieval,
    ask_question_streaming_without_retrieval,
    ask_question_streaming_without_retrieval_async
)
from retriva.config import settings
from retriva.logger import get_logger
from retriva.profiler import Profiler

logger = get_logger(__name__)
router = APIRouter(tags=["chat"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _extract_user_question(request: ChatCompletionRequest) -> str:
    """Return the content of the last 'user' message, or raise 400."""
    for msg in reversed(request.messages):
        if msg.role == "user":
            return msg.content
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="No message with role 'user' found in the messages array.",
    )


def _build_citations(chunks: list[dict]) -> list[Citation]:
    """Extract citation metadata from retrieved chunk payloads in Open WebUI format."""
    by_norm_title = {}
    for chunk in chunks:
        # Use filename for consistent citations
        raw_title = chunk.get("filename")
        if not raw_title:
             path = chunk.get("source_path", "unknown")
             raw_title = Path(path).name
             if raw_title == "unknown":
                 raw_title = "Unknown Source"
        
        # Group by title to match the simplified prompt builder logic
        norm_key = raw_title
        
        text = chunk.get("text", "")
        path = chunk.get("source_path", "unknown")
        
        if norm_key not in by_norm_title:
            by_norm_title[norm_key] = {
                "source": {"name": raw_title},
                "document": [text],
                "metadata": [{"source": path, "title": raw_title, "user_metadata": chunk.get("user_metadata")}]
            }
        else:
            # Deduplicate text snippets
            if text not in by_norm_title[norm_key]["document"]:
                by_norm_title[norm_key]["document"].append(text)
            
            # Only add unique metadata entries (per path)
            if not any(m["source"] == path for m in by_norm_title[norm_key]["metadata"]):
                # Apply per-citation metadata limit
                if settings.max_metadata_per_citation <= 0 or len(by_norm_title[norm_key]["metadata"]) < settings.max_metadata_per_citation:
                    by_norm_title[norm_key]["metadata"].append({"source": path, "title": raw_title, "user_metadata": chunk.get("user_metadata")})

    results = [Citation(**v) for v in by_norm_title.values()]
    
    # Apply total citations limit (important to prevent LLM/UI overwhelm)
    if settings.max_citations > 0:
        results = results[:settings.max_citations]
        
    logger.info(f"Grouped {len(chunks)} chunks into {len(results)} unique citations.")
    return results


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 characters per token."""
    return max(1, len(text) // 4)


def _build_citation_refs(answer: str, citations: list[Citation]) -> tuple[str, str, list[CitationRef], list[ToolCall]]:
    """
    Parse [Title] markers from the answer, map the preceding sentence to a CitationRef.
    Returns (clean_text, compat_text, citation_refs, tool_calls).
    """
    # Build a robust mapping from possible LLM citation strings to citation index
    title_to_idx = {}
    for i, c in enumerate(citations):
        name = c.source.get("name")
        if name:
            title_to_idx[name] = i
            title_to_idx[name.lower()] = i
            # Also map the stem (without extension) for cases where LLM drops it
            stem = Path(name).stem
            title_to_idx[stem] = i
            title_to_idx[stem.lower()] = i
            
            # Map page_title if it was captured in metadata
            for meta in c.metadata:
                pt = meta.get("title")
                if pt:
                    title_to_idx[pt] = i
                    title_to_idx[pt.lower()] = i

    # Regex to find [Title]
    pattern = r'\[([^\]]+)\]'
    
    clean_text = ""
    compat_text = ""
    citation_refs = []
    
    last_end = 0
    current_clean_index = 0
    
    for match in re.finditer(pattern, answer):
        raw_title = match.group(1)
        # Try exact, then lowercase
        citation_idx = title_to_idx.get(raw_title)
        if citation_idx is None:
            citation_idx = title_to_idx.get(raw_title.lower())
            
        if citation_idx is not None:
            
            text_segment = answer[last_end:match.start()]
            clean_text += text_segment
            compat_text += text_segment
            
            end_index = current_clean_index + len(text_segment)
            start_index = current_clean_index
            
            # Try to find the start of the sentence
            sentence_start = text_segment.rfind('.')
            if sentence_start != -1:
                start_index = current_clean_index + sentence_start + 1
            else:
                nl_start = text_segment.rfind('\n')
                if nl_start != -1:
                    start_index = current_clean_index + nl_start + 1
            
            # Strip leading whitespace
            while start_index < end_index and clean_text[start_index].isspace():
                start_index += 1
                
            citation_refs.append(
                CitationRef(
                    start_index=max(0, start_index),
                    end_index=end_index,
                    citation_index=citation_idx
                )
            )
            
            compat_text += f"[{citation_idx + 1}]"
            
            current_clean_index = end_index
            last_end = match.end()
        else:
            # Not a known citation, leave it in the text
            text_segment = answer[last_end:match.end()]
            clean_text += text_segment
            compat_text += text_segment
            current_clean_index += len(text_segment)
            last_end = match.end()
            
    clean_text += answer[last_end:]
    compat_text += answer[last_end:]
    
    tool_calls = []
    if citations:
        tool_calls.append(
            ToolCall(
                id=f"call_{uuid.uuid4().hex[:10]}",
                function=ToolCallFunction(
                    name="citation",
                    arguments=json.dumps({"citations": [c.model_dump() for c in citations]})
                )
            )
        )
        
    return clean_text, compat_text, citation_refs, tool_calls


# ---------------------------------------------------------------------------
# Non-streaming handler
# ---------------------------------------------------------------------------

async def _handle_non_streaming(
    request: ChatCompletionRequest, question: str, bypass_rag: bool = False
) -> ChatCompletionResponse:
    """Handle standard non-streaming request with optional RAG."""
    profiler = Profiler.start_request()
    profiler.is_streaming = False
    
    from starlette.concurrency import run_in_threadpool
    try:
        if bypass_rag:
            # Simple direct answer without retrieval
            answer = await run_in_threadpool(ask_question_without_retrieval, question)
            result = {"answer": answer, "retrieved_chunks": []}
        else:
            result = await run_in_threadpool(
                ask_question, 
                question, 
                settings.retriever_top_k, 
                request.metadata_filters, 
                request.metadata_filter_mode,
                request.kb_ids
            )
    except Exception as e:
        logger.error(f"QA pipeline error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"QA pipeline error: {e}",
        )

    answer = result["answer"]
    chunks = result.get("retrieved_chunks", [])
    citations = _build_citations(chunks)
    
    clean_text, compat_text, citation_refs, tool_calls = _build_citation_refs(answer, citations)

    # Build the prompt text for token estimation (system + user)
    prompt_text = question
    for msg in request.messages:
        prompt_text += msg.content

    response = ChatCompletionResponse(
        model="retriva",
        choices=[
            ChatChoice(
                index=0,
                message=ChatMessage(
                    role="assistant",
                    content=compat_text,
                    metadata=MessageMetadata(
                        sources=citations,
                        citation_refs=citation_refs,
                        output_text=clean_text
                    ),
                    tool_calls=tool_calls if tool_calls else None
                ),
                finish_reason="stop",
            )
        ],
        usage=UsageInfo(
            prompt_tokens=_estimate_tokens(prompt_text),
            completion_tokens=_estimate_tokens(compat_text),
            total_tokens=_estimate_tokens(prompt_text) + _estimate_tokens(compat_text),
        ),
        sources=citations,
    )

    logger.debug(
        f"Chat completion response — {len(citations)} citation(s), "
        f"{response.usage.total_tokens} est. tokens"
    )
    
    profiler.finalize()
    return response


# ---------------------------------------------------------------------------
# Streaming handler (new — SSE delta protocol)
# ---------------------------------------------------------------------------

async def _handle_streaming(
    request: ChatCompletionRequest, question: str, bypass_rag: bool = False
) -> StreamingResponse:
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    profiler = Profiler.start_request()
    profiler.is_streaming = True
    
    from starlette.concurrency import run_in_threadpool
    
    try:
        if bypass_rag:
             chunks, content_gen = await ask_question_streaming_without_retrieval_async(question)
        else:
            chunks, content_gen = await ask_question_streaming_async(
                question, 
                settings.retriever_top_k, 
                request.metadata_filters,
                request.metadata_filter_mode,
                request.kb_ids
            )
    except Exception as e:
        logger.error(f"QA pipeline error (streaming init): {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"QA pipeline error (streaming init): {e}",
        )

    async def _sse_generator():
        MAX_SSE_PAYLOAD = 12000 # ~12KB limit per line for extreme safety

        async def _normalized_yield(data_json: str):
            """
            Yields SSE data event(s). If the payload exceeds MAX_SSE_PAYLOAD,
            it is split into multiple 'data:' lines using the SSE multiline protocol.
            Each line is yielded separately to force network flushes.
            """
            if len(data_json) <= MAX_SSE_PAYLOAD:
                yield f"data: {data_json}\n\n".encode("utf-8")
            else:
                parts = [data_json[i : i + MAX_SSE_PAYLOAD] for i in range(0, len(data_json), MAX_SSE_PAYLOAD)]
                for i, part in enumerate(parts):
                    if i == len(parts) - 1:
                        yield f"data: {part}\n\n".encode("utf-8")
                    else:
                        yield f"data: {part}\n".encode("utf-8")

        # First event: role announcement
        first_chunk = ChatCompletionChunk(
            id=completion_id,
            choices=[
                StreamingChoice(
                    index=0,
                    delta=DeltaContent(role="assistant"),
                    finish_reason=None,
                )
            ],
        )
        async for b in _normalized_yield(first_chunk.model_dump_json(exclude_none=True)):
            yield b

        citations = await run_in_threadpool(_build_citations, chunks) if chunks else []
        profiler.mark_phase("citations_built")
        
        # Build mapping for citation matching
        path_to_idx = {}
        for i, c in enumerate(citations):
            for meta in c.metadata:
                path_to_idx[meta["source"]] = i
        
        title_to_idx = {}
        for chunk in chunks:
            path = chunk.get("source_path", "unknown")
            if path in path_to_idx:
                idx = path_to_idx[path]
                title = chunk.get("page_title")
                if title:
                    title_to_idx[title] = idx
                filename = Path(path).name.lower()
                title_to_idx[filename] = idx
                title_to_idx[Path(path).stem.lower()] = idx
                display_title = citations[idx].source.get("name")
                if display_title:
                    display_title = display_title.lower()
                    title_to_idx[display_title] = idx
        
        buffer = ""
        inside_bracket = False
        clean_text_so_far = ""
        citation_refs = []
        first_token = True

        full_answer = ""
        # Content events
        try:
            async for token in content_gen:
                if first_token:
                    profiler.mark_phase("first_token_received")
                    first_token = False
                
                out_token = ""
                if '[' in token or ']' in token or inside_bracket:
                    for char in token:
                        if not inside_bracket:
                            if char == '[':
                                inside_bracket = True
                                buffer += char
                            else:
                                out_token += char
                                clean_text_so_far += char
                        else:
                            buffer += char
                            if char == ']':
                                inside_bracket = False
                                content = buffer[1:-1].strip().lower()
                                citation_idx = title_to_idx.get(content)
                                if citation_idx is None:
                                    # Fuzzy match
                                    for t, i in title_to_idx.items():
                                        if content in t or t in content:
                                            citation_idx = i
                                            break

                                if citation_idx is not None:
                                    end_index = len(clean_text_so_far)
                                    sentence_start = clean_text_so_far.rfind('.')
                                    start_index = sentence_start + 1 if sentence_start != -1 else 0
                                    citation_refs.append(
                                        CitationRef(
                                            start_index=max(0, start_index),
                                            end_index=end_index,
                                            citation_index=citation_idx
                                        )
                                    )
                                    out_token += f"[{citation_idx + 1}]"
                                else:
                                    out_token += buffer
                                    clean_text_so_far += buffer
                                buffer = ""
                else:
                    out_token = token
                    clean_text_so_far += token

                if out_token:
                    full_answer += out_token
                    chunk = ChatCompletionChunk(
                        id=completion_id,
                        choices=[StreamingChoice(index=0, delta=DeltaContent(content=out_token), finish_reason=None)],
                    )
                    async for b in _normalized_yield(chunk.model_dump_json(exclude_none=True)):
                        yield b
            
            logger.debug(f"LLM Answer (streaming): {full_answer}")
        except Exception as e:
            logger.error(f"Streaming error mid-flight: {e}")

        # Prepare sources
        all_sources = []
        for c in citations:
            limit = settings.citation_snippet_size
            joined_doc = "\n\n---\n\n".join(c.document)
            if limit > 0 and len(joined_doc) > limit:
                joined_doc = joined_doc[:limit] + "..."
            all_sources.append(Citation(source=c.source, document=[joined_doc], metadata=c.metadata))

        if all_sources:
            sources_text = "\n\nSources:\n"
            for i, c in enumerate(all_sources):
                sources_text += f"[{i+1}] {c.source.get('name', 'Unknown')}\n"
            
            text_chunk = ChatCompletionChunk(
                id=completion_id,
                choices=[StreamingChoice(index=0, delta=DeltaContent(content=sources_text), finish_reason=None)],
            )
            async for b in _normalized_yield(text_chunk.model_dump_json(exclude_none=True)):
                yield b
            
            for i, c in enumerate(all_sources):
                metadata_payload = MessageMetadata(
                    sources=[c],
                    citation_refs=citation_refs if i == len(all_sources) - 1 else [],
                    output_text=clean_text_so_far if i == len(all_sources) - 1 else ""
                )
                chunk = ChatCompletionChunk(
                    id=completion_id,
                    choices=[StreamingChoice(index=0, delta=DeltaContent(), finish_reason="stop" if i == len(all_sources) - 1 else None)],
                    metadata=metadata_payload,
                )
                async for b in _normalized_yield(chunk.model_dump_json(exclude_none=True)):
                    yield b
                await asyncio.sleep(0.01)
        else:
            stop_chunk = ChatCompletionChunk(
                id=completion_id,
                choices=[StreamingChoice(index=0, delta=DeltaContent(), finish_reason="stop")],
            )
            async for b in _normalized_yield(stop_chunk.model_dump_json(exclude_none=True)):
                yield b
        
        profiler.finalize()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/v1/chat/completions")
async def create_chat_completion(request: ChatCompletionRequest):
    question = _extract_user_question(request)
    bypass_rag = question.startswith("### Task:")
    
    # Normalize legacy user_metadata_filter into advanced metadata_filters
    if request.user_metadata_filter:
        if request.metadata_filters is None:
            request.metadata_filters = []
        for k, v in request.user_metadata_filter.items():
            request.metadata_filters.append({
                "field": f"user_metadata.{k}",
                "operator": "eq",
                "value": v
            })
            
    if request.stream:
        return await _handle_streaming(request, question, bypass_rag=bypass_rag)
    else:
        return await _handle_non_streaming(request, question, bypass_rag=bypass_rag)