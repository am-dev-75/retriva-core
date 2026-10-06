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

"""Operator commands (Spec 028 §21/§23/§24): ``knowledge status |
adopt | reconcile | verify | purge | authority``.

All commands are tenant-explicit (or explicit global mode), batch
bounded, resumable, idempotent, safe-by-default (dry-run), with
structured output and documented exit codes.  No schedulers.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from retriva.knowledge.adoption import AdoptionMigrator
from retriva.knowledge.authority import (
    AuthorityError,
    AuthorityState,
    KnowledgeAuthority,
)
from retriva.knowledge.deletion import DeletionService
from retriva.knowledge.kb_registry import KBRegistryMigrator
from retriva.knowledge.purge import Purger
from retriva.knowledge.reconcile import Reconciler
from retriva.knowledge.repository import KnowledgeRepository
from retriva.logger import get_logger

_log = get_logger(__name__)

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


class CommandError(RuntimeError):
    pass


def _repo(repository=None) -> KnowledgeRepository:
    return repository or KnowledgeRepository()


def status(tenant_id: Optional[str] = None, *, repository=None) -> Dict:
    repo = _repo(repository)
    authority = KnowledgeAuthority(repo)
    readiness = authority.public_readiness()
    detail: Dict[str, Any] = {"authority": readiness}
    if tenant_id:
        with repo.transaction(tenant_id, privileged=True) as cur:
            cur.execute(
                "SELECT lifecycle_state, count(*) AS n FROM "
                "knowledge.documents WHERE tenant_id=%s GROUP BY "
                "lifecycle_state", (tenant_id,))
            detail["documents"] = {
                r["lifecycle_state"]: int(r["n"]) for r in cur.fetchall()}
            cur.execute(
                "SELECT provenance, count(*) AS n FROM "
                "knowledge.document_versions WHERE tenant_id=%s GROUP BY "
                "provenance", (tenant_id,))
            detail["versions"] = {
                r["provenance"]: int(r["n"]) for r in cur.fetchall()}
            cur.execute(
                "SELECT op_state, count(*) AS n FROM "
                "knowledge.qdrant_operations WHERE tenant_id=%s GROUP BY "
                "op_state", (tenant_id,))
            detail["operations"] = {
                r["op_state"]: int(r["n"]) for r in cur.fetchall()}
    return {"ok": True, "mode": "operator", **detail}


def adopt_kb_registry(tenant_id: str, *, apply: bool = False,
                      repository=None) -> Dict:
    migrator = KBRegistryMigrator(_repo(repository))
    evidence = migrator.read_sqlite()
    report = migrator.apply(tenant_id, evidence, apply=apply)
    payload = report.to_dict()
    if report.invalid:
        return {"ok": False, "exit_code": EXIT_FINDINGS, **payload}
    return {"ok": True, "exit_code": EXIT_OK, **payload}


def adopt(tenant_id: str, collection_name: str, *, apply: bool = False,
          batch: int = 100, checkpoint: int = 0, repository=None,
          qdrant_client=None, catalog_path: Optional[str] = None) -> Dict:
    migrator = AdoptionMigrator(
        _repo(repository), qdrant_client=qdrant_client,
        catalog_path=catalog_path)
    report = migrator.run(
        tenant_id, collection_name, apply=apply, batch=batch,
        checkpoint=checkpoint)
    payload = report.to_dict()
    exit_code = EXIT_OK if not report.conflicts else EXIT_FINDINGS
    return {"ok": exit_code == EXIT_OK, "exit_code": exit_code, **payload}


def reconcile(tenant_id: str, collection_name: str, *,
              apply: bool = False, repository=None,
              qdrant_client=None) -> Dict:
    report = Reconciler(
        _repo(repository), qdrant_client=qdrant_client).run(
        tenant_id, collection_name, apply=apply)
    payload = report.to_dict()
    exit_code = EXIT_OK if not report.findings else EXIT_FINDINGS
    return {"ok": exit_code == EXIT_OK, "exit_code": exit_code, **payload}


def verify(tenant_id: str, document_id: str, collection_name: str, *,
           repository=None, qdrant_client=None) -> Dict:
    repo = _repo(repository)
    with repo.transaction(tenant_id) as cur:
        doc = repo.get_document(
            cur, tenant_id=tenant_id, document_id=document_id)
        if doc is None:
            return {"ok": False, "exit_code": EXIT_FINDINGS,
                    "detail": "document_not_found"}
        version_id = doc.get("current_version_id")
        counts = (repo.count_chunks_by_state(
            cur, tenant_id=tenant_id, version_id=version_id)
            if version_id else {})
    qdrant_count = 0
    if version_id:
        try:
            from retriva.knowledge.visibility import count_version_points
            client = qdrant_client
            if client is None:
                from retriva.indexing.qdrant_store import get_client
                client = get_client()
            qdrant_count = count_version_points(
                client, collection_name, version_id)
        except Exception:
            qdrant_count = -1
    manifest_total = sum(counts.values())
    ok = (manifest_total > 0 and counts.get("verified", 0) == manifest_total
          and qdrant_count == manifest_total)
    return {
        "ok": ok,
        "exit_code": EXIT_OK if ok else EXIT_FINDINGS,
        "document_id": document_id,
        "version_id": version_id,
        "manifest": counts,
        "qdrant_points": qdrant_count,
    }


def purge(tenant_id: Optional[str], *, apply: bool = False,
          batch: int = 100, retention_days: int = 90,
          global_mode: bool = False, repository=None) -> Dict:
    if tenant_id is None and not global_mode:
        raise CommandError(
            "purge requires an explicit tenant or explicit global mode")
    if global_mode:
        tenant_id = None
    report = Purger(_repo(repository)).run(
        tenant_id, apply=apply, batch=batch,
        retention_days=retention_days)
    return {"ok": True, "exit_code": EXIT_OK, **report.to_dict()}


def scan(tenant_id: str, collection_name: str, *, apply: bool = False,
         authoritative: bool = False, repository=None,
         qdrant_client=None, known_kbs=None) -> Dict:
    """Automated Qdrant visible-point scan.  Dry-run by default; with
    ``apply`` the bounded evidence is persisted as a durable
    ``adopt_verify`` operation usable by the authority cutover gate.
    Never mutates Qdrant."""
    authority = KnowledgeAuthority(_repo(repository))
    client = qdrant_client
    if client is None:
        from retriva.indexing.qdrant_store import get_client
        client = get_client()
    if apply:
        op_id, result = authority.compute_cutover_scan(
            tenant_id=tenant_id, collection_name=collection_name,
            client=client, operator="operator",
            known_kbs=known_kbs, authoritative=authoritative)
        payload = result.to_summary()
        payload.update({"op_id": op_id, "exit_code":
                        EXIT_OK if result.ok else EXIT_FINDINGS})
        return {"ok": result.ok, **payload}
    from retriva.knowledge.scan import scan_visible_points

    result = scan_visible_points(
        client, collection_name, tenant_id=tenant_id,
        known_kbs=known_kbs, authoritative=authoritative)
    payload = result.to_summary()
    payload["exit_code"] = EXIT_OK if result.ok else EXIT_FINDINGS
    return {"ok": result.ok, "mode": "dry-run", **payload}


def set_authority(target: str, *, operator: str,
                  evidence: Optional[Dict[str, bool]] = None,
                  adoption_run_ref: Optional[str] = None,
                  equivalence_op_id: Optional[str] = None,
                  note: Optional[str] = None,
                  catalog_frozen: bool = False,
                  sqlite_frozen: bool = False,
                  repository=None) -> Dict:
    authority = KnowledgeAuthority(_repo(repository))
    state = AuthorityState(target)
    if state is AuthorityState.AUTHORITATIVE:
        report = authority.set_authoritative(
            operator=operator, evidence=evidence or {},
            adoption_run_ref=adoption_run_ref,
            equivalence_op_id=equivalence_op_id, note=note)
    else:
        report = authority.transition(
            state, operator=operator, note=note,
            adoption_run_ref=adoption_run_ref,
            catalog_frozen=catalog_frozen,
            sqlite_frozen=sqlite_frozen)
    return {"ok": True, "exit_code": EXIT_OK,
            "authority": {k: (v.isoformat() if hasattr(v, "isoformat")
                              else v)
                          for k, v in (report or {}).items()}}
