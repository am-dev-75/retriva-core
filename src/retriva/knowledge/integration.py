# Copyright (C) 2026 Retriva.
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

"""Workflow adapters over the ONE common knowledge service (Spec 028
§20).

- ``document`` adapter  (generic ``v2_document``): source identity is
  the server-side ``source_uri`` treated as LEGACY ``path:`` evidence
  for existing documents; new programmatic sources use ``internal:``.
- ``upload`` adapter    (``v2_upload``): source identity is
  ``upload:<uploader-context>:<filename>``; the raw client path is not
  identity.
- ``mediawiki`` adapter (``v2_mediawiki``): source identity is
  ``mediawiki:<site>:page:<page-id>`` (page identity, NOT revision).

Adapters only normalize workflow-specific source identity/metadata and
delegate every state transition to :class:`KnowledgeService`; they
contain no independent state machines.

Runtime gating: :meth:`KnowledgeIntegration.available` is True only
when the ``knowledge`` schema is present.  This keeps the pre-migration
runtime behaviour intact (legacy path) while ensuring that once the
schema exists, native metadata-dependent ingestion is fail-closed
until authority is ``authoritative``.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Sequence

from retriva.knowledge.authority import (
    AuthorityError,
    KnowledgeAuthority,
)
from retriva.knowledge.ids import (
    internal_identity,
    mediawiki_identity,
    upload_identity,
)
from retriva.knowledge.repository import (
    KnowledgeRepository,
    KnowledgeRepositoryError,
)
from retriva.knowledge.service import KnowledgeService
from retriva.logger import get_logger

_log = get_logger(__name__)


class KnowledgeIntegration:
    """Adapter facade used by the three ingestion workflows."""

    def __init__(self, repository: Optional[KnowledgeRepository] = None,
                 authority: Optional[KnowledgeAuthority] = None):
        self._repo = repository or KnowledgeRepository()
        self._authority = authority or KnowledgeAuthority(self._repo)
        self._service = KnowledgeService(self._repo)

    @property
    def service(self) -> KnowledgeService:
        return self._service

    # -- gating ----------------------------------------------------------

    def available(self) -> bool:
        """True only when the knowledge schema is present.  Never
        raises: an unavailable/absent schema means the legacy path
        continues unchanged."""
        try:
            self._authority.read_state()
            return True
        except Exception:
            return False

    def require_native_ingestion(self) -> None:
        self._authority.require_native_ingestion()

    # -- adapters --------------------------------------------------------

    def document_identity(self, source_uri: str, *,
                          logical_ref: Optional[str] = None):
        """Generic v2 document: prefer an explicit stable logical ref
        (``internal:``); otherwise the server-side ``source_uri`` is
        recorded as LEGACY ``path:`` evidence via the adoption identity
        helper, never treated as a content hash."""
        if logical_ref:
            return internal_identity(logical_ref)
        from retriva.knowledge.ids import legacy_path_identity

        return legacy_path_identity(source_uri)

    def upload_identity(self, kb_id: str, source_path: str,
                        filename: Optional[str] = None):
        return upload_identity(
            kb_id, filename or os.path.basename(source_path or ""),
            source_path=source_path)

    def mediawiki_identity(self, xml_path: str, page_id: object,
                           *, site_identity: Optional[str] = None):
        site = site_identity or os.path.splitext(
            os.path.basename(xml_path or "mediawiki"))[0]
        return mediawiki_identity(site, page_id)

    # -- lifecycle delegation -------------------------------------------

    def begin_upload(self, *, tenant_id: str, kb_id: str,
                     source_path: str, filename: Optional[str],
                     collection_name: str, job_id: str,
                     content_fingerprint: Optional[str],
                     content_size: Optional[int] = None,
                     media_type: Optional[str] = None,
                     user_metadata: Optional[Dict[str, Any]] = None):
        return self._service.register_submission(
            tenant_id=tenant_id,
            identity=self.upload_identity(kb_id, source_path, filename),
            kb_ids=[kb_id], collection_name=collection_name,
            job_id=job_id, job_type="v2_upload",
            content_fingerprint=content_fingerprint,
            content_size=content_size, media_type=media_type,
            user_metadata=user_metadata)

    def begin_document(self, *, tenant_id: str, source_uri: str,
                       kb_id: str, collection_name: str, job_id: str,
                       content_fingerprint: Optional[str],
                       logical_ref: Optional[str] = None,
                       content_size: Optional[int] = None,
                       media_type: Optional[str] = None,
                       user_metadata: Optional[Dict[str, Any]] = None):
        return self._service.register_submission(
            tenant_id=tenant_id,
            identity=self.document_identity(
                source_uri, logical_ref=logical_ref),
            kb_ids=[kb_id], collection_name=collection_name,
            job_id=job_id, job_type="v2_document",
            content_fingerprint=content_fingerprint,
            content_size=content_size, media_type=media_type,
            user_metadata=user_metadata)

    def begin_mediawiki_page(self, *, tenant_id: str, xml_path: str,
                             page_id: object, kb_id: str,
                             collection_name: str,
                             content_fingerprint: str,
                             source_revision: Optional[str] = None,
                             content_size: Optional[int] = None,
                             title: Optional[str] = None,
                             user_metadata: Optional[Dict[str, Any]] = None,
                             job_id: Optional[str] = None
                             ):
        return self._service.register_page_version(
            tenant_id=tenant_id,
            identity=self.mediawiki_identity(xml_path, page_id),
            kb_ids=[kb_id], collection_name=collection_name,
            content_fingerprint=content_fingerprint,
            source_revision=source_revision, content_size=content_size,
            title=title, user_metadata=user_metadata, job_id=job_id)

    # -- completion / failure -------------------------------------------

    def complete_verified(self, *, tenant_id: str, document_id: str,
                          version_id: str, ingestion_id: str,
                          prior_version_id: Optional[str] = None,
                          observed_chunk_count: Optional[int] = None
                          ) -> bool:
        return self._service.finalize_verified(
            tenant_id=tenant_id, document_id=document_id,
            version_id=version_id, ingestion_id=ingestion_id,
            prior_version_id=prior_version_id,
            observed_chunk_count=observed_chunk_count)

    def fail(self, *, tenant_id: str, ingestion_id: str,
             version_id: Optional[str] = None,
             error_code: str = "ingestion_failed") -> None:
        self._service.fail_ingestion(
            tenant_id=tenant_id, ingestion_id=ingestion_id,
            version_id=version_id, error_code=error_code)
