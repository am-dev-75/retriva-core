![](docs/assets/Retriva_logo_slogan_white_background.jpg)

# Retriva

- [Retriva](#retriva)
  - [Introduction](#introduction)
    - [Key Features](#key-features)
    - [License notes](#license-notes)
  - [Architecture](#architecture)
    - [Overview](#overview)
    - [Open WebUI (OWUI)](#open-webui-owui)
      - [OWUI adapter](#owui-adapter)
    - [Retriva core](#retriva-core)
    - [End-to-End Flow Summary](#end-to-end-flow-summary)
      - [Upload-only flow](#upload-only-flow)
      - [Question flow](#question-flow)
    - [Features and design principles](#features-and-design-principles)
      - [Basic](#basic)
      - [Advanced features](#advanced-features)
      - [Data Sovereignty](#data-sovereignty)
  - [Implementation](#implementation)
    - [Software architecture](#software-architecture)
    - [API](#api)
  - [Quick Start](#quick-start)
    - [Use in tandem with Open WebUI (optional)](#use-in-tandem-with-open-webui-optional)
  - [Licensing](#licensing)

## Introduction

Retriva is a conversational AI agent. It is built to provide users with accurate and relevant information by leveraging the power of Retrieval Augmented Generation (RAG). It is built to provide users with accurate and relevant information by leveraging the power of Retrieval Augmented Generation (RAG). It is designed for enterprise use cases where data privacy and security are of utmost importance.

For more details abouth the birth of the project, please see also [Retriva Documentation](https://github.com/am-dev-75/retriva-docs).

### Key Features

* OpenAI-compatible integration throughout
* Asynchronous, resilient ingestion
* Strict separation of control plane and data plane
* Identity-preserving document handling
  * Every document keeps the identity it was given at upload time, even if its content is identical to another document. In other words, Retriva does not collapse, merge, or deduplicate documents automatically based on file content.
* Debug-only internal observability endpoints
* Seamless integration with [Open WebUI](https://github.com/open-webui/open-webui)
  * To enable this, use
    * This container: [Open WebUI for Retriva](https://github.com/am-dev-75/open-webui_retriva)
    * [This additional service](https://github.com/am-dev-75/open-webui_retriva-adapter), acting as a bridge between Retriva backend and Open WebUI frontend
* When combined with [Open WebUI](https://github.com/am-dev-75/open-webui_retriva)
  * Chat-based ingestion directives
  * Deterministic intent classification
    * Given the same request, the adapter will always make the same routing decision — regardless of timing, retries, or OWUI’s internal orchestration.

### License notes

Why did I choose the Apache License 2.0? Because this license, combined with certain specific design choices, allows for the creation of Retriva extensions without being required to release them as source code. No one knows if or how the project will evolve. If anyone were ever to use it as a starting point for developing a real product, I believe that the ability to extend it permissively while still remaining connected to the main repository for core functionality is a significant advantage.

## Architecture

### Overview
Retriva is a retrieval‑augmented generation (RAG) platform designed to integrate seamlessly with Open WebUI (OWUI) while preserving a clean separation of concerns between user interaction, ingestion orchestration, and LLM execution.

The final logical architecture should look like ![this:](docs/assets/Retriva_target_logic_architecture.drawio.png)

At a high level, the architecture consists of four main components:

- **Open WebUI (OWUI)** – the user-facing interface
- **Thin Adapter** – the control plane and policy enforcement layer
- **Retriva Core** – ingestion, retrieval, and document management
- **LLM Providers** – external or internal model backends

### Open WebUI (OWUI)

Open WebUI is responsible for:

- User authentication and chat sessions
- File uploads
- Knowledge Base (KB) management
- UI-level orchestration (search planning, follow-up suggestions, streaming reconciliation)

OWUI always communicates using OpenAI-compatible APIs, even for non-user actions such as uploads or internal planning. As a result, OWUI may emit multiple chat-completion requests for a single user action. These requests are control-plane artifacts, not direct expressions of user intent.
OWUI remains intentionally unaware of Retriva internals.

#### OWUI adapter

To interface Open WebUI with Retriva, an [adapter](https://github.com/am-dev-75/open-webui_retriva-adapter) is needed. .

The adapter sits between OWUI and Retriva and is the architectural keystone of the system.
Its responsibilities include:
* Intent classification
  * Distinguishes human-authored questions from OWUI-generated control prompts
  * Ensures uploads and directives do not trigger unintended LLM calls
* Directive handling
  * Implements chat-based ingestion directives (e.g. `@@ingestion_tag_start`, `@@ingestion_tag_stop`)
  * Maintains per-chat, ephemeral ingestion context
* Ingestion orchestration
  * Detects uploads indirectly via OWUI’s Files API (out-of-band)
  * Performs asynchronous ingestion through polling
  * Applies user-provided metadata at ingestion time
* Policy enforcement
  * Ensures upload-only turns never reach the LLM
  * Guarantees deterministic behavior regardless of OWUI’s internal orchestration loops
* Observability
  * Maintains mapping stores:
    * OWUI Knowledge Bases ↔ Retriva kb_ids
    * OWUI file IDs ↔ Retriva doc_ids
  * Exposes gated, internal debug endpoints (`/internal/...`) for inspection

Crucially, the adapter:

- Never calls the LLM directly
- Never forwards user credentials
- Never interprets OWUI control prompts as user intent

It is a pure control plane, not a model gateway.

### Retriva core
Retriva is the data plane and system of record for:
* Document ingestion
* Chunking and embedding
* Metadata storage (including user-provided tags)
* Knowledge Base assignment
* Retrieval and ranking
* LLM request construction

Retriva:
* Receives ingestion jobs from the adapter
* Stores documents using its own identifiers (doc_id)
* Applies metadata exactly as provided at ingestion time
* Executes retrieval and calls the LLM only when explicitly requested by the adapter

Retriva treats every upload as a distinct document, even if file content is identical. This preserves user intent, document lifecycle independence, and metadata correctness.

### End-to-End Flow Summary
#### Upload-only flow
```
User → OWUI (upload)
OWUI → Adapter (chat + control prompts)
Adapter → Synthetic acknowledgement
Adapter → OWUI Files API (polling)
Adapter → Retriva ingestion API
```
#### Question flow
```
User → OWUI (question)
OWUI → Adapter
Adapter → Retriva
Retriva → LLM
```
At no point do uploads implicitly cause LLM calls.

### Features and design principles
#### Basic

See [this page](docs/basic_features.md).

#### Advanced features

See [this page](docs/advanced_features.md).

#### Data Sovereignty

From the very beginning, Retriva was designed with data sovereignty in mind—that is, ensuring that parties other than the owner of the data entered into the knowledge base could not access it. Currently, there are several solutions to address this requirement, each with its own pros and cons. This [section](docs/data_sovereignty.md) provides an overview of these options. Given Retriva’s modular nature, it can be deployed in various ways, including hybrid configurations that combine the options listed in the linked page.

## Implementation

### Software architecture

![](docs/assets/Retriva_software_architecture.drawio.png)

See [this page](docs/implementation.md) for the implementation details.

### API

See [this page](./docs/api.md) for the API documentation.

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
    ```(retriva-venv) $ pip install -r requirements.txt```
  * Copy `.env` from `.env.example` and fill in the values so that Retriva can connect to the Qdrant instance and the LLM's runner(s) you intend to use.
* Start the ingestion API server:

```bash
(retriva-venv) $ PYTHONPATH=src python -m retriva.ingestion_api
```

* Build the knowledge base from **your** documents with the CLI. For instance:

```bash
(retriva-venv) $ PYTHONPATH=src python -m retriva.cli ingest --path ~/my_documents
```

For more details, run `PYTHONPATH=src python -m retriva.cli -h`.

* Start the chat application:

```bash
(retriva-venv) $ streamlit run src/retriva/ui/streamlit_app.py
```

### Use in tandem with Open WebUI (optional)

* Start the Retriva backend providing OpenAI API:

```bash
(retriva-venv) $ PYTHONPATH=src python -m retriva.openai_api
```

* Clone the https://github.com/am-dev-75/open-webui_retriva repository.
* Modify the docker-compose.yml file
  * to mount the Open WebUI data directory to a persistent volume
  * to adjust the URL of Open WebUI adapter for Retriva.
* Start a containerized instance of [Open WebUI for Retriva](https://github.com/am-dev-75/open-webui_retriva).
* In Open WebUI (OWUI)
  * create admin user
  * in User'settings->Account create the API key.
  * copy this API key in the [Open WebUI/Retriva adapter](https://github.com/am-dev-75/open-webui_retriva-adapter)'s `.env` file so that the adapter che authenticate with OWUI (this operation must be done on first OWUI start only).
* Stop Open WebUI container.
* Start [Open WebUI/Retriva adapter](https://github.com/am-dev-75/open-webui_retriva-adapter).
* Start Open WebUI container.
* Log in to OWUI and create a [function](https://docs.openwebui.com/features/extensibility/plugin/) by copying [this code](https://github.com/am-dev-75/open-webui_retriva-adapter/blob/main/adapter/scripts/retriva_push_based_synchronization.py). Change the Open WebUI's Adapter URL according to your deployment. This function must either enabled for the model "Retriva" or globally.
* In OWUI, point your browser to the Open WebUI for Retriva instance and start having fun.

## Licensing

This project, including all source code, agentic specifications, and documentation, is licensed under the Apache License 2.0. See the LICENSE file for details.
