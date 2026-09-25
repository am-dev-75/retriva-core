# Reranking subsystem

Retriva's retrieval pipeline is two-stage:

```text
Retriever (Qdrant vector search, broad recall)
    |
    v
Candidate chunks  (chunk IDs, source IDs, metadata, citations, retrieval scores)
    |
    v
Provider-neutral Reranker interface        (retriva.protocols.Reranker)
    |
    v
Globally configured provider adapter       (retriva.qa.reranking factory)
    |
    v
Provider-neutral ranked results            (same chunk dicts, reordered)
    |
    v
Diversity filter -> Hybrid selection -> Context selection -> Answer generation
```

The reranking provider and model are selected through Retriva's **single
global settings system** (environment variables loaded by
`retriva.config.settings`). The selected reranker applies to **every
knowledge base, retrieval operation, user, and customer** — there is
deliberately **no knowledge-base-specific reranker configuration** (the
per-KB `settings` JSON object stored in the KB registry is opaque to the
retrieval pipeline and is not consulted by the reranker).

## Providers

| Provider name (env value) | Transport | Typical use |
| --- | --- | --- |
| `""` or `openrouter` (default) | Cohere-compatible `POST /rerank` via `httpx` against `RETRIEVAL_RERANK_BASE_URL` | OpenRouter, Cohere, Jina, self-hosted rerankers. **Default — preserves legacy behavior for existing deployments.** |
| `cohere` | Alias of `openrouter` | Same transport, clearer name for Cohere endpoints. |
| `bedrock` | Amazon Bedrock Rerank API via `boto3` (`bedrock-runtime` `rerank`) | AWS-native deployments. |

Custom transports can be added with
`retriva.qa.reranking.factory.register_rerank_provider(name, factory)`
and selected globally like any builtin.

## Configuration

All configuration lives in the global `Settings` model
(`src/retriva/config.py`) and is provided via environment variables (or
the `.env` file pydantic-settings loads).

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `ENABLE_RETRIEVAL_RERANKING` | `true` | Master switch for the reranking stage. |
| `RETRIEVAL_RERANK_PROVIDER` | `""` (→ `openrouter`) | Provider selection: `openrouter`, `cohere`, `bedrock`. |
| `RETRIEVAL_RERANK_MODEL` | `cohere/rerank-v3.5` | Model id. For `bedrock`: a bare rerank model id (e.g. `amazon.rerank-v1:0`) or a full model ARN. |
| `RETRIEVAL_RERANK_BASE_URL` | `https://openrouter.ai/api/v1` | Cohere-compatible endpoint base URL (`openrouter` provider only). |
| `RETRIEVAL_RERANK_API_KEY` | falls back to `OPENROUTER_OPENAI_API_KEY` | API key (`openrouter` provider only). **Ignored by `bedrock`.** |
| `RETRIEVAL_RERANK_AWS_REGION` | `AWS_REGION` → `AWS_DEFAULT_REGION` | AWS region (`bedrock` provider only). |
| `RETRIEVAL_RERANK_CANDIDATES` | `100` | Cap on how many stage-1 candidates are sent to reranking (`0` = all). |
| `RETRIEVAL_RERANK_TOP_N` | `30` | How many chunks reranking returns. |
| `RETRIEVAL_RERANK_BATCH_SIZE` | `100` | Documents per `/rerank` call (`openrouter` only). |
| `RETRIEVAL_RERANK_MAX_LENGTH` | `4096` | Per-document truncation in characters before submission. |
| `RETRIEVAL_RERANK_TIMEOUT` | `30.0` | Request timeout, seconds (all providers). |
| `RETRIEVAL_RERANK_MAX_RETRIES` | `2` | Total attempts on transient errors (all providers). |
| `RETRIEVAL_RERANK_RETRY_BASE_DELAY` | `1.0` | Exponential backoff base delay, seconds. |

### Precedence

1. Explicit `RETRIEVAL_RERANK_PROVIDER` (case-insensitive, `""` means
   default) → resolves via the provider registry; unknown names fail fast
   with the list of registered providers.
2. Empty selection → `openrouter` (legacy Cohere-compatible transport),
   so existing deployments behave exactly as before the subsystem existed.
3. `RETRIEVAL_RERANK_API_KEY` falls back to `OPENROUTER_OPENAI_API_KEY`
   via `model_post_init` — but only when the selected provider is not
   `bedrock` (Bedrock uses the AWS credential chain instead).
