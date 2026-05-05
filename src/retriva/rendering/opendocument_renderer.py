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
from typing import Dict, Optional
from retriva.rendering import register_renderer
from retriva.logger import get_logger

logger = get_logger(__name__)

class OpenDocumentSkeletonRenderer:
    """Skeleton renderer for OpenDocument formats (odt, ods, odp)."""

    def render(
        self,
        artifact_type: str,
        parameters: Dict[str, str],
        output_path: Path,
        cancel_check: Optional[callable] = None,
    ) -> bool:
        logger.info(f"Rendering skeleton for OpenDocument: {artifact_type}")
        
        format_ext = output_path.suffix.lower()
        title = parameters.get("title", f"Retriva {format_ext[1:].upper()} Artifact")
        
        content = (
            f"Artifact Title: {title}\n"
            f"Format: {format_ext[1:].upper()}\n\n"
            "This is a skeleton placeholder for OpenDocument formats.\n"
            "Full implementation is planned for the next release."
        )
        
        if cancel_check and cancel_check():
            return False
            
        with open(output_path, "w") as f:
            f.write(content)
            
        return True

# Register the renderer for multiple formats
register_renderer("odt", OpenDocumentSkeletonRenderer)
register_renderer("ods", OpenDocumentSkeletonRenderer)
register_renderer("odp", OpenDocumentSkeletonRenderer)
