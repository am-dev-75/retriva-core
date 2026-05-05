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
from typing import Optional, Protocol, runtime_checkable
from retriva.config import settings
from retriva.logger import get_logger

logger = get_logger(__name__)

@runtime_checkable
class ArtifactStorageProvider(Protocol):
    """Protocol for artifact storage backends."""
    def store(self, artifact_id: str, content: bytes, extension: str) -> str:
        """Store artifact content and return a URI or path."""
        ...
    
    def get_path(self, artifact_id: str) -> Optional[Path]:
        """Return the local path for the artifact if available."""
        ...

    def delete(self, artifact_id: str) -> None:
        """Delete the artifact from storage."""
        ...

class LocalStorageProvider:
    """Default OSS implementation using local filesystem."""
    
    def __init__(self, base_path: str = None):
        self.base_path = Path(base_path or settings.artifacts_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def store(self, artifact_id: str, content: bytes, extension: str) -> str:
        if not extension.startswith("."):
            extension = f".{extension}"
        path = self.base_path / f"{artifact_id}{extension}"
        with open(path, "wb") as f:
            f.write(content)
        return str(path)

    def get_path(self, artifact_id: str) -> Optional[Path]:
        # Search for any file starting with artifact_id
        found = list(self.base_path.glob(f"{artifact_id}.*"))
        return found[0] if found else None

    def delete(self, artifact_id: str) -> None:
        path = self.get_path(artifact_id)
        if path and path.exists():
            path.unlink()
            logger.info(f"Deleted artifact {artifact_id} from local storage")
