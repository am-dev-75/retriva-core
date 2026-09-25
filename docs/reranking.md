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

`bedrock` and `openrouter` are the **canonical** provider names. Friendly
aliases normalize to the canonical name before any snapshot is built, so
cache fingerprints, provider instances, logs, metrics and the status API
always carry the canonical value:

| Accepted value (env) | Canonical name |
| --- | --- |
| `openrouter` | `openrouter` |
| `cohere` | `openrouter` (same Cohere-compatible transport) |
| `bedrock` | `bedrock` |
| `aws_bedrock` | `bedrock` |
| `aws-bedrock` | `bedrock` |

Case and surrounding-whitespace variants are accepted (`" AWS_Bedrock "` →
`bedrock`). Unknown names fail fast with the list of registered providers.

| Canonical name | Transport | Typical use |
| --- | --- | --- |
| `openrouter` (default) | Cohere-compatible `POST /rerank` via `httpx` against `RETRIEVAL_RERANK_BASE_URL` | OpenRouter, Cohere, Jina, self-hosted rerankers. **Default — preserves legacy behavior for existing deployments.** |
| `bedrock` | Amazon Bedrock Rerank API via `boto3` (`bedrock-agent-runtime` client) | AWS-native deployments. |

## Amazon Bedrock — exact API and SDK minimum

The provider uses the **dedicated Rerank API**, confirmed against the
bundled botocore service model (`tests/test_reranker_bedrock_api_model.py`
fails CI if the installed SDK does not expose it — no mocked clients):

* client: `boto3.client("bedrock-agent-runtime")` (service *Bedrock Agent
  Runtime* — the only service exposing `Rerank`; `bedrock-runtime` has no
  `rerank` operation);
* operation: `rerank`;
* request: `queries` (`TEXT`), `sources` (`INLINE` / `TEXT` documents),
  `rerankingConfiguration` with `type='BEDROCK_RERANKING_MODEL'`,
  `bedrockRerankingConfiguration.modelConfiguration.modelArn` and an
  explicit `numberOfResults`;
* response: `results[].index`, `results[].relevanceScore`.

SDK minimum: **boto3/botocore ≥ 1.35.72** (the first release containing
the Rerank operation). Enforced by the SDK-model test and
`boto3>=1.35.72` in `requirements.txt`. Retriva passes no credentials to
`boto3.client()`; botocore resolves and refreshes credentials itself.

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
| `RETRIEVAL_RERANK_STRICT_STARTUP_VALIDATION` | `false` | When `true`, both Retriva APIs **fail to start** on invalid rerank configuration (see below). |
| `RETRIEVAL_RERANK_ENFORCE_EU_REGION` | `false` | EU data-residency enforcement for the `bedrock` provider (see below). |
| `RETRIEVAL_RERANK_ALLOWED_AWS_REGIONS` | `""` | Comma-separated allowed regions for enforcement, e.g. `eu-central-1`. |
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

* API keys are ordinary settings values and are **never logged** or
  returned by any endpoint. The status endpoint reports only
  `api_key_set: true|false`.
* For `bedrock`, credentials are **not** part of Retriva settings and are
  never passed to `boto3.client()`. The standard AWS credential chain
  applies: `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` /
  `AWS_SESSION_TOKEN` environment variables, shared credentials/config
  files, or — recommended — the IAM role of the workload. botocore
  remains fully responsible for refreshing temporary credentials; Retriva
  only rebuilds its cached *client object* when the AWS-relevant
  environment changes (values are compared in memory, never logged).
* Credentials are excluded from cache fingerprints, so key rotation never
  forces provider reconstruction.

### IAM (least privilege)

IAM actions for all Amazon Bedrock services use the **`bedrock`** service
prefix (the SDK client name `bedrock-agent-runtime` is not the IAM
namespace). Retriva's runtime path needs exactly one action.

