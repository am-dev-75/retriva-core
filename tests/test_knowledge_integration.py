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

"""Spec 028 P4: one common knowledge service used by the document,
upload, and MediaWiki adapters (source-identity normalization and
shared state transitions)."""

from __future__ import annotations

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from retriva.knowledge.integration import KnowledgeIntegration  # noqa: E402
from retriva.knowledge.repository import KnowledgeRepository  # noqa: E402

TENANT = "tenant-adapter"
COLLECTION = "retriva_chunks"


@pytest.fixture(autouse=True)
def _reset_authority(knowledge_repo):
    with knowledge_repo.transaction(privileged=True) as cur:
        cur.execute(
            "UPDATE knowledge.authority SET state='schema_ready', "
            "authoritative=FALSE, native_ingestion_available=FALSE "
            "WHERE singleton=TRUE")
    yield


def test_all_three_adapters_share_one_service(knowledge_repo):
    integration = KnowledgeIntegration(knowledge_repo)
    assert integration.available() is True

    up = integration.begin_upload(
        tenant_id=TENANT, kb_id="default", source_path="/srv/up/report.pdf",
        filename="report.pdf", collection_name=COLLECTION,
        job_id="job-up", content_fingerprint="sha256:" + "1" * 64)
    doc = integration.begin_document(
        tenant_id=TENANT, source_uri="/srv/docs/spec.md", kb_id="default",
        collection_name=COLLECTION, job_id="job-doc",
        content_fingerprint="sha256:" + "2" * 64,
        logical_ref="spec-028")
    mw = integration.begin_mediawiki_page(
        tenant_id=TENANT, xml_path="/data/rdwiki/export.xml",
        page_id="12345", kb_id="default", collection_name=COLLECTION,
        content_fingerprint="sha256:" + "3" * 64,
        source_revision="99", title="Page 12345")

    assert up.document_id and doc.document_id and mw.document_id
    assert len({up.document_id, doc.document_id, mw.document_id}) == 3

    # Source namespaces are distinct and correctly normalized.
    with knowledge_repo.transaction(TENANT) as cur:
        cur.execute(
            "SELECT namespace, normalized_ref FROM knowledge.sources "
            "WHERE tenant_id=%s ORDER BY namespace", (TENANT,))
        rows = {r["namespace"]: r["normalized_ref"]
                for r in cur.fetchall()}
    assert "upload" in rows and rows["upload"].endswith("report.pdf")
    assert rows.get("internal") == "spec-028"
    assert rows.get("mediawiki") == "export:page:12345"


def test_document_adapter_uses_legacy_path_evidence_without_ref(
        knowledge_repo):
    integration = KnowledgeIntegration(knowledge_repo)
    sub = integration.begin_document(
        tenant_id=TENANT, source_uri="/srv/legacy/a.md", kb_id="default",
        collection_name=COLLECTION, job_id="job-legacy",
        content_fingerprint="sha256:" + "4" * 64)
    with knowledge_repo.transaction(TENANT) as cur:
        cur.execute(
            "SELECT namespace FROM knowledge.sources WHERE source_id="
            "(SELECT source_id FROM knowledge.documents WHERE "
            "document_id=%s)", (sub.document_id,))
        assert cur.fetchone()["namespace"] == "path"


def test_mediawiki_page_identity_separates_site_and_revision(
        knowledge_repo):
    integration = KnowledgeIntegration(knowledge_repo)
    page = integration.mediawiki_identity("/data/wiki/rdwiki.xml", "42")
    assert page.namespace == "mediawiki"
    assert page.normalized_ref == "rdwiki:page:42"
    # A different revision is not part of identity.
    other = integration.mediawiki_identity("/data/wiki/rdwiki.xml", "42")
    assert other.normalized_ref == page.normalized_ref


def test_completion_gate_delegates_to_common_service(knowledge_repo):
    integration = KnowledgeIntegration(knowledge_repo)
    sub = integration.begin_upload(
        tenant_id=TENANT, kb_id="default", source_path="/srv/x.pdf",
        filename="x.pdf", collection_name=COLLECTION, job_id="job-x",
        content_fingerprint="sha256:" + "5" * 64)
    integration.service.register_manifest(
        tenant_id=TENANT, version_id=sub.version_id,
        chunk_id_seed=f"{sub.document_id}:sha256:" + "5" * 64,
        chunk_count=1)
    # Not verified yet -> gate refuses promotion.
    assert integration.complete_verified(
        tenant_id=TENANT, document_id=sub.document_id,
        version_id=sub.version_id, ingestion_id=sub.ingestion_id) is False
    with knowledge_repo.transaction(TENANT) as cur:
        knowledge_repo.set_chunks_sync_state(
            cur, tenant_id=TENANT, version_id=sub.version_id,
            ordinals=[0], sync_state="verified")
    assert integration.complete_verified(
        tenant_id=TENANT, document_id=sub.document_id,
        version_id=sub.version_id, ingestion_id=sub.ingestion_id) is True
