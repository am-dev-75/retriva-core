# Copyright (C) 2026 Andrea Marson (am-dev-75@gmail.com)
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
Session Document Processing — generic, CRM-agnostic chat-session
attachment and artifact support for Retriva Core.

Public surface:
- :class:`AttachmentService`  — upload, validate, scan, parse-without-ingest.
- :class:`SessionArtifactService` — session-scoped output artifacts.
- :class:`SessionStore`       — SQLite metadata store (singleton).
- :func:`sweep_expired`       — expiration sweeper.
- :class:`MalwareScanner`     — provider-neutral scanner protocol.

Importing this package registers the default no-op malware scanner.
"""

from retriva.session.models import (  # noqa: F401
    AttachmentRecord,
    AttachmentStatus,
    ParsedAttachment,
    ParsedElement,
    SessionArtifactRecord,
    ArtifactStatus,
)
from retriva.session.store import SessionStore, get_session_store  # noqa: F401
from retriva.session.attachments import AttachmentService  # noqa: F401
from retriva.session.artifacts import SessionArtifactService  # noqa: F401
from retriva.session.lifecycle import sweep_expired  # noqa: F401

# Register the default no-op malware scanner (priority 10).
import retriva.session.malware  # noqa: F401