**Runtime policy (what the rerank provider needs):**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RerankRuntime",
      "Effect": "Allow",
      "Action": ["bedrock:Rerank"],
      "Resource": "arn:aws:bedrock:REGION::foundation-model/cohere.rerank-v3-5:0"
    }
  ]
}
```

`bedrock:InvokeModel` is **retained** in the documentation for
completeness: it is the action an operator would need only when reranking
is routed through an InvokeModel-based transport (e.g. a legacy
Cohere-rerank-via-InvokeModel integration or a Pro extension). The Retriva
provider itself does not call `InvokeModel`:

```json
{
  "Sid": "InvokeModelOnlyIfUsingInvokeModelBasedRerank",
  "Effect": "Allow",
  "Action": ["bedrock:InvokeModel"],
  "Resource": "arn:aws:bedrock:REGION::foundation-model/cohere.rerank-v3-5:0"
}
```

**Marketplace provisioning policy (one-time setup, keep OUT of the
runtime role):** subscribing/entitling a marketplace model (e.g. Cohere
Rerank 3.5) and checking availability are separate operations. Grant them
only to the operator/automation role performing setup — never to the
serving runtime role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "MarketplaceProvisioningOneTime",
      "Effect": "Allow",
      "Action": [
        "bedrock:ListFoundationModelAgreementOffers",
        "bedrock:CreateFoundationModelAgreement",
        "bedrock:DeleteFoundationModelAgreement",
        "bedrock:GetFoundationModelAvailability",
        "bedrock:PutUseCaseForModelAccess",
        "bedrock:GetUseCaseForModelAccess"
      ],
      "Resource": "*"
    }
  ]
}
```

Uncertainty note: AWS account verification was pending in the reference
environment, so resource-level scoping could not be confirmed live. If
`bedrock:Rerank` rejects the resource-scoped form, use `"Resource": "*"`
until verification is possible, and validate with the opt-in smoke test
below. See the **IAM validation record** for combinations confirmed
against AWS. Action names are validated against the botocore service
models by `tests/test_reranker_iam_docs.py`.

### IAM validation record

Record here which action/resource combinations AWS accepted (fill in
after running the smoke test against a verified account):

| Date | Action | Resource | Result | Notes |
| --- | --- | --- | --- | --- |
| 2026-09-25 | `bedrock:Rerank` | `arn:aws:bedrock:eu-central-1::foundation-model/cohere.rerank-v3-5:0` | **not yet confirmable** | Audit environment has no AWS credentials (`NoCredentialsError`) — no request reached AWS; account-verification status unconfirmed. |
| — | — | — | — | After verification: run the gated smoke test (below) with runtime credentials and record each accepted action/resource pair here. |

### Model entitlement

Marketplace models (e.g. Cohere Rerank 3.5, `cohere.rerank-v3-5:0`)
require a one-time **subscription/entitlement** for the AWS account in
that region (Bedrock console → model access) — see the separate
provisioning policy above. Without it the provider reports
`model_access_error` / `marketplace_entitlement_error` and the standard
fallback policy applies.

## Strict startup validation

`RETRIEVAL_RERANK_STRICT_STARTUP_VALIDATION=true` (default `false`):

* **strict mode**: both Retriva APIs (OpenAI-compatible and ingestion)
  **fail to start** when the rerank configuration is invalid — unsupported
  provider, missing region or model, unsupported SDK operation (Rerank
  missing from the installed botocore), invalid endpoint URL, invalid
  numeric settings, or EU-region-policy violations. No AWS request is
  made during validation.
* **non-strict mode (default, backward compatible)**: startup proceeds;
  issues are logged (log-and-continue, the Qdrant-init convention), and
  invalid numeric settings are defaulted with a clear, sanitized warning
  plus a `degraded` configuration status on
  `/internal/reranker/status`.

## EU region enforcement

Opt-in for AWS deployments (data residency):

```bash
RETRIEVAL_RERANK_PROVIDER=bedrock
RETRIEVAL_RERANK_ENFORCE_EU_REGION=true
RETRIEVAL_RERANK_ALLOWED_AWS_REGIONS=eu-central-1
```

When enabled, violations are rejected — never rerouted, never falling
back to another provider:

* the resolved region must be in the allowed list (at startup and at
  rank time);
* the model ARN's region must match the configured client region;
* endpoint overrides (`AWS_ENDPOINT_URL`,
  `AWS_ENDPOINT_URL_BEDROCK_AGENT_RUNTIME`) are rejected;
* any violation raises a policy `RerankProviderError` (category
  `invalid_request`) and the pipeline applies the standard vector-order
  fallback with the SAME provider selected.

Defaults are backward compatible: with the flag unset nothing changes.

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

### Bedrock error normalization

