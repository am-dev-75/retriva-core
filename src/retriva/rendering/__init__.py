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

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Optional, Protocol, runtime_checkable
from retriva.logger import get_logger
from retriva.registry import CapabilityRegistry

logger = get_logger(__name__)

@runtime_checkable
class Renderer(Protocol):
    """Protocol for all artifact renderers."""

    def render(
        self,
        artifact_type: str,
        parameters: Dict[str, str],
        output_path: Path,
        cancel_check: Optional[callable] = None,
    ) -> bool:
        """Render the artifact to the specified output path."""
        ...

def get_renderer(format_key: str) -> Renderer:
    """Resolve the highest-priority renderer for the given format."""
    registry = CapabilityRegistry()
    return registry.get_instance(f"renderer:{format_key}")

def register_renderer(format_key: str, renderer_class: type, priority: int = 100):
    """Register a renderer class in the global CapabilityRegistry."""
    registry = CapabilityRegistry()
    registry.register(f"renderer:{format_key}", renderer_class, priority)
