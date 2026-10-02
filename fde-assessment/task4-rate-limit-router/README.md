# Task 4 — Rate-Limiting & Model Fallback Router

A resilient LLM-gateway routing layer: a token-aware sliding-window rate limiter
persisted in on-disk SQLite, automatic failover from a primary model provider to
a secondary, and one standardized error shape for every failure.

## Files

| File | Purpose |
| --- | --- |
| `app.py` | The HTTP gateway wiring limiter and router together. Entry point. |
| `rate_limiter.py` | Sliding window log in SQLite, transactional admission, reconciliation |
| `router.py` | Primary/secondary failover policy |
| `providers.py` | Provider interface; normalises every upstream failure at the boundary |
| `errors.py` | The single client-facing error shape |
| `tokens_estimate.py` | Pre-flight token estimation |
| `fake_upstream.py` | Fake primary/secondary endpoints with latency and failure injection |
| `tests/test_rate_limiter.py` | Budgets, boundaries, eviction, persistence, races |
| `tests/test_router.py` | Failover triggers, timeout precision, flapping, sanitisation |
| `tests/test_gateway_e2e.py` | The three concerns together, over HTTP |

See the [root README](../../README.md) for the per-function reference.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# terminal 1 — fake primary + secondary model endpoints
uvicorn fake_upstream:app --port 9100

# terminal 2 — the gateway
uvicorn app:app --port 8080
```

Environment: `RATE_LIMIT_DB` (default `rate_limit.sqlite3`), `RATE_LIMIT_TOKENS`
(50000), `RATE_LIMIT_WINDOW_SECONDS` (60), `PROVIDER_TIMEOUT_MS` (3000),
`PRIMARY_URL`, `SECONDARY_URL`.

```bash
curl -sX POST localhost:8080/v1/complete \
     -H 'content-type: application/json' -H 'X-API-Key: key-acme' \
     -d '{"prompt":"hello","max_tokens":100}'
```

```json
{"request_id":"req_7bcb9fdf73944962","text":"[primary] hello","provider":"primary",
 "failed_over":false,
 "usage":{"estimated_tokens":102,"actual_tokens":21,"tenant_window_tokens":21,"limit_tokens":150}}
```

Drive the providers to exercise failover:

```bash
curl -sX POST localhost:9100/_control/primary -d '{"mode":"429"}' -H 'content-type: application/json'
# next request -> {"provider":"secondary","failed_over":true, ...}

curl -sX POST localhost:9100/_control/primary -d '{"mode":"hang"}' -H 'content-type: application/json'
# next request -> times out at PROVIDER_TIMEOUT_MS, then {"provider":"secondary", ...}

curl -sX POST localhost:9100/_control/secondary -d '{"mode":"500"}' -H 'content-type: application/json'
# both down ->
# {"error":{"type":"upstream_unavailable",
#           "message":"No model provider was able to serve this request",
#           "request_id":"req_ffc26b64...",
#           "details":{"attempts":["primary:timeout","secondary:unavailable"]}}}
```

Over the budget:

```json
{"error":{"type":"rate_limit_exceeded","message":"Token rate limit exceeded",
          "request_id":"req_db66...","details":{"limit_tokens":150,"window_seconds":60.0,
          "used_tokens":63,"requested_tokens":102,"retry_after_seconds":59.93}}}