AWS SDK errors are classified into safe categories; the status API
receives only the category and AWS error code — never the raw AWS
message:

`account_verification_pending`, `authentication_error`,
`authorization_error`, `credentials_expired`, `model_access_error`,
`marketplace_entitlement_error`, `model_not_found`, `invalid_request`,
`throttled`, `timeout`, `service_unavailable`, `network_error`,
`invalid_response`.

The 403 `ValidationException` containing *“Your account is currently
being verified”* is classified as `account_verification_pending` with a
fixed guidance message.

### Score preservation

During successful reranking each returned chunk carries:

* `_retrieval_score` — the original retrieval score;
* `_rerank_score` — the provider's relevance score;
* `_score` — the provider's relevance score (legacy key used by
  downstream sorting/diversity filters).

Fallback and disabled behavior preserve the original `_score` and never
set `_rerank_score` / `_retrieval_score`. Internal fields
(`_rerank_score`, `_retrieval_score`) are stripped from external API
payloads (v2 retrieval responses); `_score` remains, as it was part of
retrieval responses before the subsystem existed. Chat citations are
built from explicit fields only.

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
call.

## Client lifecycle

* **Bedrock**: one `boto3.client("bedrock-agent-runtime")` per provider
  instance, created lazily, reused for every call; rebuilt only when the
  region or AWS-relevant environment changes. Timeouts (`read_timeout` /
  `connect_timeout`) and bounded botocore adaptive retries
  (`max_attempts = RETRIEVAL_RERANK_MAX_RETRIES`) are applied at client
  construction; no application-level retry loop wraps the Bedrock call,
  so retries never multiply.
* **OpenRouter**: the legacy transport creates an `httpx.Client` per
  request with the configured per-call timeout. Reusing a shared client
  is deliberately deferred: a shared client pins the timeout at first
  use (settings changes must remain live per call), shares connection
  state across configuration changes, and the legacy module surface
  (`_call_rerank_api`) that Pro extensions patch is per-call by design.
  `httpx.Client` construction opens no sockets, so per-call creation
  leaks nothing.
* **No unbounded thread pools**: reranking introduces no threads; the
  retrieval path runs on the caller's thread (Starlette's bounded
  threadpool for async endpoints), and Celery workers are bounded by
  `CELERY_WORKER_CONCURRENCY`.
* **Provider cache**: one provider instance per canonical configuration;
  reconstruction is thread-safe (built under the cache lock; exactly one
  build per config change even under concurrent retrievals).

## Observability

* `retriva.qa.reranking.reranker_health` — service-level status
  (`ok` / `degraded` / `error` / `disabled` / `unknown`), last error
  **category** (normalized), bounded last-error message (≤ 200 chars,
  no raw provider payloads, no stack traces), consecutive failures,
  success/failure totals.
* `retriva.qa.reranking.reranker_metrics` — in-process counters
  (`calls_total`, `success_total`, `failure_total`, `fallback_total`,
  `documents_scored_total`), per-provider breakdown, and latency
  samples (last/avg/max).
* Both are exposed read-only via `GET /internal/reranker/status` on the
  OpenAI-compatible API.

### Status endpoint security audit

`GET /internal/reranker/status` is **not authenticated** (consistent with
the existing `/health` and `/internal/profiler/log` endpoints) and runs
on the Retriva Core HTTP app. In the containerized deployment
`retriva-core` publishes host port `${CORE_PORT:-8001}` and the Retriva
Gateway does **not** proxy arbitrary core paths — so the endpoint is
reachable by anyone who can reach the published core port; do not expose
`CORE_PORT` publicly and firewall it in production. The payload is
normalized and bounded by construction:

* never returns credentials (only `api_key_set: true|false`);
* never returns raw SDK exceptions — errors are normalized to categories
  plus sanitized, length-capped messages;
* never returns query or document text, stack traces, or signed headers;
* failure history is bounded to the single last error plus counters.
* Per-request phase timing: the profiler records a `rerank_complete`
  phase (visible when `ENABLE_INTERNAL_PROFILER=true`).

## Live smoke test (opt-in)

`tests/test_reranker_bedrock_smoke.py` performs a REAL rerank call using
the normal Retriva provider path. It is **skipped by default and never
runs in normal CI**; run it manually after account verification and model
entitlement:

