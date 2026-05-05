# SDD — Retriva Core Support for API v2 Artifacts

## Feature
Add first-class generated artifact support in Retriva Core under `/api/v2/artifacts`.

## Scope
Retriva Core only.

## Summary
Retriva Core will provide an endpoint to generate and download artifacts (documents, reports, lists) based on user requests. Rendering is owned by Retriva Core.

Supported formats:
- `markdown`
- `pdf`
- `document_list`
- `basic_report`
- `docx`
- `xlsx`
- `odt`
- `ods`
- `odp`

## API Contract

### POST `/api/v2/artifacts`
Initiates an artifact generation job.

**Request Body:**
```json
{
  "artifact_type": "document_list",
  "format": "pdf",
  "parameters": {
    "title": "My Document List",
    "include_metadata": true
  },
  "user_metadata": {
    "app": "open-webui"
  }
}
```

**Response (202 Accepted):**
```json
{
  "status": "accepted",
  "message": "Artifact generation job accepted",
  "job_id": "...",
  "artifact_id": "..."
}
```

### GET `/api/v2/artifacts/{artifact_id}`
Checks the status of an artifact or downloads it if ready.
If the job is still running, returns 202 with status.
If the job is completed, returns the binary file with appropriate `Content-Type`.

## Implementation Details

### Rendering Engine
A new `retriva.rendering` package will be introduced. It will use a registry pattern similar to parsers.
- `Renderer` protocol with a `render` method.
- Concrete implementations for different formats/types.
- Initial implementation might focus on `markdown` and `pdf` (using a lightweight library).

### Job Management
Artifact generation will be handled as a background job via `JobManager`.
`job_type` will be `v2_artifact`.

### Storage
Generated artifacts will be stored in a temporary directory (e.g., `settings.artifacts_path` or a subfolder in `settings.mirror_base_path`).
Cleanup policy: artifacts are deleted after download or after a certain TTL.

## Constraints
- API v1 remains unchanged.
- `/api/v2/documents` remains unchanged.
- No changes to Open WebUI (handled by the adapter).