```

## Run the tests

```bash
pip install -r requirements.txt
pytest
```

67 tests. The window-eviction tests use an injectable clock rather than
`sleep(60)`. The timeout tests run against a real uvicorn server on a real
socket — httpx's `ASGITransport` awaits the app directly, so there is no socket
for a read timeout to fire on, and a timeout test through it would prove
nothing.

## Rate limiter: sliding window log

Every admitted request writes one row `(tenant, timestamp, tokens)`. Current
usage is the sum of a tenant's rows inside the trailing window; older rows are
deleted on the way past. Why this and not the cheaper options:

- A **fixed window** ("50k per calendar minute") permits a 100k burst across a
  boundary — 50k at 11:59:59 and 50k at 12:00:00. For a *token* budget that maps
  straight to spend and to upstream capacity, that is the worst failure mode.
- A **sliding-window counter** (weighted blend of previous and current window)
  fixes the burst but only approximates, and it cannot answer "exactly 50,000 is
  allowed, 50,001 is not" — a stated requirement.
- The log is exact. Its cost is one row per request plus a periodic delete,
  which is nothing next to an LLM call. The same interface backs a Redis sorted
  set unchanged if row volume ever mattered.

**Concurrency.** Check-then-write is a race: two requests can both read 49,000
and both admit 5,000. Every admission runs inside one `BEGIN IMMEDIATE`
transaction, which takes SQLite's RESERVED lock *before* the read, serializing
read-decide-write across threads and processes. A test fires 20 simultaneous
5,000-token requests through a barrier and asserts exactly 10 are admitted;
another runs 40 requests across four independent limiter objects sharing one
database file.

**Persistence.** State is a real file. Tests assert enforcement survives both a
fresh limiter object and a genuinely separate Python interpreter, and that
persisted entries still expire correctly after a restart.

## Token estimation

`estimate_tokens = ceil(len(prompt) / 4) + max_tokens`.

Admission control has to charge *something* before the request is made — that is
what admission control is — but the true count is not known until the provider
answers. So the gateway reserves an estimate, then calls `reconcile()` with the
provider's reported usage. Notes on the scheme:

- `len(prompt) / 4` is the standard rule of thumb for English BPE tokenizers.
  It is approximate, but it is *fast*: no tokenizer to load, no per-provider
  vocabulary to keep in sync, and no risk of the admission path becoming the
  slowest thing in the request.
- Adding `max_tokens` reserves the output budget. Charging only for the prompt
  would let a tenant fire tiny prompts with huge `max_tokens` and blow through
  the budget before a single reconciliation lands.
- The estimate errs high on purpose. Over-charging is corrected within
  milliseconds; under-charging is an unmetered request.
- If both providers fail, the reservation is *released*: a tenant is not charged
  for the gateway's own outage.

## Failover policy

| Provider outcome | Router behaviour |
| --- | --- |
| Success | Return it. Later providers untouched. |
| HTTP 429 | Retry once (cheap), then advance to the next provider. |
| Connection error / 5xx / undecodable body | Retry once, then advance. |
| Timeout at the deadline | Advance **immediately** — no retry. |
| Non-retryable 4xx (not 429) | Stop. Another provider rejects it identically. |

**Why timeouts are not retried.** A timeout has already spent the entire
deadline. Retrying it doubles what the client waits — 6 s on a 3 s budget — to
re-ask a provider that just failed to answer. A 429 or a refused connection
fails in milliseconds, so retrying *those* costs almost nothing and genuinely
absorbs a blip. Worst-case latency is therefore one deadline per provider, not
one per attempt, and `test_a_single_timeout_costs_one_deadline_not_two` holds it
there.

**An ordered chain, not a hardcoded pair.** `ModelRouter(providers=[a, b, c, d])`
walks the list in order. `primary` and `secondary` remain as properties, so
callers written against the old shape are unaffected.

## Circuit breaking

This is the fix for the defect that mattered most. The previous router held no
health state, so a provider that was hard down was re-attempted on **every**
request — meaning every request paid the full 3000 ms deadline for as long as
the outage lasted. A three-second tax on 100% of traffic.

Each provider now has a `CircuitBreaker`:

| State | Behaviour |
| --- | --- |
| `CLOSED` | Normal. `failure_threshold` *consecutive* failures trip it open. |
| `OPEN` | Calls refused in microseconds — no socket, no deadline to wait out. |
| `HALF_OPEN` | After `recovery_seconds`, a limited probe is admitted. Success closes it; failure re-opens and restarts the clock. |

Measured, with a 3 s deadline against a hard-down primary:

```
req 1:  3000 ms  via secondary  attempts=['primary:timeout', 'secondary:ok']
req 2:  3000 ms  via secondary  attempts=['primary:timeout', 'secondary:ok']
req 3:     0 ms  via secondary  attempts=['primary:circuit_open', 'secondary:ok']
req 4:     0 ms  via secondary  attempts=['primary:circuit_open', 'secondary:ok']
```

Only *consecutive* failures count, so one success clears the tally and a blip
never trips a healthy provider. A non-retryable 4xx is recorded as
*indeterminate* — it describes the request, not the provider, so it neither
trips a breaker nor clears a tally.

**The test that would have caught the original defect** is
`test_sustained_outage_latency_stays_far_below_the_deadline`. It asserts a
*cost*, not a mechanism: steady-state latency during an outage must stay under
a tenth of the deadline. Disabling the breaker fails it at **3.00 s against a
0.30 s budget** — a defect no mechanism test in the old suite could see.

## Rate limits: per tenant, multi-dimensional

The limiter machinery was always sound; the product around it was not. It had
one global limit for everybody and a two-entry plaintext key dict in source.

**Quotas are configuration now.** `tenants.py` resolves an API key to a
`Tenant` carrying its own `Quota`. Tiers ship as `free` / `pro` / `enterprise`,
per-tenant `overrides` cover negotiated limits, and `TENANTS_FILE` points at a
JSON document — see [`examples/tenants.example.json`](../../examples/tenants.example.json).
With nothing configured, a clearly-labelled demo registry is used and the
service logs a warning.

**Two dimensions, not one.** Tokens per minute *and* requests per minute, both
over the same sliding window, answered by one query against the covering index
so the second dimension costs no extra round trip. A tenant sending many tiny
prompts exhausts a provider's connections long before it exhausts a token
budget, so tokens alone were never sufficient. A refusal names which budget ran
out (`"limit": "tokens"` or `"requests"`).

**Keys are not recoverable.** Only a SHA-256 hash is stored, comparison is
constant-time across every entry, and keys are never logged — only the tenant
id they resolve to. Hashing is unsalted and unstretched deliberately: these are
high-entropy machine-generated secrets, so there is no dictionary to defend
against and per-request Argon2 would price every call. A test asserts a
presented key appears in neither the response nor the logs.

**Token estimation is pluggable and script-aware.** The default heuristic
charges Latin text at ~4 characters per token and non-Latin (CJK, emoji) far
more densely, because 4:1 under-charges those by roughly 4x. `max_tokens` is
reserved in full, so tiny prompts with huge output budgets cannot slip past.
An exact tokenizer is deliberately *not* the default — Anthropic's counting is
a network call, and putting a round trip in front of every admission decision
to then fail closed when it is slow is the wrong trade. The estimate errs high
and is reconciled against real usage within milliseconds.

## Operational surface

- **`/healthz`** — liveness. Cheap and dependency-free, because a probe that
  fails on a downstream blip makes an orchestrator restart a healthy pod.
- **`/readyz`** — readiness. Checks the database admission depends on *and*
  provider health: if every breaker is open the instance is up but cannot
  serve, so it returns 503 and leaves rotation rather than serving errors. The two
  are deliberately independent, and a test asserts liveness still passes when
  readiness fails.
- **`/v1/providers`** — per-provider breaker state: which providers are being
  skipped and how long until the next probe, so an outage is visible without
  reading logs.
- **`/v1/quota`** — the calling tenant's own tier, limits and window usage.
- **Bounded provider pool** (100 connections, 20 keep-alive).
- **Graceful shutdown** drains the group-commit workers on lifespan exit, with a
  test asserting no accepted admission is lost.

## Key design tradeoff

The main one is **reserve-then-reconcile versus charge-on-completion**. Charging
after the fact is exact and simple, but it means the limit is only enforced
*retrospectively* — a burst of concurrent requests all see zero usage and all
get through, which is precisely the case a token budget exists to prevent.
Reserving an estimate up front costs a second write per request and makes the
in-window number briefly pessimistic, but the limit actually holds under
concurrency. That is the trade worth making for a budget that maps to spend.

The second is the **sliding window log over a counter**: exact "50,000 yes,
50,001 no" semantics and no boundary burst, paid for with one row per request.
At LLM-gateway request rates the row is free; at a million QPS it would not be,
and the same interface moves to a Redis sorted set without touching a caller.
