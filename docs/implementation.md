# Notes about the implementation

- [Notes about the implementation](#notes-about-the-implementation)
  - [The ingestion process](#the-ingestion-process)
  - [The retrieval process](#the-retrieval-process)
  - [Exposed APIs](#exposed-apis)
    - [Ingestion API](#ingestion-api)
    - [OpenAI API](#openai-api)
  - [Retriva WebUI Interfacing](#retriva-webui-interfacing)
    - [Streaming support](#streaming-support)
    - [Job cancellation support](#job-cancellation-support)
    - [Knowledge Base Management](#knowledge-base-management)

## The ingestion process

The Gateway decides whether to split the ingestion process (staging files locally first vs. forwarding them immediately) based on the `source_type` property that is provided when the batch is initially created via the `POST /gateway/ingestion/batches` endpoint.

Here is how the logic works (located in `src/retriva_gateway/api/v2/ingestion.py`).

If the batch is created with `source_type="mediawiki_export"`, the Gateway splits the process:

* **Split Process (Staging)**:  During this entire phase, the Core API doesn't know about these files yet, which is why JobManager is empty and your curl command returns [].
* **Upload**: As files are uploaded to the batch, the Gateway saves them locally to its temporary directory (e.g., `/tmp/retriva-gateway-uploads/<batch_id>`). It does not contact the Core API during this phase.
* **Finalize**: Once the client finishes uploading and calls the `/finalize` endpoint, the Gateway makes a single call to the Core API (`core_client.ingest_mediawiki_export`), telling it to process the entire staged directory asynchronously at once as a single job.

If the `source_type` is anything else (like the default "auto"), the Gateway does not split the process (**Direct Pass-through (Immediate)** processing): for every single file uploaded to the batch, the Gateway immediately forwards it to the Core API via `core_client.upload_file_to_batch`, and the Core API starts an ingestion job for that specific file right away.

Only when the Core API is invoked the job(s) appear(s) when querying the Core jobs API (`curl -s http://localhost:8000/api/v2/jobs`). If you want to track the progress of the upload phase, the easiest way is to check how many files the Gateway has staged so far:
```
bash
find /tmp/retriva-gateway-uploads -type f | wc -l
(I just checked, and you currently have around 3,700 files staged).
```

## The retrieval process

The hybrid retrieval feature merges the top ranked vector search results with the top re-ranked results, allowing you to get the best of both worlds: implicit evidence from vector search (when you ask about something you haven't explicitly named) and the accuracy of re-ranking for explicit queries.

The flow looks like this:

vector search → re-ranking → hybrid selection → prompt build

where hybrid selection implies

top M reranked → append up to L vector chunks (deduped).

A two-knob model approach is used, giving precise control:


| Knob                       | Controls                                     | Example |
| -------------------------- | -------------------------------------------- | ------- |
| `HYBRID_RERANK_KEEP_TOP_M` | How many reranked chunks to keep (precision) | 6       |
| `HYBRID_VECTOR_KEEP_TOP_L` | How many vector chunks to add (recall)       | 10      |

This example means you can rerank a broader set (`RETRIEVAL_RERANK_TOP_N = 30`) but only keep the top 6 in the hybrid context, supplemented by 10 vector recall chunks. The `M` knob decouples reranker breadth from the final context composition.

Qdrant (200) → Candidates (20) → Rerank (top 6) → Hybrid (M=6 + L=10) → Budget (25) → LLM
                                       6 chunks   →   up to 16 chunks   →  ≤ 25 sources

## Exposed APIs

Retriva exposes two APIs: an ingestion API and an OpenAI-compatible API.

### Ingestion API

The ingestion API is a proprietary REST API that is used to ingest documents into Retriva. It is located at `/api/v1/ingest` and is mainly used by

* Retriva CLI
* Open WebUI adapter.
  By default it runs on port 8000.

### OpenAI API

The OpenAI-compatible API is located at `/api/v1/chat/completions`. It allows any OpenAI-compatible client to provide questions for Retriva to answer. The answer is streamed back to the client, along with metadata about the answer. For instance [Open WebUI](https://github.com/open-webui/open-webui).

Key design decision: the OpenAI-compatible API lives in a separate package (openai_api/) running by default on port 8001, keeping it cleanly decoupled from the ingestion API.

## Retriva WebUI Interfacing

Retriva WebUI serves as the primary frontend for Retriva, replacing the previous Open WebUI integration. It connects to the Retriva Gateway (port 8002), which sits in front of Retriva Core (ports 8000/8001).

### Streaming support

Key design decisions:

* Branch in existing endpoint, not a new route — `stream=true` triggers StreamingResponse
* Real LLM streaming via `client.chat.completions.create(stream=True)` — tokens arrive from the upstream model
* Grounding validation skipped in streaming mode (it needs the full answer text)
* Citations only in non-streaming — delta protocol has no slot for metadata
* New `ask_question_streaming()` is a sibling of `ask_question()`, not a modification

### Job cancellation support

Key design decisions:

* Cooperative cancellation via `cancel_check` callback injected into `upsert_chunks()` and `get_embeddings()` — checked at batch boundaries
* Thread-safe singleton `JobManager` with `threading.Lock` — `BackgroundTasks` run in a thread pool
* Backward-compatible — `IngestResponse.job_id` is optional; existing clients unaffected
* No rollback — chunks upserted before cancellation stay in Qdrant
* `CancellationError` propagates from checkpoints → caught by the background worker → sets state to cancelled

### Knowledge Base Management

Retriva maintains its knowledge base in a vector database natively. For instance, when working in tandem with Qdrant, it makes use of the `retriva_chunks` collection.

Knowledge Bases (KBs) are managed natively via the v2 API at `/api/v2/kbs`. There is no longer a need for an adapter or external ID mappings. Retriva WebUI creates and manages KBs directly using Retriva Core's endpoints.

Documents ingested through the WebUI are routed through the Gateway, which can perform staging (e.g., for MediaWiki exports) before forwarding them to Core's native ingestion pipeline. Each document gets a unique `doc_id` and is tied directly to its target `kb_id`.