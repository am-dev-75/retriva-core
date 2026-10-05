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

"""Durable v2 artifact storage helpers (Spec 026 / ADR-031).

Server-generated, path-safe storage references for the v2 artifact
workflow: deterministic final/partial/provenance paths scoped to the
artifact storage directory, bounded provenance sidecars for the
finalized→crash→missing-success reconciliation window, deterministic
media types, and the evidence-based adoption used by reconciliation.

The generated artifact FILE is owned by the artifact store: job
retention never deletes it (ADR-031; the artifact may outlive its
job-history record).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from retriva.logger import get_logger

logger = get_logger(__name__)

_PROVENANCE_SUFFIX = ".prov.json"
_PARTIAL_SUFFIX = ".partial"

#: Deterministic media types for the supported output formats; safe
#: binary fallback for anything unknown.
MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".docx": ("application/vnd.openxmlformats-officedocument."
              "wordprocessingml.document"),
    ".xlsx": ("application/vnd.openxmlformats-officedocument."
              "spreadsheetml.sheet"),
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
}

#: Format key → file extension whitelist (the only extensions a
#: server-generated reference may use).
FORMAT_EXTENSIONS = {
    "pdf": ".pdf",
    "markdown": ".md",
    "docx": ".docx",
    "xlsx": ".xlsx",
    "odt": ".odt",
    "ods": ".ods",
    "odp": ".odp",
}

_ARTIFACT_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def media_type_for(filename: str) -> str:
    return MEDIA_TYPES.get(Path(filename).suffix.lower(),
                           "application/octet-stream")


def validate_artifact_id(artifact_id: str) -> str:
    """Server-generated artifact ids are uuid4 hex; anything else is
    rejected before it can touch a path (traversal-proof)."""
    if not artifact_id or not _ARTIFACT_ID_RE.match(artifact_id):
        raise ValueError("invalid artifact id")
    return artifact_id


def validate_format_extension(format_key: str) -> str:
    ext = FORMAT_EXTENSIONS.get(format_key)
    if ext is None:
        raise ValueError(f"unsupported artifact format {format_key!r}")
    return ext


def artifact_paths(base_dir: Path, artifact_id: str, ext: str):
    """Deterministic (final, partial, provenance) paths for one
    artifact.  All three live in the SAME directory (same filesystem
    ⇒ atomic ``os.replace`` is available).  Raises on any non-
    server-generated identifier or extension."""
    validate_artifact_id(artifact_id)
    if not ext.startswith(".") or "/" in ext or "\\" in ext or \
            ".." in ext:
        raise ValueError("invalid artifact extension")
    directory = Path(base_dir)
    final = (directory / f"{artifact_id}{ext}").resolve()
    # Path-traversal safety net: every path must stay inside the
    # resolved artifact root.
    root = directory.resolve()
    for p in (final,):
        if root not in p.parents:
            raise ValueError("artifact path escapes the artifact root")
    return final, Path(f"{final}{_PARTIAL_SUFFIX}"), \
        Path(f"{final}{_PROVENANCE_SUFFIX}")


def hash_file(path: Path) -> tuple:
    """Streaming (size, sha256) of a complete file."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def write_provenance(prov_path: Path, *, tenant_id: str,
                     artifact_id: str, job_id: str, format: str,
                     sha256: str, size: int, storage_ref: str) -> None:
    """Bounded provenance sidecar (atomic write) for the
    finalized→crash→missing-success reconciliation window.  Bounded
    evidence only: identities, format, checksum, size — no content,
    no paths outside the artifact root, no secrets."""
    payload = {
        "tenant_id": tenant_id,
        "artifact_id": artifact_id,
        "job_id": job_id,
        "format": format,
        "sha256": sha256,
        "size": int(size),
        "storage_ref": storage_ref,
    }
    text = json.dumps(payload, sort_keys=True)
    tmp = Path(f"{prov_path}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, prov_path)


def read_provenance(prov_path: Path) -> Optional[Dict[str, Any]]:
    """Tolerant read: missing or corrupt provenance is NOT evidence
    (returns None; callers treat that as unproven)."""
    try:
        data = json.loads(prov_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def provenance_matches(prov: Optional[Dict[str, Any]], *,
                       tenant_id: str, artifact_id: str, job_id: str,
                       sha256: str, size: int) -> bool:
    if not prov:
        return False
    return (
        prov.get("tenant_id") == tenant_id
        and prov.get("artifact_id") == artifact_id
        and prov.get("job_id") == job_id
        and prov.get("sha256") == sha256
        and prov.get("size") == size
    )


def quarantine_partial(partial: Path, prov_path: Path) -> None:
    """Conservative cleanup of abandoned partial output (and any
    stale provenance sidecar) — bounded, best-effort."""
    for p in (partial, prov_path):
        try:
            if p.exists():
                p.unlink()
        except OSError:
            logger.warning(
                "artifact quarantine cleanup failed (best-effort): "
                "path=%s", p.name)


def adopt_finalized_artifact(service, job, attempt) -> bool:
    """Reconciliation evidence adapter (Spec 026 §17): adopt a
    finalized artifact ONLY when provenance proves it belongs to
    this tenant, artifact id, and durable job, and the file's actual
    checksum/size match the sidecar.  Never reruns provider work.

    Returns True when the job was durably adopted as ``succeeded``
    with bounded result metadata; False on ANY uncertainty (the
    caller then parks the job in ``manual_review``).
    """
    from retriva.jobs.domain import SanitizedError

    input_meta = job.input_metadata or {}
    artifact_id = input_meta.get("artifact_id")
    format_key = input_meta.get("format")
    if not artifact_id or not format_key:
        return False
    try:
        ext = validate_format_extension(format_key)
        validate_artifact_id(artifact_id)
    except ValueError:
        return False
    base = _artifact_base_dir()
    if base is None:
        return False
    final, _partial, prov_path = artifact_paths(base, artifact_id, ext)
    if not final.exists():
        return False
    prov = read_provenance(prov_path)
    if prov is None:
        return False
    try:
        size, sha256 = hash_file(final)
    except OSError:
        return False
    if not provenance_matches(
            prov, tenant_id=job.tenant_id, artifact_id=artifact_id,
            job_id=job.id, sha256=sha256, size=size):
        return False
    storage_ref = str(final.relative_to(base))
    result = {
        "artifact_id": artifact_id,
        "storage_ref": storage_ref,
        "media_type": media_type_for(final.name),
        "size": size,
        "sha256": sha256,
    }
    adopted = service.repo.complete_success(
        tenant_id=job.tenant_id, job_id=job.id,
        attempt_id=attempt.id, result_metadata=result,
        detail={"adopted_from": "finalization_provenance"})
    if adopted:
        logger.info(
            "artifact adopted from provenance evidence: job=%s "
            "artifact=%s", job.id, artifact_id)
    return bool(adopted)


def _artifact_base_dir() -> Optional[Path]:
    """The artifact storage directory (resolved through the
    configured provider; never from request input)."""
    try:
        from retriva.infrastructure.storage import LocalStorageProvider
        return Path(LocalStorageProvider().base_path)
    except Exception as exc:  # noqa: BLE001 - evidence is best-effort
        logger.warning(
            "artifact base directory unavailable: exception=%s",
            exc.__class__.__name__)
        return None