4. Bedrock region: `RETRIEVAL_RERANK_AWS_REGION` → `AWS_REGION` →
   `AWS_DEFAULT_REGION`.

### Secret handling

* API keys are ordinary settings values and are **never logged** nor
  returned by any endpoint. The status endpoint reports only
  `api_key_set: true|false`.
* For `bedrock`, credentials are **not** part of Retriva settings. The
  standard AWS credential chain applies: `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` environment variables,
  shared credentials/config files, or — recommended — the IAM role of
  the workload. Minimal IAM policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["bedrock:Rerank"],
      "Resource": "arn:aws:bedrock:REGION::foundation-model/amazon.rerank-v1:0"
    }
  ]
}
```

## Architecture

### Domain interface

`retriva.protocols.Reranker` defines the pipeline-level contract:

```python
def rerank(self, query: str, chunks: List[Dict], top_n: int) -> List[Dict]: ...
```

Implementations must return the **same chunk dicts** (not copies),
reordered by relevance, so chunk IDs, source IDs, metadata, citations and
retrieval scores are preserved. Only the internal `_score` key is
overwritten with the provider's relevance score so downstream sorting,
the diversity filter and hybrid selection use the reranked ordering.

### Provider SPI

`retriva.qa.reranking.base.RerankProvider` is the transport-level
interface:

```python
def rank(self, query: str, documents: List[str], top_n: int) -> List[Dict]:
    # returns [{"index": int, "relevance_score": float}, ...]
```

Providers raise `RerankProviderError` on failure instead of returning
partial results; fallback policy is owned by the caller.

### Factory and runtime reload

`retriva.qa.reranking.factory` resolves the provider:

* `get_reranker_provider()` snapshots the effective configuration into a
  fingerprint and rebuilds the provider only when the fingerprint
  changes — a cheap per-retrieval check that makes the subsystem pick up
  settings mutated at runtime (programmatic mutation by extensions or
  tests). Changes to environment variables alone still require a process
  restart, because pydantic-settings reads them once at startup.
* `validate_rerank_config()` performs static validation (provider known,
  required fields present) **without network calls**. It runs during
  startup of both Retriva APIs and logs issues (log-and-continue, the
  same convention as Qdrant initialization); it never blocks startup.

### Capabilities and Retriva Pro override paths

The pipeline resolves the `reranker` capability through the
`CapabilityRegistry` (`DefaultReranker`, priority 100). Two override
paths remain:

1. Replace the whole capability with a custom implementation registered
   at priority > 100 (legacy Pro path, unchanged).
2. Add a transport to the provider factory and select it globally.

## Error and fallback policy

Unchanged from the legacy reranker: if the provider fails (timeout,
connection error, 4xx/5xx, malformed output, empty results, unknown
provider name), `DefaultReranker` logs a warning and returns the
candidate chunks truncated to `top_n` in their original vector-similarity
order — retrieval never fails because reranking is unavailable. Every
failure is recorded in health + metrics (below).

### Output hardening (defensive contract)

Provider output is treated as untrusted external input. `DefaultReranker`
validates every result entry (`_coerce_result`) and:

* skips entries with a non-integer `index` (bools rejected), an
  unconvertible `relevance_score`, or a non-object payload;
* skips out-of-bounds and duplicate indices;
* clamps the mapped selection to `top_n` even when a provider returns
  more results;
* treats a "successful" call with **no usable results** as a fallback
  (health → `degraded`, `fallback_total` incremented) instead of silently
  returning an empty context;
* never lets a malformed entry raise out of `rerank()` — the fallback
  policy always applies, and `_score` is always numeric so downstream
  sorting cannot break.

Transport-level hardening: `RETRIEVAL_RERANK_MAX_RETRIES` below 1 is
clamped to one attempt; a non-JSON or non-object response body raises a
clear `RuntimeError` (→ fallback) instead of an obscure `AttributeError`;
non-numeric values for `RETRIEVAL_RERANK_TIMEOUT` /
`RETRIEVAL_RERANK_MAX_RETRIES` / `RETRIEVAL_RERANK_RETRY_BASE_DELAY`
fail-safe to the documented defaults at config-snapshot time. The
`Authorization` header is only sent when an API key is configured.
`top_n <= 0` short-circuits to an empty selection without a provider
call. Per-provider success/failure counters are attributed explicitly by
the caller, so a failed provider *build* (no name known) is never
misattributed to a previously active provider.

## Observability

* `retriva.qa.reranking.reranker_health` — service-level status
  (`ok` / `degraded` / `error` / `disabled` / `unknown`), last error,
  consecutive failures, success/failure totals.
* `retriva.qa.reranking.reranker_metrics` — in-process counters
  (`calls_total`, `success_total`, `failure_total`, `fallback_total`,
  `documents_scored_total`), per-provider breakdown, and latency
  samples (last/avg/max).
* Both are exposed read-only via `GET /internal/reranker/status` on the
  OpenAI-compatible API (secrets redacted; includes the effective config
  and any startup validation issues).
* Per-request phase timing: the profiler records a `rerank_complete`
  phase (visible when `ENABLE_INTERNAL_PROFILER=true`).

## Deployment examples

### Docker Compose

```yaml
environment:
  # Cohere-compatible / OpenRouter (default)
  RETRIEVAL_RERANK_PROVIDER: ""            # or openrouter / cohere
  RETRIEVAL_RERANK_MODEL: cohere/rerank-v3.5
  RETRIEVAL_RERANK_BASE_URL: https://openrouter.ai/api/v1
  RETRIEVAL_RERANK_API_KEY: ${RETRIEVAL_RERANK_API_KEY}
