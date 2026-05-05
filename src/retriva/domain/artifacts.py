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

from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

class ArtifactStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"

class Artifact(BaseModel):
    id: str
    type: str = Field(..., description="e.g. 'document_list', 'basic_report'")
    format: str = Field(..., description="e.g. 'pdf', 'docx'")
    status: ArtifactStatus
    created_at: datetime
    updated_at: datetime
    metadata: Dict[str, str] = Field(default_factory=dict)
    error: Optional[str] = None
    download_url: Optional[str] = None

class ArtifactCapabilities(BaseModel):
    supported_formats: List[str]
    supported_types: List[str]
    templates: List[str] = Field(default_factory=list)
