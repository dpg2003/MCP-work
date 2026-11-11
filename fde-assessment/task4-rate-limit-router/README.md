# Task 4 — Rate-Limiting & Model Fallback Router

A resilient LLM-gateway routing layer: a token-aware sliding-window rate limiter
persisted in on-disk SQLite, automatic failover from a primary model provider to
a secondary, and one standardized error shape for every failure.

## Files

| File | Purpose |
| --- | --- |
| `app.py` | The HTTP gateway wiring limiter and router together. Entry point. |
| `rate_limiter.py` | Sliding window log in SQLite, transactional admission, reconciliation |
| `admission.py` | Group-commit front-end keeping SQLite off the event loop |
| `concurrency.py` | Per-tenant in-flight ceiling (`max_concurrent`) |
| `tenants.py` | API key to tenant/tier/quota resolution, hashed and constant-time |
| `router.py` | N-provider failover policy and retry rules |
| `circuit_breaker.py` | Stops paying the outage latency tax on a known-dead provider |
| `providers.py` | Provider interface; normalises every upstream failure at the boundary |
| `errors.py` | The single client-facing error shape |
| `tokens_estimate.py` | Pre-flight token estimation, pluggable and script-aware |
| `fake_upstream.py` | Fake primary/secondary endpoints with latency and failure injection |
| `tests/test_rate_limiter.py` | Budgets, boundaries, eviction, persistence, races |
| `tests/test_admission.py` | Group commit matches a serial oracle under concurrency |
| `tests/test_concurrency.py` | The in-flight ceiling, isolation, and bookkeeping |
| `tests/test_concurrency_e2e.py` | The gateway applies the ceiling and never leaks a slot |
| `tests/test_tenants.py` | Key resolution, tiers, overrides, and that keys never leak |
| `tests/test_router.py` | Failover triggers, timeout precision, flapping, sanitisation |
| `tests/test_circuit_breaker.py` | State transitions and traffic-driven recovery |
| `tests/test_tokens_estimate.py` | Estimator contract and script-aware charging |
| `tests/test_operations.py` | Health, readiness, and the operational endpoints |
| `tests/test_gateway_e2e.py` | The concerns together, over HTTP |

See the [root README](../../README.md) for the per-function reference.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# terminal 1 (this one) — fake primary + secondary model endpoints
uvicorn fake_upstream:app --port 9100

# terminal 2 — the gateway
source .venv/bin/activate
uvicorn app:app --port 8080
```

Already ran `../../setup.sh`? Use `source ../../.venv/bin/activate` in each
terminal instead of creating a project venv.

Environment:

| Variable | Default | Effect |
| --- | --- | --- |
| `RATE_LIMIT_DB` | `rate_limit.sqlite3` | SQLite file, relative to the working directory |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Sliding-window length |
| `TENANTS_FILE` | unset | Tenant/quota JSON (see below); unset uses the demo tenants and logs a warning |
| `PROVIDER_TIMEOUT_MS` | `3000` | Per-attempt provider deadline |
| `PRIMARY_URL` / `SECONDARY_URL` | `http://127.0.0.1:9100/{primary,secondary}/v1/complete` | Provider endpoints |
| `PROVIDER_MAX_CONNECTIONS` / `PROVIDER_MAX_KEEPALIVE` | `100` / `20` | Provider connection pool |
| `RATE_LIMIT_TOKENS` | `50000` | The limiter's fallback token limit. The HTTP gateway always applies the tenant's own quota instead, so this does **not** change what `/v1/complete` admits; quotas come from `TENANTS_FILE`. |

The demo tenants are `key-acme` (tier `pro`: 50,000 tokens and 300 requests per
window, 16 in flight) and `key-globex` (tier `free`: 10,000 tokens, 60 requests,
4 in flight).

```bash
curl -sX POST localhost:8080/v1/complete \
     -H 'content-type: application/json' -H 'X-API-Key: key-acme' \
     -d '{"prompt":"hello","max_tokens":100}'
```

```json
{"request_id":"req_c8257cf06ef841cc","text":"[primary] hello","provider":"primary","failed_over":false,
 "tenant":{"id":"tenant-acme","tier":"pro"},
 "usage":{"estimated_tokens":102,"actual_tokens":21,"tenant_window_tokens":21,"limit_tokens":50000,"limit_requests":300}}
```

Drive the providers to exercise failover:

```bash
curl -sX POST localhost:9100/_control/primary -d '{"mode":"429"}' -H 'content-type: application/json'
# next request -> {"provider":"secondary","failed_over":true, ...}

curl -sX POST localhost:9100/_control/primary -d '{"mode":"hang"}' -H 'content-type: application/json'
# next request -> times out at PROVIDER_TIMEOUT_MS, then {"provider":"secondary", ...}

curl -sX POST localhost:9100/_control/secondary -d '{"mode":"500"}' -H 'content-type: application/json'
# both down (the timed-out primary is not retried; the 500 is retried once) ->
# {"error":{"type":"upstream_unavailable",
#           "message":"No model provider was able to serve this request",
#           "request_id":"req_bccee370...",
#           "details":{"attempts":["primary:timeout","secondary:unavailable","secondary:unavailable"]}}}

# back to normal
curl -sX POST localhost:9100/_control/primary -d '{"mode":"ok"}' -H 'content-type: application/json'
curl -sX POST localhost:9100/_control/secondary -d '{"mode":"ok"}' -H 'content-type: application/json'
```

Over the budget — one request whose estimate exceeds the `free` tier's 10,000
tokens:

```bash
curl -sX POST localhost:8080/v1/complete \
     -H 'content-type: application/json' -H 'X-API-Key: key-globex' \
     -d '{"prompt":"hello","max_tokens":20000}'
```

```json
{"error":{"type":"rate_limit_exceeded","message":"Rate limit exceeded",
          "request_id":"req_6379dbfa...","details":{"limit":"tokens","tier":"free",
          "limit_tokens":10000,"limit_requests":60,"window_seconds":60.0,
          "used_tokens":0,"used_requests":0,"requested_tokens":20002,"retry_after_seconds":0.0}}}
```

HTTP 429 (shown from a fresh database; `used_*` and `retry_after_seconds`
reflect earlier usage in the window). `max_tokens` above 32,000 is an
`invalid_request` (HTTP 400) instead.

## Run the tests

```bash
pip install -r requirements.txt
pytest
```

209 tests. The window-eviction tests use an injectable clock rather than
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

**Three dimensions, not one.** Tokens per minute *and* requests per minute, both
over the same sliding window, answered by one query against the covering index
so the second dimension costs no extra round trip. A tenant sending many tiny
prompts exhausts a provider's connections long before it exhausts a token
budget, so tokens alone were never sufficient. A refusal names which budget ran
out (`"limit": "tokens"`, `"requests"` or `"concurrency"`).

The third dimension is `max_concurrent`, in [`concurrency.py`](concurrency.py),
and it is worth saying plainly that for a while this one was a lie: the field
was defined in `Quota`, documented, and reported over `/v1/quota`, while nothing
anywhere enforced it. An API that advertises a limit it does not apply is worse
than one that never offered it.

A window bounds *total work over time*; concurrency bounds *simultaneous work
right now*, and nothing else in the stack covers that. A tenant that opens 500
sockets at once is comfortably inside a 50,000-token minute for a while, yet it
has already taken every upstream connection in the pool and every other tenant
is queued behind it.

Two decisions inside it are deliberate:

- **Reject, do not queue.** The obvious implementation is a semaphore per
  tenant that callers await. That converts a concurrency limit into unbounded
  latency — a waiting caller still holds a socket and a handler task, so under a
  sustained burst the queue grows without bound and everything times out. The
  gateway degrades instead of shedding. Returning 429 immediately keeps queue
  depth bounded by the limit and lets clients back off. The advertised
  `retry_after_seconds` is accordingly short: unlike a window rejection, a
  concurrency rejection clears as soon as any one in-flight request finishes.
- **No lock.** `try_acquire` reads the counter and writes it back with no
  `await` in between, which on a single-threaded event loop cannot be
  interleaved. Adding an `asyncio.Lock` would *introduce* a suspension point and
  make the operation genuinely racy in the window it was meant to protect.

The slot is held for the whole request and released in a `finally`, including on
the window rejection that returns before routing ever starts. A leaked slot is
worse than no limit, because the tenant's depth would ratchet up until every
later request was shed permanently — so each exit path from the handler has its
own test in `tests/test_concurrency_e2e.py`.

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
- **`/v1/quota`** — the calling tenant's own tier, limits, window usage and
  current in-flight depth.
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