```

### Amazon Bedrock

```yaml
environment:
  RETRIEVAL_RERANK_PROVIDER: bedrock
  RETRIEVAL_RERANK_MODEL: amazon.rerank-v1:0
  RETRIEVAL_RERANK_AWS_REGION: eu-central-1
  # Credentials via IAM role (recommended) or:
  # AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN
```

`RETRIEVAL_RERANK_MODEL` may also be a full ARN, e.g.
`arn:aws:bedrock:eu-central-1::foundation-model/amazon.rerank-v1:0`.
Note that `boto3` is a declared dependency; it is imported lazily, so
deployments that never select `bedrock` pay no import cost.

### Kubernetes (Deployment env snippet)

```yaml
env:
  - name: RETRIEVAL_RERANK_PROVIDER
    value: bedrock
  - name: RETRIEVAL_RERANK_AWS_REGION
    value: eu-central-1
  # Recommended: attach an IAM role via IRSA / kube2iam instead of
  # injecting AWS credentials as secret env vars. When static secrets are
  # required, source them from a Secret:
  - name: AWS_ACCESS_KEY_ID
    valueFrom:
      secretKeyRef: { name: retriva-bedrock, key: access-key-id }
  - name: AWS_SECRET_ACCESS_KEY
    valueFrom:
      secretKeyRef: { name: retriva-bedrock, key: secret-access-key }
```

## Migration and backward compatibility

* No action is required for existing deployments: with
  `RETRIEVAL_RERANK_PROVIDER` unset (the default) the pipeline uses the
  same Cohere-compatible OpenRouter transport, the same defaults, the
  same retry/timeout policy and the same fallback semantics as before.
* The legacy module `retriva.qa.reranker` keeps its public surface
  (`DefaultReranker`, `_call_rerank_api`, `_rerank_batched`,
  `_truncate_documents`) for Pro extensions that patch or subclass it;
  it now also records health/metrics and routes through the provider
  factory.
* There is no per-KB reranker configuration anywhere in Retriva (past or
  present), so no KB data migration is needed.
* The `RETRIEVAL_RERANK_MAX_RETRIES` / `RETRIEVAL_RERANK_TIMEOUT` /
  `RETRIEVAL_RERANK_RETRY_BASE_DELAY` settings replace the previously
  hard-coded constants (`MAX_RETRIES=2`, `REQUEST_TIMEOUT=30.0`,
  `RETRY_BASE_DELAY=1.0`) with identical defaults.

## Testing

* `tests/test_reranker.py` — legacy `DefaultReranker` behavior
  (reordering, metadata preservation, fallback, truncation, batching).
* `tests/test_reranking_providers.py` — provider resolution precedence,
  runtime reload, validation, health, metrics, secret redaction.
* `tests/test_reranker_bedrock.py` — Bedrock request/response contract,
  ARN/region resolution, error wrapping, and end-to-end provider
  selection through the global settings.
* `tests/test_reranker_hardening.py` — malformed-output handling
  (coercion, dedup, clamping, all-invalid fallback), transport payload
  validation, config fail-safety, and per-provider metric attribution.
