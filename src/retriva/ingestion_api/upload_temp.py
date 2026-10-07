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

"""Explicit ownership for request-local upload temporary files.

Spec 030 / ADR-035.  One component owns cleanup at any time; cleanup is
idempotent, missing-safe, and confined to the accepted upload temp
root.  Ownership transfers explicitly from the request handler to the
durable job / worker (via the persisted ``temp_path``) and is then
released once by the worker/local executor on terminal completion.
"""

from __future__ import annotations

import os
import tempfile
from typing import Optional

from retriva.logger import get_logger

_log = get_logger(__name__)

_OWNED = "owned"
_TRANSFERRED = "transferred"
_RELEASED = "released"

# Low-cardinality cleanup outcome labels (no paths, no ids).
_CLEANUP_OK = "upload_tempfile_cleanup_ok"
_CLEANUP_FAIL = "upload_tempfile_cleanup_failed"


def _within_root(path: str, root: str) -> bool:
    """True only when ``path`` resolves inside ``root`` and is not a
    symlink (path-traversal / symlink-escape safe)."""
    try:
        real_root = os.path.realpath(root)
        real_path = os.path.realpath(path)
    except OSError:
        return False
    if os.path.islink(path):
        return False
    try:
        return os.path.commonpath([real_path, real_root]) == real_root
    except ValueError:
        return False


class UploadTempFile:
    """Ownership guard for one request-local upload temporary file."""

    __slots__ = ("path", "root", "_state")

    def __init__(self, path: str, root: str) -> None:
        self.path = path
        self.root = root
        self._state = _OWNED

    # -- construction ----------------------------------------------------

    @classmethod
    def create(cls, data: bytes, *, suffix: str, root: str
               ) -> "UploadTempFile":
        os.makedirs(root, exist_ok=True)
        fd, path = tempfile.mkstemp(suffix=suffix, dir=root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except Exception:
            cls.cleanup(path, root=root)
            raise
        return cls(path, root)

    # -- ownership -------------------------------------------------------

    @property
    def owned(self) -> bool:
        return self._state == _OWNED

    @property
    def transferred(self) -> bool:
        return self._state == _TRANSFERRED

    def transfer(self) -> str:
        """Hand ownership to the durable job/worker; the request handler
        MUST NOT delete the file afterwards."""
        self._state = _TRANSFERRED
        return self.path

    def release(self) -> bool:
        """Delete the file if the request handler still owns it.
        Idempotent and missing-safe."""
        if self._state == _RELEASED:
            return False
        if self._state == _TRANSFERRED:
            return False
        removed = self.cleanup(self.path, root=self.root)
        self._state = _RELEASED
        return removed

    def __enter__(self) -> "UploadTempFile":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._state == _OWNED:
            self.release()
        return False

    # -- shared cleanup --------------------------------------------------

    @staticmethod
    def cleanup(path: Optional[str], *, root: str) -> bool:
        """Idempotent, missing-safe deletion confined to ``root``.
        Returns True only when a file was actually removed.  Never
        raises for a missing file, symlink, or out-of-root path."""
        if not path:
            return False
        if not _within_root(path, root):
            _log.warning("upload_tempfile_cleanup_refused_out_of_root")
            return False
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            _log.warning(_CLEANUP_FAIL)
            return False