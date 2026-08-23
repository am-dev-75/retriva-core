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
chat LLM.

This extractor uses the existing ``settings.chat_*`` configuration to
call an OpenAI-compatible LLM and extract entities and assertions from
chunk text.  It does NOT contain any domain-specific logic.

Extensions can override this by registering a higher-priority
``entity_extractor`` capability.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List
from uuid import uuid4

from retriva.config import settings
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
- Return ONLY the JSON object, no other text.
"""


class DefaultEntityExtractor:
    """Generic entity extractor using the chat LLM.

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

        client = OpenAI(
            api_key=settings.chat_openai_api_key,
            base_url=settings.chat_base_url,
        )

        try:
            response = client.chat.completions.create(
                model=settings.chat_model,
                messages=[
                    {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": text_block},
                ],
                temperature=0.0,
                top_p=1.0,
                max_tokens=4096,
            )
            content = response.choices[0].message.content or ""
            extraction = self._parse_extraction(content)
        except Exception as e:
            logger.error(
                f"DefaultEntityExtractor: LLM extraction failed for "
                f"doc={source_document_id}: {e}"
            )
            return GraphMutationRequest(
                tenant_id=tenant_id,
                kb_id=kb_id,
                source_document_id=source_document_id,
                source_chunk_ids=chunk_ids,
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

    def _parse_extraction(self, content: str) -> Dict[str, Any]:
        """Parse the LLM response as JSON, with fallback."""
        # Strip markdown code fences if present
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            logger.warning(
                f"DefaultEntityExtractor: failed to parse LLM output as JSON: {e}"
            )
            return {"entities": [], "assertions": []}

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
