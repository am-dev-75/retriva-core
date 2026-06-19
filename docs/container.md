# Containerization Guide

This repository (`retriva-core`) is a Python backend service providing the Ingestion API and the OpenAI-compatible Chat API for Retriva. It is designed to be run as a long-running API container.

## Service Details
- **Type**: Long-running API service
- **Container Entrypoint**: `python -m retriva.openai_api` (default) or `python -m retriva.ingestion_api`
- **Exposed Ports**: `8001` (OpenAI API) and `8000` (Ingestion API)
- **Health Check**: `curl -f http://localhost:8001/health` or `curl -f http://localhost:8000/health` (built into the Dockerfile `HEALTHCHECK`)

## Build Command

To build the image locally:
```bash
docker build -t retriva-core:local .
```

## Run Command

To run the OpenAI API (Core) service:
```bash
docker run --rm -d -p 8001:8001 --name retriva-core --env-file .env retriva-core:local
```

To run the Ingestion API service:
```bash
docker run --rm -d -p 8000:8000 --name retriva-ingestion --env-file .env retriva-core:local python -m retriva.ingestion_api --host 0.0.0.0 --port 8000
```

## Required Environment Variables

The service relies on environment variables defined in `.env.example`. Key variables for container-to-container networking:
- `QDRANT_URL`: Should point to the Qdrant vector database container (e.g., `http://qdrant:6333`).
- `TIKA_SERVER_URL`: Should point to the Apache Tika container (e.g., `http://tika:9998`).
- `OPENAI_API_PORT`: Port to bind the OpenAI API (default: `8001`).
- `INGESTION_API_PORT`: Port to bind the Ingestion API (default: `8000`).

## Example Docker Compose Snippet

```yaml
services:
  retriva-core:
    image: retriva-core:local
    container_name: retriva-core
    ports:
      - "8001:8001"
    env_file:
      - .env
    depends_on:
      - qdrant
    restart: unless-stopped

  retriva-ingestion:
    image: retriva-core:local
    container_name: retriva-ingestion
    command: python -m retriva.ingestion_api --host 0.0.0.0 --port 8000
    ports:
      - "8000:8000"
    env_file:
      - .env
    depends_on:
      - qdrant
      - tika
    restart: unless-stopped
```

## Troubleshooting Notes

- **Invalid port in Qdrant URL**: Ensure `.env` values do not contain quotes if your environment loader does not strip them (e.g. `QDRANT_URL=http://qdrant:6333` instead of `QDRANT_URL="http://qdrant:6333"`).
- **Service unreachability**: Ensure sibling services (like `qdrant` and `tika`) are referenced by their Docker Compose service names (`http://qdrant:6333`) and not `http://localhost:6333`.
- **Healthcheck failures**: The built-in healthcheck tests both `8001` and `8000`. If you bind to a different port inside the container, you may need to override the `healthcheck` in `docker-compose.yml`.
