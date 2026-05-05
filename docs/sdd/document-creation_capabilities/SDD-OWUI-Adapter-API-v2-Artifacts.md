# SDD Pack — OWUI Adapter Support for Retriva API v2 Artifacts

## Feature
Add support in the Open WebUI adapter for explicit document/artifact creation requests by calling Retriva Core `/api/v2/artifacts`.

## Scope
OWUI Adapter only.

## Summary
The adapter detects explicit user requests to generate downloadable documents and routes them to Retriva Core API v2 artifacts. The adapter must not generate files directly.

Supported formats and artifact types:
- Markdown → `markdown`
- PDF → `pdf`
- Simple document list → `document_list`
- Basic report → `basic_report`
- DOCX / Word → `docx`
- Excel / spreadsheet → `xlsx`
- OpenDocument text → `odt`
- OpenDocument spreadsheet → `ods`
- OpenDocument presentation → `odp`

## Intent Detection
Classify as `artifact_request` only when:
1. The message is human-authored.
2. The message contains explicit creation/export intent.
3. The requested format or artifact type is detectable.

## Adapter Behavior
1. Classify as artifact_request.
2. Extract format and artifact type.
3. POST to `/api/v2/artifacts`.
4. Return synthetic response with artifact/job details.
5. Do not forward pure artifact requests to the normal chat LLM path.

## Configuration
```env
ENABLE_ARTIFACT_REQUESTS=true
RETRIVA_ARTIFACTS_API_BASE_URL=http://localhost:8000/api/v2
ARTIFACT_REQUEST_TIMEOUT_SECONDS=10
ARTIFACT_DEFAULT_FORMAT=pdf
```

## Acceptance Criteria
1. PDF document list requests are classified as artifact_request.
2. Adapter calls Core `/api/v2/artifacts`.
3. Adapter returns synthetic acknowledgement.
4. OWUI synthetic prompts do not trigger artifact generation.
5. Existing ingestion, directives, retrieval, and chat flows remain unchanged.
