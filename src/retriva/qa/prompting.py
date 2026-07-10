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

from pathlib import Path
from typing import List, Dict

from retriva.config import settings
from retriva.logger import get_logger

logger = get_logger(__name__)


def _citation_label(chunk: Dict) -> str:
    """
    Return the canonical source label for a chunk.

    Citations are grouped by ``filename`` + ``section_path`` so that different
    sections of the same document get distinct citation numbers (e.g. [1] for
    "Network Settings" and [2] for "Power Supply").  This makes citations
    actionable even when the entire knowledge base is a single large document.

    This MUST match the label used when building citations in
    ``chat_completions._build_citations``.  Keeping the two in sync is what
    lets the post-processor map a bracketed reference back to a numbered
    citation chip.
    """
    filename = chunk.get("filename")
    if not filename:
        path = chunk.get("source_path", "unknown")
        filename = Path(path).name
        if filename == "unknown":
            filename = chunk.get("page_title") or "Unknown Source"

    section = chunk.get("section_path") or ""
    if section:
        return f"{filename} — {section}"
    return filename


def _build_context(retrieved_chunks: List[Dict]) -> tuple[str, str]:
    """
    Group retrieved chunks by citation label and return the two pieces that
    every prompt template needs:

    * ``context_str``  — the ``<source>…</source>`` blocks containing the
      chunk text, URL and metadata, each tagged with its numeric ``[N]``
      citation marker.
    * ``source_list``  — the flat numbered list of source labels, shown to
      the model as "Available sources:".

    Both are returned so that the default template and any user-supplied
    override template can consume them with the same ``{context}`` and
    ``{source_list}`` placeholders.
    """
    grouped = {}
    for chunk in retrieved_chunks:
        label = _citation_label(chunk)
        if label not in grouped:
            grouped[label] = {
                "url": chunk.get("canonical_doc_id", chunk.get("source_path", "")),
                "texts": [chunk.get("text", "")],
                "user_metadata": chunk.get("user_metadata", {})
            }
        else:
            # Only add if text is not exactly the same
            new_text = chunk.get("text", "")
            if new_text not in grouped[label]["texts"]:
                grouped[label]["texts"].append(new_text)

            # Merge metadata
            meta = chunk.get("user_metadata", {})
            if meta:
                if not grouped[label].get("user_metadata"):
                    grouped[label]["user_metadata"] = {}
                grouped[label]["user_metadata"].update(meta)

    context_str = ""
    source_list = ""
    for idx, (label, data) in enumerate(grouped.items()):
        citation_number = idx + 1
        url = data["url"]
        combined_text = "\n\n---\n\n".join(data["texts"])

        meta_str = ""
        user_metadata = data.get("user_metadata")
        if user_metadata:
            meta_str = "Metadata tags:\n"
            for k, v in user_metadata.items():
                meta_str += f"- {k}: {v}\n"

        # Build context block tagged with the numeric citation marker so the
        # model can reference it directly as [N].
        context_str += (
            f"\n<source id=\"{citation_number}\">\n"
            f"[{citation_number}] {label}\n"
            f"URL: {url}\n"
            f"{meta_str}"
            f"{combined_text}\n"
            f"</source>\n"
        )
        source_list += f"  - [{citation_number}] {label}\n"

    return context_str, source_list


_DEFAULT_PROMPT_TEMPLATE = """You are Retriva, a Precision Technical Documentation Assistant.
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
- Cite every factual claim using the numeric marker of the source it comes from, in square brackets, e.g. [1] or [2].
- Always use this numeric [N] format, using the exact number shown for each source below. Multiple sources may be cited together, e.g. [1][3].
- Do NOT invent descriptive labels such as [SERVICE MANUAL] or [DATASHEET]; only the numeric form is allowed.
- Available sources:
{source_list}

LANGUAGE RULE:
- Detect the language of the question. Respond strictly in that language.
- When the source context is in a different language from the response language, translate carefully and idiomatically — do not produce word-for-word calques.
- Use natural, fluent phrasing in the response language. For example, when translating from English to Italian, use "da 20 a 5000 ms" (not "20 a 5000 ms"), "passo" or "incremento" (not "step"), "valore predefinito" (not "default").
- Keep technical terms in their established localized form when one exists (e.g. "indirizzo IP", "maschera di sottorete", "gateway"). If no established translation exists, keep the original English term.
- Preserve all numeric values, ranges, and units exactly as they appear in the source — never round or convert units during translation.

CONTEXT:
{context}
"""


def build_prompt(question: str, retrieved_chunks: List[Dict]) -> str:
    """
    Builds the grounded system prompt with Open WebUI-compatible citations.

    The LLM is instructed to cite sources using a numeric ``[N]`` marker that
    matches the numbered source list shown both in the prompt and in the
    rendered "Sources" list.  The downstream post-processor
    (``_build_citation_refs`` / the streaming bracket parser) maps those
    markers to clickable citation chips.  Numbering here is grouped by the
    same key (the citation label) and order used by ``_build_citations`` so
    that source ``[N]`` in the body always lines up with ``[N]`` in the list.

    The default template can be overridden end-to-end (without touching code)
    by setting the ``SYSTEM_PROMPT_OVERRIDE`` environment variable (mapped to
    ``settings.system_prompt_override``).  An override template may use the
    ``{source_list}`` and ``{context}`` placeholders to inject the numbered
    source list and the ``<source>`` context blocks respectively.  If the
    override does not contain either placeholder, the context and source list
    are appended to it verbatim so the model still has the evidence it needs.
    """
    context_str, source_list = _build_context(retrieved_chunks)

    override = settings.system_prompt_override
    if override and override.strip():
        override = override.strip()
        if "{source_list}" in override or "{context}" in override:
            # Safe to format directly; missing placeholders are left as-is
            # by passing them through a dict that supplies defaults.
            try:
                system_prompt = override.format(
                    source_list=source_list,
                    context=context_str,
                )
            except (KeyError, IndexError):
                # User template referenced an unknown placeholder; fall back
                # to appending context so we never starve the model.
                logger.warning(
                    "system_prompt_override contains unknown placeholders; "
                    "appending context/source_list verbatim."
                )
                system_prompt = (
                    override
                    + f"\n\nAvailable sources:\n{source_list}\n\nCONTEXT:\n{context_str}"
                )
        else:
            # No placeholders — append the evidence so the model still sees it.
            system_prompt = (
                override
                + f"\n\nAvailable sources:\n{source_list}\n\nCONTEXT:\n{context_str}"
            )
        logger.info("Using SYSTEM_PROMPT_OVERRIDE for system prompt.")
        return system_prompt

    return _DEFAULT_PROMPT_TEMPLATE.format(source_list=source_list, context=context_str)


class DefaultPromptBuilder:
    """OSS default prompt builder — grounded QA with citation format."""

    def build_prompt(self, question: str, chunks: List[Dict]) -> str:
        return build_prompt(question, chunks)


# Register as default implementation
from retriva.registry import CapabilityRegistry
CapabilityRegistry().register("prompt_builder", DefaultPromptBuilder, priority=100)
