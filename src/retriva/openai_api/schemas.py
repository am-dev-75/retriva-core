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
Pydantic schemas matching the OpenAI Chat Completions and Models API.

These schemas are the contract between Open WebUI (or any OpenAI-compatible
client) and Retriva.  They intentionally mirror the OpenAI spec — field names,
types, and nesting must not diverge.
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Dict
import time
import uuid


# ---------------------------------------------------------------------------
# Citation metadata (Retriva extension, nested inside messages)
# ---------------------------------------------------------------------------

class Citation(BaseModel):
    source: dict = Field(..., description="Source identification (e.g. {'name': '...'})")
    document: List[str] = Field(..., description="The actual text content(s) being cited")
    metadata: Optional[List[dict]] = Field(None, description="Metadata for each document")


class CitationRef(BaseModel):
    start_index: int = Field(..., description="Start character index in the output_text")
    end_index: int = Field(..., description="End character index in the output_text")
    citation_index: int = Field(..., description="Index of the citation in the citations array")


class MessageMetadata(BaseModel):
    sources: List[Citation] = Field(default_factory=list)
    citation_refs: List[CitationRef] = Field(default_factory=list)
    output_text: Optional[str] = None


# ---------------------------------------------------------------------------
# Chat messages
# ---------------------------------------------------------------------------

class ToolCallFunction(BaseModel):
    name: Optional[str] = None
    arguments: str

class ToolCall(BaseModel):
    id: str
    type: str = "function"
    function: ToolCallFunction

class ChatMessage(BaseModel):
    role: str = Field(..., description="One of: system, user, assistant, tool")
    content: str = Field(..., description="Message text")
    metadata: Optional[MessageMetadata] = None
    tool_calls: Optional[List[ToolCall]] = None
    # For role == "tool" (agent loop tool results):
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


# ---------------------------------------------------------------------------
# Chat completions — request
# ---------------------------------------------------------------------------

class ChatCompletionRequest(BaseModel):
    model: str = Field(..., description="Model identifier (accepted but ignored)")
    messages: List[ChatMessage] = Field(..., min_length=1)
    stream: bool = Field(False, description="Whether to stream the response")
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    kb_ids: List[str] = Field(default_factory=lambda: ["default"], description="Knowledge base IDs to search")
    metadata_filters: Optional[List[dict]] = Field(default_factory=list, description="Advanced metadata filters (v2)")
    metadata_filter_mode: str = Field("soft", description="Filtering mode (soft or hard)")
    user_metadata_filter: Optional[Dict[str, str]] = Field(None, description="[DEPRECATED] Simple metadata filter")
    # --- Function calling (agent loop) ---
    # Accepted and forwarded to the chat LLM when present.  Callers that do
    # not send tools are unaffected (plain RAG chat).
    tools: Optional[List[dict]] = Field(
        None, description="OpenAI function-calling tool definitions (agent loop)"
    )
    tool_choice: Optional[object] = Field(
        None, description="OpenAI tool_choice ('auto' | 'none' | specific)"
    )


# ---------------------------------------------------------------------------
# Chat completions — response
# ---------------------------------------------------------------------------

class ChatChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:12]}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = "retriva"
    choices: List[ChatChoice]
    usage: UsageInfo = Field(default_factory=UsageInfo)
    sources: Optional[List[Citation]] = None


# ---------------------------------------------------------------------------
# Chat completions — streaming response (SSE delta protocol)
# ---------------------------------------------------------------------------

class DeltaContent(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    metadata: Optional[MessageMetadata] = None


class StreamingChoice(BaseModel):
    index: int = 0
    delta: DeltaContent
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = "retriva"
    choices: List[StreamingChoice]
    metadata: Optional[MessageMetadata] = None
    sources: Optional[List[Citation]] = None


# ---------------------------------------------------------------------------
# Models — response
# ---------------------------------------------------------------------------

class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "retriva"


class ListModelsResponse(BaseModel):
    object: str = "list"
    data: List[ModelInfo]