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
Default entity extractor — generic, domain-neutral extraction using the
LLM.

This extractor uses the ``task_*`` LLM configuration (falling back to
``chat_*``) to extract entities and assertions from chunk text.  It does
NOT contain any domain-specific logic.

Extraction robustness (root-cause fix, 2026-09):
- Reasoning models spend their completion budget on chain-of-thought
  BEFORE emitting the JSON answer.  With a small ``max_tokens`` the budget
  is exhausted by reasoning and ``content`` comes back EMPTY with
  ``finish_reason="length"``.  The extractor therefore:
  * uses the task LLM (``task_reasoning_effort`` defaults to ``low``),
  * checks ``finish_reason`` and treats ``length`` as a retryable failure,
  * validates the JSON against an explicit schema,
  * handles code fences and surrounding prose safely,
  * performs bounded retries ONLY for retryable failures (length,
    transient provider errors),
  * records extraction metrics and warnings instead of silently
    converting malformed output into a valid empty graph.

Extensions can override this by registering a higher-priority
``entity_extractor`` capability.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional
from uuid import uuid4

from retriva.config import settings
from retriva.logger import get_logger
from retriva.graph.contracts import (
    Assertion,
    AssertionClass,
    Entity,
    EntityCategory,
    GraphMutationRequest,
)
from retriva.logger import get_logger

logger = get_logger(__name__)

EXTRACTION_SYSTEM_PROMPT = """You are a knowledge-graph extraction engine.
Extract entities and assertions from the given text.

Return a JSON object with this exact structure:
{
  "entities": [
    {
      "name": "Entity Name",
      "category": "Person|Organization|Product|Project|Location|Event|Technology|Regulation|Concept|Unknown",
      "description": "Brief description (optional)",
      "aliases": ["alias1", "alias2"]
    }
  ],
  "assertions": [
    {
      "subject": "Subject Entity Name",
      "predicate": "retriva:predicateName",
      "object": "Object Entity Name or literal value",
      "is_literal": false,
      "confidence": 0.85,
      "evidence_chunk_index": 0
    }
  ]
}

Rules:
- Use retriva: namespace for predicates (e.g. retriva:worksFor, retriva:locatedIn).
- Only extract clearly stated facts. Do not infer or hallucinate.
- Confidence: 0.0-1.0 based on how explicitly the text states the assertion.
- Map entity names to categories using the provided category list.
- Be CONCISE: emit at most 20 entities and 30 assertions. Do not enumerate
  every attribute as a separate assertion.
- Your response MUST be a single JSON object and nothing else: no markdown
  fences, no commentary, no reasoning text.
"""

# Bounded extraction output: keeps the JSON well within the completion
# budget even for reasoning models.
_MAX_ENTITIES = 20
_MAX_ASSERTIONS = 30

# Retryable failure kinds (transient or budget-related).
_RETRYABLE_FAILURES = {"length", "timeout", "provider_error"}


class ExtractionFailure(Exception):
    """Raised when extraction output cannot be obtained or validated.

    ``failure_kind`` distinguishes retryable failures (output truncated by
    the token budget, provider timeouts) from non-retryable ones (schema-
    invalid JSON that a retry would not fix).
    """

    def __init__(self, kind: str, detail: str, metrics: Optional[Dict[str, Any]] = None):
        self.kind = kind  # "length" | "timeout" | "provider_error" | "schema" | "empty"
        self.detail = detail
        self.metrics = metrics or {}
        super().__init__(f"{kind}: {detail}")


def _strip_fences(content: str) -> str:
    """Strip markdown code fences anywhere in the content (safely)."""
    content = content.strip()
    # Fenced block anywhere: prefer the fenced content.
    fence_match = re.search(
        r"```(?:json)?\s*\n?(.*?)\n?\s*```", content, re.DOTALL
    )
    if fence_match:
        return fence_match.group(1).strip()
    # Leading/trailing fence without closing (truncated output).
    content = re.sub(r"^```(?:json)?\s*", "", content)
    content = re.sub(r"\s*```$", "", content)
    return content.strip()


