![](docs/assets/Retriva_logo_slogan_white_background.jpg)

# Retriva

- [Retriva](#retriva)
  - [Introduction](#introduction)
    - [Features and design principles](#features-and-design-principles)
      - [Key features](#key-features)
        - [Details about basic features](#details-about-basic-features)
        - [Details about advanced features](#details-about-advanced-features)
      - [Design principles](#design-principles)
        - [Optimized for engineering/scientific knowledge bases](#optimized-for-engineeringscientific-knowledge-bases)
        - [Knowledge base deep grounding](#knowledge-base-deep-grounding)
        - [Multi-modal support](#multi-modal-support)
        - [Modular design](#modular-design)
        - [Models agnosticism](#models-agnosticism)
        - [Frontend agnosticism](#frontend-agnosticism)
        - [Data Sovereignty](#data-sovereignty)
    - [License notes](#license-notes)
  - [Architecture](#architecture)
    - [Introduction](#introduction-1)
    - [Overview](#overview)
    - [Retriva WebUI](#retriva-webui)
      - [Document Ingestion](#document-ingestion)
      - [Interacting with ingested documents](#interacting-with-ingested-documents)
        - [Document Discovery](#document-discovery)
        - [Chat and Q\&A (RAG)](#chat-and-qa-rag)
    - [Retriva Gateway](#retriva-gateway)
    - [Retriva Core](#retriva-core)
    - [End-to-End Flow Summary](#end-to-end-flow-summary)
      - [Upload-only flow](#upload-only-flow)
      - [Question flow](#question-flow)
    - [Implementation](#implementation)
    - [API](#api)
  - [Quick Start](#quick-start)
    - [Use in tandem with Open WebUI (optional)](#use-in-tandem-with-open-webui-optional)
  - [Future development](#future-development)
  - [Licensing](#licensing)

## Introduction

Retriva is a conversational AI agent. It is built to provide users with accurate and relevant information by leveraging the power of Retrieval Augmented Generation (RAG).  It is designed for enterprise use cases where data privacy and security are of utmost importance.

As described in more detail in the [Architecture section](#architecture), Retriva consists of three main components, which have separate repositories:

- [Retriva WebUI](https://github.com/am-dev-75/retriva-webui)
- [Retriva Gateway](https://github.com/am-dev-75/retriva-gateway)
- [Retriva Core](https://github.com/am-dev-75/retriva-core)

For more details abouth the birth of the project, please see also [Retriva Documentation](https://github.com/am-dev-75/retriva-docs).

There is a [YouTube playlist](https://www.youtube.com/playlist?list=PLlXdajtWCbM6ChFh6P8dML1I9uAQhjOZ9)  illustrating how to work with Retriva from a user's perspective.

### License notes

Why did I choose the Apache License 2.0? Because this license, combined with certain specific design choices, allows for the creation of Retriva extensions without being required to release them as source code. No one knows if or how the project will evolve. If anyone were ever to use it as a starting point for developing a real product, I believe that the ability to extend it permissively while still remaining connected to the main repository for core functionality is a significant advantage.

## Features

* OpenAI-compatible chat API
* Asynchronous, resilient ingestion (proprietary streaming API)
* Strict separation of control plane and data plane
* Nearly-deterministic behavior
  * Given the same user request and identical Knowledge Bases, the system will always produce the same output
* Identity-preserving document handling
  * Every document keeps the identity it was given at upload time, even if its content is identical to another document. In other words, Retriva does not collapse, merge, or deduplicate documents automatically based on file content.
* Document creation API supporting the most common document formats such as PDF, DOCX, XLSX, ODT, ODS, ODP, and Markdown.
* LangChain-free
* Debug-only internal observability endpoints
* User-provided tags for advanced document filtering/querying

### Internal profiler

The Retriva's Core internal profiler is a debugging tool that allows you to see how long each step of the RAG pipeline takes.

To enable it, set the `ENABLE_INTERNAL_PROFILER` environment variable to `true`.

The profiler logs each step to the console.

Example output:

```
...
[20260428 16:05:06] [DEBUG] [Profiler][f880bb28-79b4-4bef-9797-ee5a46b5ec19] Phase 'request_received' reached at 0.00ms
[20260428 16:05:06] [DEBUG] [Profiler][f880bb28-79b4-4bef-9797-ee5a46b5ec19] Phase 'request_validated' reached at 0.11ms
[20260428 16:05:07] [DEBUG] [Profiler][f880bb28-79b4-4bef-9797-ee5a46b5ec19] Phase 'retrieval_vector_search_complete' reached at 825.38ms
[20260428 16:05:07] [DEBUG] [Profiler][f880bb28-79b4-4bef-9797-ee5a46b5ec19] Phase 'retrieval_ranking_complete' reached at 825.45ms
[20260428 16:05:07] [DEBUG] [Profiler][f880bb28-79b4-4bef-9797-ee5a46b5ec19] Phase 'prompt_construction_complete' reached at 825.70ms
[20260428 16:05:07] [DEBUG] [Profiler][f880bb28-79b4-4bef-9797-ee5a46b5ec19] Phase 'inference_request_sent' reached at 844.89ms
[20260428 16:05:17] [DEBUG] [Profiler][f880bb28-79b4-4bef-9797-ee5a46b5ec19] Phase 'inference_first_token_received' reached at 11147.15ms
[20260428 16:05:26] [DEBUG] [Profiler][f880bb28-79b4-4bef-9797-ee5a46b5ec19] Phase 'inference_complete' reached at 19817.17ms
[20260428 16:05:26] [DEBUG] [Profiler][f880bb28-79b4-4bef-9797-ee5a46b5ec19] Phase 'post_processing_complete' reached at 19817.99ms
[20260428 16:05:26] [DEBUG] [Profiler][f880bb28-79b4-4bef-9797-ee5a46b5ec19] Phase 'response_sent' reached at 19818.75ms
...
```
Times are expressed in milliseconds and are relative to the start of the request (`Phase 'request_received'`). When a request/response session completes, the profiler emits a structured log message with all the collected data, which can be read from the endpoint `/internal/profiler/log`. The log message is in JSON format. Example:

```
[20260428 16:05:26] [INFO] PROFILER_LOG: {
  "request_id": "f880bb28-79b4-4bef-9797-ee5a46b5ec19",
  "timestamp": "2026-04-28T14:05:26.252540+00:00",
  "model": "qwen/qwen3.5-27b",
  "provider": "https://openrouter.ai/api/v1",
  "is_streaming": true,
  "phases": {
    "request_received": 0,
    "request_validated": 0.11,
    "retrieval_vector_search_complete": 825.38,
    "retrieval_ranking_complete": 825.45,
    "prompt_construction_complete": 825.7,
    "inference_request_sent": 844.89,
    "inference_first_token_received": 11147.15,
    "inference_complete": 19817.17,
    "post_processing_complete": 19817.99,
    "response_sent": 19818.75
  },
  "total_duration_ms": 19818.81
}
```
### Extension development

As mentioned in the [Licensing notes](#licensing-notes), Retriva Core support extensions that can be added to the codebase. These extensions allow you to customize and enhance Retriva's functionality without modifying the core code.

## Design principles

### Optimized for engineering/scientific knowledge bases

Retriva is optimized for engineering/scientific knowledge bases. This means that it is designed to handle the specific needs of engineering and scientific users, such as those who work with technical documentation, research papers, and other specialized content. Thanks to the use of specialized models, embeddings, and chunking strategies, the system is able to provide accurate and relevant information to users, even though this is "hidden" in complex, articulated, and technical documents feeding Retriva during ingestion.

### Knowledge base deep grounding

Retriva is built on a "grounded-only" principle. It generates responses based strictly on the information available in the provided Knowledge Base (KB). If the system cannot find sufficient information within the KB to answer a query, it will state so explicitly rather than attempting to synthesize or "hallucinate" a response. This ensures high reliability and trustworthiness for information-critical applications.

### Multi-modal support

Retriva is designed to support multi-modal inputs, including text, images, and other data types. This allows users to interact with Retriva in a more natural and intuitive way, as well as to leverage the full potential of multi-modal models.

### Modular design

Retriva is designed to be modular, allowing users to customize the system to their specific needs. This is achieved through the use of a plugin architecture, which allows users to add or remove features as needed. This modular design also makes it easier to maintain and upgrade the system, as each component can be developed and tested independently.

### Models agnosticism

Retriva is designed to be model-agnostic, allowing users to choose the models that best suit their needs. This is achieved simply by changing environment variables specifying the desired models.

### Frontend agnosticism

Although Retriva comes with a web frontend called [Retriva WebUI](https://github.com/am-dev-75/retriva-webui), it is designed to be frontend-agnostic. This allows users to implement a different frontend that best suits their needs if necessary. 

##### Data Sovereignty

From the very beginning, Retriva was designed with data sovereignty in mind—that is, ensuring that parties other than the owner of the data entered into the knowledge base could not access it. Currently, there are several solutions to address this requirement, each with its own pros and cons. This [section](docs/data_sovereignty.md) provides an overview of these options. Given Retriva’s modular nature, it can be deployed in various ways, including hybrid configurations that combine the options listed in the linked page.


## Architecture

### Introduction
Basically, to use a RAG system you need to
* Firstly, **ingest** information into it.Ingestion is the process of extracting information from documents and storing it in a way that can be searched and retrieved. This documents — which are usually files — are sually, are ingested in separate Knowledge Bases (KB) that users can name and manage. Therefore, you can think of KBs as specific-purpose databases in which you can store whatever information you want.
* When you want to interact with your KBs, you query them in natural language for retrieving information, elaborating the data they containm and so on. In this regard, think about Retriva as a tailored version of ChatGPT knowing **your** KBs.

### Overview

Retriva is designed to be preserve a clean separation of concerns between user interaction, ingestion orchestration, and LLM execution. The following diagram illustrates its architecture:

![](docs/assets/Retriva_software_architecture.drawio.png)

At a high level, the architecture consists of four main components:

- **Retriva WebUI** – the user-facing interface
- **Retriva Gateway** – sits between Retriva WebUI and Retriva Core, providing a thin adapter layer between the frontend and the backend, which abstracts the backend API.
- **Retriva Core** – ingestion, retrieval, and document management
- **LLM Providers** – external LLM servers, such as OpenRouter, Ollama, LM Studio, or others. LLMs are used for embeddings and RAG chat completions.

### Retriva WebUI

Retriva WebUI is the user-facing web interface that allows users to interact with their documents and knowledge bases through conversational AI (RAG).

#### Document Ingestion
The **Ingestion page** is used to add new files and documents to the system. Users can:
* **Select Target Knowledge Base**: Specify which Knowledge Base the uploaded documents should belong to. If no Knowledge Base is selected, the documents will be uploaded to the `default` Knowledge Base.
* **Upload Files & Folders**: Upload individual files or entire directory structures of supported formats (PDF, DOCX, XLSX, Markdown, etc.).
* **Attach Ingestion Metadata (optional)**: Define key-value pairs (e.g., `project: Apollo`) to tag the uploaded files. This metadata is applied at document level during ingestion and automatically propagated to all generated text chunks. These optional, user-defined metadata can be later used to filter documents during catalog exploration and RAG retrieval.
* **Monitor Ingestion Jobs**: Track the real-time status (queued, processing, ready, or failed) of current and historical ingestion batches.

#### Interacting with ingested documents
Users can interact with ingested documents through two primary interfaces: **Document Discovery** and **Chat** (RAG).

##### Document Discovery
The **Documents** page allows users to search, filter, list, and manage all ingested documents across different Knowledge Bases. Users can:
* Perform search queries on filenames, source paths, and metadata tags (as a matter of fact, filenames and source paths are also stored as metadata tags).
* Filter documents by specific metadata properties.
* Delete documents and their associated index chunks.

##### Chat and Q&A (RAG)
The **Chat** page enables natural language conversations with the retrieval-augmented generation pipeline. When submitting a query, users can configure:
* **Knowledge Base Selection**: Restrict retrieval to specific knowledge bases (e.g., `default` or project-specific KBs).
* **Metadata Filters**: Define custom key-value constraints (e.g., `user_metadata.project = apollo`) to target specific subsets of files.
* **Metadata Filtering Mode**: Choose how strictly the metadata filters should be applied during vector retrieval:
  * **Hard Mode (`hard`)**: Filters are treated as mandatory exclusion constraints. Only chunks that strictly match all specified metadata filters are considered for retrieval and reranking. Any chunk that does not match is completely excluded.
  * **Soft Mode (`soft`)**: Filters act as ranking and recall boost signals. Chunks matching the metadata filters are prioritized and boosted in the final scores, but semantically relevant documents that do not match the filter can still be retrieved to ensure comprehensive context.

### Retriva Gateway

Retriva Gateway serves as the control plane and Backend-for-Frontend (BFF) layer, sitting between Retriva WebUI and Retriva Core. It handles orchestration, WebUI integration, and policy enforcement.

Its key capabilities include:
* **BFF (Backend-for-Frontend) API**: Exposes tailored endpoints for the WebUI (e.g. `/gateway/kbs`, `/gateway/documents`, `/gateway/chat`, and `/gateway/artifacts`), abstracting the complexity of the underlying Core services.
* **Ingestion Orchestration**: Coordinates multi-step, asynchronous document ingestion batches, handles file uploads, tracks job progress, and applies user-provided metadata at ingestion time.
* **Intent Classification & Guardrails**: Inspects incoming chat requests to distinguish actual user messages from WebUI control loops or upload notifications. It ensures that system tasks do not trigger unnecessary LLM completions.
* **Identifier Mapping & Sync**: Maintains persistent mappings between WebUI-specific identifiers (such as front-end file IDs or knowledge base IDs) and Core-internal identifiers (such as `doc_id` and Qdrant collection tags).
* **Retrieval & Citation Mapping**: Translates chunk-level vector search results and citations returned by Retriva Core back into UI-friendly references containing the original source filenames and logic paths.

### Retriva Core

Retriva Core is the data plane and system of record for:

* Document ingestion
* Chunking and embedding
* Metadata storage (including user-provided tags)
* Knowledge Base assignment
* Retrieval and ranking
* LLM request construction

Retriva Core:

* Receives ingestion jobs from Retriva Gateway
* Stores documents using its own identifiers (doc_id)
* Applies metadata exactly as provided at ingestion time
* Executes retrieval and calls the LLM when requested by Retriva Gateway

Retriva Core treats every upload as a distinct document, even if file content is identical. This preserves user intent, document lifecycle independence, and metadata correctness.

### End-to-End Flow Summary

#### Upload-only flow

```
User → WebUI (upload file)
WebUI → Gateway (create batch & upload file)
Gateway → Core Ingestion API (upload & start job)
```

#### Question flow

```
User → WebUI (ask question)
WebUI → Gateway (send chat request)
Gateway → Core OpenAI Chat API (chat completions with RAG)
Core → LLM Provider
```

At no point do uploads implicitly cause LLM calls.

### Implementation

See [this page](docs/implementation.md) for the implementation details.

### API

See [this page](./docs/openapi.yaml) for the documentation of Retriva Core's API.
Gateway's API documentation is [here](https://github.com/am-dev-75/retriva-gateway/blob/main/docs/openapi.yaml).

## Quick Start

* If not already available, [deploy a Qdrant instance](https://qdrant.tech/documentation/quickstart/).
  * This is the typical log when you start the containerized version:

```
           _                 _  
  __ _  __| |_ __ __ _ _ __ | |_  
 / _` |/ _` | '__/ _` | '_ \| __| 
| (_| | (_| | | | (_| | | | | |_  
 \__, |\__,_|_|  \__,_|_| |_|\__| 
    |_|               

Version: 1.17.1, build: eabee371
Access web UI at http://localhost:6333/dashboard

2026-04-03T21:17:56.070752Z  INFO storage::content_manager::consensus::persistent: Loading raft state from ./storage/raft_state.json
2026-04-03T21:17:56.081084Z  INFO storage::content_manager::toc: Loading collection: retriva_chunks
2026-04-03T21:17:56.101527Z  INFO collection::shards::local_shard: Recovering shard ./storage/collections/retriva_chunks/0: 0/1 (0%)
2026-04-03T21:17:56.103581Z  INFO collection::shards::local_shard: Recovered collection retriva_chunks: 1/1 (100%)
2026-04-03T21:17:56.108149Z  INFO qdrant: Distributed mode disabled
2026-04-03T21:17:56.108463Z  INFO qdrant: Telemetry reporting enabled, id: c012b687-e64f-419e-abf8-8ada1e63a2b8
2026-04-03T21:17:56.147714Z  INFO qdrant::tonic: Qdrant gRPC listening on 6334
2026-04-03T21:17:56.147723Z  INFO qdrant::tonic: TLS disabled for gRPC API
2026-04-03T21:17:56.148679Z  INFO qdrant::actix: REST transport settings: keep_alive=5s, client_request_timeout=5s, client_disconnect_timeout=5s
2026-04-03T21:17:56.148694Z  INFO qdrant::actix: TLS disabled for REST API
2026-04-03T21:17:56.148977Z  INFO qdrant::actix: Qdrant HTTP listening on 6333
2026-04-03T21:17:56.149118Z  INFO actix_server::builder: starting 31 workers
2026-04-03T21:17:56.149514Z  INFO actix_server::server: Actix runtime found; starting in Actix runtime
2026-04-03T21:17:56.149519Z  INFO actix_server::server: starting service: "actix-web-service-0.0.0.0:6333", workers: 31, listening on: 0.0.0.0:6333
```

* Install required packages
  * `$ sudo apt-get update && sudo apt-get install -y tesseract-ocr tesseract-ocr-eng tesseract-ocr-ita ghostscript`
* After cloning this repository
  * install dependencies (use of a virtual environment is recommended):
    ```(retriva-core) $ pip install -r requirements.txt```
  * Copy `.env` from `.env.example` and fill in the values so that Retriva can connect to the Qdrant instance and the LLM's runner(s) you intend to use.
* Clone the [Retriva Gateway repository](https://github.com/am-dev-75/retriva-gateway)
  * Then, install dependencies (use of a virtual environment is recommended):
```(retriva-gateway) $ pip install -r requirements.txt```
  * Copy `.env` from `.env.example` and fill in the values so that Retriva Gateway can connect to Retriva Core instance.
* Clone the [Retriva WebUI repository](https://github.com/am-dev-75/retriva-webui)
* Start Retriva components
  * To start Retriva components manually, you can use [this script](./scripts/housekeeping/restart-retriva.py) as reference.
* Point your browser to http://localhost:5173/ and have fun!

## Possible future developments

* Containerized version to facilitate deployment and simplify dependency management.
* Test deployment on a confidential computing-enabled cloud, using TensorRT-LLM as the LLM runner.
* Enable Qdrant hybrid search (semantic + keyword).
* Refine retrieval pipeline by adding GraphRAG capabilities.
* Interfacing to external IAM systems for user authentication and authorization.

## Licensing

This project, including all source code, agentic specifications, and documentation, is licensed under the Apache License 2.0. See the LICENSE file for details.
