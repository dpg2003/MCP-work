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

| Primary outcome | Router behaviour |
| --- | --- |
| Success | Return it. Secondary is never called (asserted by call count). |
| HTTP 429 | Fail over to secondary. |
| Timeout at the deadline (3000 ms default) | Fail over to secondary. |
| Connection error / 5xx / undecodable body | Fail over to secondary. |
| Non-retryable 4xx (not 429) | **No** failover — standardized error. |

Connection errors and 5xx join the two required triggers because they mean the
same thing operationally, and a router that fails over on a timeout but not on a
connection refusal has a hole in it. A 4xx is different in kind: it is a
statement about the *request*, so retrying elsewhere just burns the secondary's
quota.

The deadline is enforced inside the provider (`httpx` timeout) rather than as a
router-level `asyncio.wait_for`, so it covers connect, write, read, and
pool-acquire time — and cancelling the task would otherwise leave the socket to
be reaped later.

**No circuit breaker, deliberately.** The router holds no health state, so a
flapping primary cannot wedge it: every request re-evaluates from scratch and
recovery is immediate. The cost is one failed attempt per request during a
sustained outage. A test alternates the primary's health 200 times and asserts
every request lands on the right provider, plus that the connection pool stays
bounded across 150 failovers. At high volume this would be the thing to revisit
— a breaker with a half-open probe — and it is a self-contained change because
the router is the only thing that would need to know.

## Standardized errors

Every failure — validation, auth, rate limit, upstream, and an unexpected
internal exception — exits through `errors.GatewayError` and serializes as:

```json
{"error": {"type": "...", "message": "...", "request_id": "req_...", "details": {}}}
```

`details` is populated **only** from values the gateway itself computed.
Provider-specific failures are normalised into gateway-owned exception types at
the `providers.py` boundary, so nothing above that layer can even see an httpx
exception, an upstream status code, or an upstream body — the guarantee is
structural rather than a promise to remember. Upstream detail is logged against
`request_id` and goes no further.

The fake upstream returns a body containing a stack trace, a file path, an
internal hostname, and an `sk-live-` API key fragment; tests assert none of it
appears in any client response, on either the single-provider or
both-providers-down path.

## Performance

### Limiter throughput: 7,080 → 18,351 ops/s

- **Covering index** `(tenant, ts, tokens)` answers the hot `SUM` from the index
  alone, without touching the table.
- **Amortised eviction.** Deleting expired rows on every request was pure write
  amplification: every read already filters on `ts > cutoff`, so an un-deleted
  expired row cannot influence any answer. The `DELETE` is housekeeping, and
  housekeeping does not need to run a thousand times a second.

### Getting SQLite off the event loop: 647 → 1,956 req/s

Admission ran SQLite directly inside a coroutine, so a commit spike (28 ms
observed) stalled *every* in-flight request on the worker, not just its own.

The obvious fix — `asyncio.to_thread` per call — measured **worse**: the thread
hop costs ~260 µs against 54 µs of actual database work, making it slower than
blocking, just politer.

So the limiter uses **group commit**, the same technique a database WAL uses.
Concurrent admissions queue; one worker drains the whole group and hands it to a
single thread, which decides the batch inside one transaction. Sixty-four
requests pay one hop and one commit instead of sixty-four of each — so throughput
*improves* with concurrency instead of collapsing.

| Concurrency | Throughput | p50 | p99 |
| --- | --- | --- | --- |
| 4 | 1,500 req/s | 1.9 ms | 3.6 ms |
| 16 | 1,934 req/s | 4.8 ms | 9.2 ms |
| 64 | 1,985 req/s | 16.0 ms | 49.9 ms |

Before: 647 req/s at concurrency 64, p50 95 ms, p99 152 ms.

**Semantics are unchanged.** `try_consume_many` decides a batch in arrival order,
each request against the running total including everything admitted earlier in
the same batch — exactly what sequential calls produce. The in-memory running
total is exact rather than approximate because `BEGIN IMMEDIATE` holds the write
lock for the whole transaction. `tests/test_admission.py` asserts this against a
serial oracle over 500 randomised batches, and separately asserts the limit is
never exceeded under concurrency.

A lone request is never delayed waiting for a batch that may not arrive: the
worker takes whatever is *already* queued and dispatches immediately.

## Operational surface

- **`/healthz`** — liveness. Cheap and dependency-free, because a probe that
  fails on a downstream blip makes an orchestrator restart a healthy pod.
- **`/readyz`** — readiness. Touches the database admission depends on, and
  returns 503 so the instance leaves rotation rather than serving errors. The two
  are deliberately independent, and a test asserts liveness still passes when
  readiness fails.
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
