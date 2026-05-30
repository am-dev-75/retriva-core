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

from typing import List, Dict

def build_prompt(question: str, retrieved_chunks: List[Dict]) -> str:
    """
    Builds the grounded system prompt with Open WebUI-compatible citations.

    Open WebUI parses bracketed references (e.g. ``[Page Title]``) from the
    LLM response text and turns them into clickable citation chips.  To make
    this work the context blocks must carry identifiable source labels and
    the LLM must be instructed to reference those labels.
    """
    # Group chunks by title to avoid duplicate source IDs in the prompt
    grouped = {}
    for chunk in retrieved_chunks:
        title = chunk.get("page_title", "Unknown Page")
        if title not in grouped:
            grouped[title] = {
                "url": chunk.get("canonical_doc_id", chunk.get("source_path", "")),
                "texts": [chunk.get("text", "")],
                "user_metadata": chunk.get("user_metadata", {})
            }
        else:
            # Only add if text is not exactly the same
            new_text = chunk.get("text", "")
            if new_text not in grouped[title]["texts"]:
                grouped[title]["texts"].append(new_text)
            
            # Merge metadata
            meta = chunk.get("user_metadata", {})
            if meta:
                if not grouped[title].get("user_metadata"):
                    grouped[title]["user_metadata"] = {}
                grouped[title]["user_metadata"].update(meta)

    context_str = ""
    source_list = ""
    for title, data in grouped.items():
        url = data["url"]
        combined_text = "\n\n---\n\n".join(data["texts"])
        source_id = f"[{title}]"
        
        meta_str = ""
        user_metadata = data.get("user_metadata")
        if user_metadata:
            meta_str = "Metadata tags:\n"
            for k, v in user_metadata.items():
                meta_str += f"- {k}: {v}\n"
        
        # Build context block with unique source tag
        context_str += (
            f"\n<source id=\"{title}\">\n"
            f"Source: {title}\n"
            f"URL: {url}\n"
            f"{meta_str}"
            f"{combined_text}\n"
            f"</source>\n"
        )
        source_list += f"  - {source_id}\n"

    system_prompt = f"""You are Retriva, a Precision Technical Documentation Assistant.
Your goal is to provide factually dense, highly nuanced, and strictly grounded answers.

PERSONA & TONE:
- Professional, technical, and objective.
- Prioritize accuracy over completeness. If a value is mentioned for a different product or board (e.g., "SBCX" vs "AURA SOM"), do NOT attribute it to the subject unless the context explicitly confirms they are the same.
- Distinguish clearly between "Direct Evidence" (measurements for the subject) and "Related/Peripheral Evidence" (measurements for different but similar hardware).

ANSWERING RULES:
1. Answer ONLY using the provided context.
2. Read the ENTIRE context before formulating your answer. If there are multiple measurements, graphs, or tests for the same hardware, you must compare them all and report the absolute minimums/maximums across all provided data.
3. If the context does not contain sufficient evidence to answer the question, state: "I do not have sufficient evidence in my knowledge base to answer this question."
4. If the user asks for a specific "maximum" or "rated" value and it is NOT explicitly listed, state that the theoretical maximum is not documented, then provide the highest measured values found in the test data as an alternative.
5. NUANCE: Use "Note:" or "Caveat:" sections to discuss data points that are mentioned in the context but whose attribution to the subject is ambiguous or uncertain.

CITATION RULES:
- Use the format [Source Title] for every factual claim.
- Available sources:
{source_list}

LANGUAGE RULE:
- Detect the language of the question. Respond strictly in that language.

CONTEXT:
{context_str}
"""
    return system_prompt


class DefaultPromptBuilder:
    """OSS default prompt builder — grounded QA with citation format."""

    def build_prompt(self, question: str, chunks: List[Dict]) -> str:
        return build_prompt(question, chunks)


# Register as default implementation
from retriva.registry import CapabilityRegistry
CapabilityRegistry().register("prompt_builder", DefaultPromptBuilder, priority=100)