def _extract_json_object(content: str) -> Optional[Dict[str, Any]]:
    """Extract the outermost JSON object from LLM output.

    Handles: raw JSON, fenced JSON, and JSON surrounded by prose.  Returns
    None when no parseable JSON object is present.
    """
    candidate = _strip_fences(content)
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    # Prose-wrapped: find the first '{' and try progressively from the LAST
    # '}' (outermost object), tolerating trailing prose.
    start = candidate.find("{")
    if start < 0:
        return None
    end = candidate.rfind("}")
    if end <= start:
        return None
    try:
        parsed = json.loads(candidate[start:end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _validate_extraction_schema(data: Dict[str, Any]) -> List[str]:
    """Validate extraction output against the explicit schema.

    Returns a list of schema problems (empty = valid).  Malformed entries
    are reported; the caller decides whether to keep the valid remainder.
    """
    errors: List[str] = []
    if not isinstance(data, dict):
        return ["extraction is not a JSON object"]
    entities = data.get("entities")
    assertions = data.get("assertions", [])
    if not isinstance(entities := data.get("entities", []), list):
        errors.append("'entities' must be a list")
    elif not isinstance(entities, list):  # pragma: no cover
        errors.append("'entities' must be a list")
    if not isinstance(data.get("assertions", []), list):
        errors.append("'assertions' must be a list")
    for i, ent in enumerate(entities if isinstance(entities, list) else []):
        if not isinstance(ent, dict):
            errors.append(f"entities[{i}] must be an object")
            continue
        if not str(ent.get("name", "")).strip():
            errors.append(f"entities[{i}].name is empty")
    for i, ast in enumerate(data.get("assertions", []) or []):
        if not isinstance(ast, dict):
            errors.append(f"assertions[{i}] must be an object")
            continue
        if not str(ast.get("subject", "")).strip():
            errors.append(f"assertions[{i}].subject is empty")
        if not str(ast.get("predicate", "")).strip():
            errors.append(f"assertions[{i}].predicate is empty")
        if "object" not in ast:
            errors.append(f"assertions[{i}].object is missing")
    return errors


class DefaultEntityExtractor:
    """Generic entity extractor using the task LLM.

    Registered as ``entity_extractor`` at priority 100.
    """

    PROFILE_ID = "retriva:default"
    VERSION = "0.1.0"

    def extract(
        self,
        chunks: List[Dict[str, Any]],
        profile_id: str,
        tenant_id: str,
        kb_id: str,
        source_document_id: str,
    ) -> GraphMutationRequest:
        """Extract candidate entities and assertions from chunks.

        Returns a :class:`GraphMutationRequest` with candidate entities
        (temporary IDs) and assertions.  Entity IDs are assigned by the
        :class:`EntityResolutionService` during indexing.
        """
        from openai import OpenAI

        # Collect chunk texts for extraction
        chunk_texts = []
        chunk_ids = []
        for i, chunk in enumerate(chunks):
            text = chunk.get("text", "")
            if not text or len(text.strip()) < 20:
                continue
            chunk_texts.append((i, text))
            chunk_ids.append(chunk.get("chunk_id", f"chunk_{i}"))

        if not chunk_texts:
            return GraphMutationRequest(
                tenant_id=tenant_id,
                kb_id=kb_id,
                source_document_id=source_document_id,
                source_chunk_ids=chunk_ids,
            )

        # Build extraction prompt
        text_block = "\n\n".join(
            f"[CHUNK {i}]\n{text}" for i, text in chunk_texts
        )

        # Bounded input: oversized chunk blocks starve the completion budget
        # and push the model into long reasoning.  Cap the input text.
        max_input_chars = 12000
        input_truncated = len(text_block) > max_input_chars
        if len(text_block) > max_input_chars:
            text_block = text_block[:max_input_chars]

        extraction, metrics = self._extract_with_retries(text_block)
        self.last_metrics = metrics
        if input_truncated:
            bounded_warning = (
                "Graph extraction analyzed a bounded portion of this "
                "document. Relevant entities or relationships outside the "
                "selected content may not have been extracted."
            )
            metrics.setdefault("warnings", []).append(bounded_warning)
            metrics["input_truncated"] = True
        if metrics.get("warnings"):
            for w in metrics["warnings"]:
                logger.warning(
                    f"DefaultEntityExtractor: doc={source_document_id} {w}"
                )
        if extraction is None:
            # Explicit failure — NEVER silently convert malformed output into
            # a valid empty graph.  The indexer records the failure metrics.
            raise ExtractionFailure(
                metrics.get("failure_kind", "schema"),
                metrics.get("failure_detail", "extraction failed"),
                metrics,
            )

        # Build candidate entities (with temporary IDs)
        entity_name_to_temp_id: Dict[str, str] = {}
        candidate_entities: List[Entity] = []
        for ent_data in extraction.get("entities", []):
            name = ent_data.get("name", "").strip()
            if not name:
                continue
            temp_id = f"tmp_{uuid4().hex[:16]}"
            entity_name_to_temp_id[name] = temp_id

            category = self._map_category(ent_data.get("category", "Unknown"))
            candidate_entities.append(Entity(
                entity_id=temp_id,
                tenant_id=tenant_id,
                kb_id=kb_id,
                name=name,
                name_normalized=name.strip().lower(),
                category=category,
                aliases=ent_data.get("aliases", []),
                description=ent_data.get("description"),
                security_scope=[kb_id],
            ))

        # Build assertions
        candidate_assertions: List[Assertion] = []
        for ast_data in extraction.get("assertions", []):
            subject_name = ast_data.get("subject", "").strip()
            predicate = ast_data.get("predicate", "").strip()
            if not subject_name or not predicate:
                continue

            subject_temp_id = entity_name_to_temp_id.get(subject_name)
            if not subject_temp_id:
                # Create entity if not already present
                subject_temp_id = f"tmp_{uuid4().hex[:16]}"
                entity_name_to_temp_id[subject_name] = subject_temp_id
                candidate_entities.append(Entity(
                    entity_id=subject_temp_id,
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    name=subject_name,
                    name_normalized=subject_name.strip().lower(),
                    security_scope=[kb_id],
                ))

            object_value = ast_data.get("object", "").strip()
            is_literal = ast_data.get("is_literal", False)
            object_entity_id = None
            object_literal = None

            if is_literal:
                object_literal = object_value
            else:
                object_entity_id = entity_name_to_temp_id.get(object_value)
                if not object_entity_id:
                    # Create object entity
                    object_entity_id = f"tmp_{uuid4().hex[:16]}"
                    entity_name_to_temp_id[object_value] = object_entity_id
                    candidate_entities.append(Entity(
                        entity_id=object_entity_id,
                        tenant_id=tenant_id,
                        kb_id=kb_id,
                        name=object_value,
                        name_normalized=object_value.strip().lower(),
                        security_scope=[kb_id],
                    ))

            # Map evidence chunk
            evidence_idx = ast_data.get("evidence_chunk_index", 0)
            source_chunk_id = (
                chunk_ids[evidence_idx]
                if evidence_idx < len(chunk_ids)
                else chunk_ids[0] if chunk_ids else ""
            )

            candidate_assertions.append(Assertion(
                tenant_id=tenant_id,
                kb_id=kb_id,
                subject_entity_id=subject_temp_id,
                predicate=predicate,
                object_entity_id=object_entity_id,
                object_value=object_literal,
                assertion_class=AssertionClass.EXTRACTED,
                source_document_ids=[source_document_id],
                source_chunk_ids=[source_chunk_id] if source_chunk_id else [],
                extraction_confidence=float(ast_data.get("confidence", 0.5)),
                extractor_profile=self.PROFILE_ID,
                extractor_version=self.VERSION,
                security_scope=[kb_id],
            ))

        logger.info(
            f"DefaultEntityExtractor: extracted "
            f"{len(candidate_entities)} entities, "
            f"{len(candidate_assertions)} assertions from doc={source_document_id}"
        )

        return GraphMutationRequest(
            tenant_id=tenant_id,
            kb_id=kb_id,
            entities=candidate_entities,
            assertions=candidate_assertions,
            source_document_id=source_document_id,
            source_chunk_ids=chunk_ids,
        )

    def _extract_with_retries(
        self, text_block: str, max_attempts: int = 2
    ) -> tuple:
        """Call the task LLM with finish_reason handling and bounded retries.

        Retryable failures (output truncated by the token budget, provider
        timeouts, transient provider errors) are retried up to
        ``max_attempts``.  Non-retryable schema failures fail immediately.

        Returns ``(extraction_dict_or_None, metrics_dict)``.  ``extraction``
        is None exactly when the extraction failed; the metrics carry the
        failure kind, detail, and diagnostics (no source content).
        """
        from openai import OpenAI

        client = OpenAI(
            api_key=settings.task_openai_api_key or settings.chat_openai_api_key,
            base_url=settings.task_base_url or settings.chat_base_url,
        )
        # Task LLM: batch extraction must NOT inherit the chat reasoning
        # effort ('xhigh' consumes the whole completion budget on
        # chain-of-thought, leaving content empty with finish_reason=length).
        task_effort = getattr(settings, "task_reasoning_effort", "") or "low"
        extra_kwargs: dict = {}
        if task_effort.strip():
            extra_kwargs["extra_body"] = {"reasoning_effort": task_effort.strip()}

        metrics: Dict[str, Any] = {
            "model": settings.task_model or settings.chat_model,
            "input_chars": len(text_block),
            "max_tokens": settings.task_max_tokens or settings.chat_max_tokens,
            "attempts": 0,
            "warnings": [],
        }

        last_error: Optional[str] = None
        for attempt in range(1, max_attempts + 1):
            metrics["attempts"] = attempt
            try:
                response = client.chat.completions.create(
                    model=settings.task_model or settings.chat_model,
                    messages=[
                        {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                        {"role": "user", "content": text_block},
                    ],
                    temperature=settings.task_temperature,
                    max_tokens=settings.task_max_tokens or settings.chat_max_tokens,
                    **extra_kwargs,
                )
            except Exception as e:  # provider/transport error — retryable
                last_error = f"provider_error: {type(e).__name__}: {e}"
                metrics["warnings"].append(
                    f"attempt {attempt}: provider error ({type(e).__name__})"
                )
                continue

            choice = response.choices[0] if response.choices else None
            if choice is None:
                last_error = "provider_error: empty choices"
                metrics["warnings"].append(f"attempt {attempt}: empty choices")
                continue

            finish_reason = getattr(choice, "finish_reason", None)
            content = choice.message.content or ""
            usage = getattr(response, "usage", None)
            metrics.update({
                "finish_reason": finish_reason,
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "content_chars": len(content),
                "reasoning_chars": len(getattr(choice.message, "reasoning", "") or ""),
            })

            if finish_reason == "length" and not content.strip():
                # Reasoning consumed the entire budget; nothing was emitted.
                last_error = (
                    "length: completion budget exhausted by reasoning tokens "
                    "before any JSON was emitted"
                )
                metrics["warnings"].append(
                    f"attempt {attempt}: finish_reason=length with empty "
                    "content (reasoning consumed the budget)"
                )
                continue

            parsed = _extract_json_object(content)
            if parsed is None:
                last_error = f"schema: no parseable JSON object in output"
                metrics["warnings"].append(
                    f"attempt {attempt}: no parseable JSON object "
                    f"(finish_reason={finish_reason}, {len(content)} chars)"
                )
                # Truncated JSON (finish_reason=length with partial content)
                # is retryable; prose-wrapped garbage usually is not, but a
                # single retry is cheap and bounded.
                continue

            schema_errors = _validate_extraction_schema(parsed)
            if schema_errors:
                # Non-retryable: the model produced structurally invalid
                # data.  Keep the valid remainder only when entities is a
                # proper non-empty list; otherwise fail explicitly (never
                # silently convert malformed output into a valid graph).
                metrics["warnings"].append(
                    f"schema problems: {'; '.join(schema_errors[:5])}"
                )
                entities = parsed.get("entities")
                if not isinstance(entities, list) or not entities:
                    last_error = f"schema: {'; '.join(schema_errors[:3])}"
                    metrics["failure_kind"] = "schema"
                    metrics["failure_detail"] = last_error
                    return None, metrics

            metrics["ok"] = True
            return parsed, metrics

        metrics["failure_kind"] = "length" if last_error and last_error.startswith("length") else (
            "provider_error" if last_error and last_error.startswith("provider_error") else "schema"
        )
        metrics["failure_detail"] = last_error or "unknown extraction failure"
        return None, metrics

    def _parse_extraction(self, content: str) -> Dict[str, Any]:
        """Parse the LLM response as JSON.

        .. deprecated::
           Retained for backward compatibility with existing callers/tests.
           New code should use :meth:`_extract_with_retries`, which adds
           finish_reason handling, schema validation, and bounded retries.
           Malformed output raises nothing here — it returns an empty
           extraction (legacy behavior).
        """
        parsed = _extract_json_object(content)
        if parsed is None:
            logger.warning(
                "DefaultEntityExtractor: failed to parse LLM output as JSON"
            )
            return {"entities": [], "assertions": []}
        return parsed

    @staticmethod
    def _map_category(category_str: str) -> EntityCategory:
        """Map a category string to an :class:`EntityCategory`."""
        mapping = {
            "person": EntityCategory.PERSON,
            "organization": EntityCategory.ORGANIZATION,
            "product": EntityCategory.PRODUCT,
            "project": EntityCategory.PROJECT,
            "location": EntityCategory.LOCATION,
            "event": EntityCategory.EVENT,
            "technology": EntityCategory.TECHNOLOGY,
            "regulation": EntityCategory.REGULATION,
            "concept": EntityCategory.CONCEPT,
        }
        return mapping.get(category_str.strip().lower(), EntityCategory.UNKNOWN)