```bash
RETRIVA_RUN_BEDROCK_SMOKE_TEST=1 \
RETRIEVAL_RERANK_PROVIDER=bedrock \
RETRIEVAL_RERANK_AWS_REGION=eu-central-1 \
RETRIEVAL_RERANK_MODEL=cohere.rerank-v3-5:0 \
  PYTHONPATH=src python -m pytest tests/test_reranker_bedrock_smoke.py -v
```

The test requires `eu-central-1` and `cohere.rerank-v3-5:0`, uses
synthetic documents, and expects the Qdrant document to rank first. If
the AWS account is still being verified, it fails with a clear
`account_verification_pending` report instead of an opaque AWS error.

## Deployment examples

### Docker Compose

```yaml
environment:
  # Cohere-compatible / OpenRouter (default)
  RETRIEVAL_RERANK_PROVIDER: ""            # or openrouter / cohere
  RETRIEVAL_RERANK_MODEL: cohere/rerank-v3.5
  RETRIEVAL_RERANK_BASE_URL: https://openrouter.ai/api/v1
  RETRIEVAL_RERANK_API_KEY: ${RETRIEVAL_RERANK_API_KEY}
  # Optional: strict startup validation and EU enforcement
  #RETRIEVAL_RERANK_STRICT_STARTUP_VALIDATION: "false"
  #RETRIEVAL_RERANK_ENFORCE_EU_REGION: "false"
  #RETRIEVAL_RERANK_ALLOWED_AWS_REGIONS: eu-central-1
```

### Amazon Bedrock (canonical `bedrock`; aliases `aws_bedrock` / `aws-bedrock` accepted)

```yaml
environment:
  RETRIEVAL_RERANK_PROVIDER: bedrock
  RETRIEVAL_RERANK_MODEL: cohere.rerank-v3-5:0   # or amazon.rerank-v1:0 / full ARN
  RETRIEVAL_RERANK_AWS_REGION: eu-central-1
  # Credentials via IAM role (recommended) or:
  # AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN
  # Optional data-residency enforcement:
  #RETRIEVAL_RERANK_ENFORCE_EU_REGION: "true"
  #RETRIEVAL_RERANK_ALLOWED_AWS_REGIONS: eu-central-1
```

`RETRIEVAL_RERANK_MODEL` may also be a full ARN, e.g.
`arn:aws:bedrock:eu-central-1::foundation-model/cohere.rerank-v3-5:0`.
Note that `boto3` is a declared dependency (**>= 1.35.72**, the first SDK
containing the Rerank operation); it is imported lazily, so deployments
that never select `bedrock` pay no import cost. Model settings changes
require a process restart (env vars are read once at startup); rotating
AWS credentials without restart is detected and rebuilds the boto3 client.

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
* `tests/test_reranker_bedrock_api_model.py` — CI test on the INSTALLED
  botocore service model: fails if the SDK does not expose `Rerank` on
  `bedrock-agent-runtime` with the shapes Retriva needs, and if the SDK
  or the declared `boto3` floor predates 1.35.72.
* `tests/test_reranker_bedrock_smoke.py` — opt-in LIVE AWS smoke test
  (skipped by default; see above).
* `tests/test_reranker_canonical.py` — canonical naming: `aws_bedrock` /
  `aws-bedrock` / case variants → `bedrock` in configs, fingerprints,
  cache identity, instances and status output.
* `tests/test_reranker_eu_policy.py` — EU enforcement (rank-time and
  static validation) and the no-reroute/no-provider-fallback policy.
* `tests/test_reranker_strict_startup.py` — strict vs non-strict startup
  behavior, numeric defaulting warnings, no-AWS-request guarantee.
* `tests/test_reranker_errors.py` — Bedrock error normalization,
  account-verification classification, health sanitization/bounding.
* `tests/test_reranker_lifecycle.py` — cache identity/instance reuse,
  thread-safe reconstruction, boto3 client construction (timeouts,
  bounded botocore retries, no manually frozen credentials), httpx
  timeout lifecycle.
* `tests/test_reranker_hardening.py` — malformed-output handling
  (coercion, dedup, clamping, all-invalid fallback), transport payload
  validation, config fail-safety, and per-provider metric attribution.
* `tests/test_reranker_score_preservation.py` — `_retrieval_score` /
  `_rerank_score` / `_score` semantics, fallback/disabled behavior, and
  external-payload sanitization.
