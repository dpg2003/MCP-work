# FDE Assessment — MCP & LLM Gateways

Four runnable projects covering the four assessment tasks: an MCP server, an
MCP security gateway, a streaming PII-redaction guardrail, and a
rate-limiting/fallback router.

Each lives in its own folder with its own pinned `requirements.txt`, its own
pytest suite, and its own README.

---

## Quick start

```bash
./setup.sh        # create .venv and install all four projects' dependencies
./run_tests.sh    # documentation check + all four test suites
./bench/run_all.sh   # optional: reproduce the performance numbers
```

That is the whole setup. `run_tests.sh` runs each suite in its own process,
continues past a failing project, names the failures, and exits non-zero.
Extra arguments pass through to pytest (`./run_tests.sh -x -k redact`).

```
===== documentation coverage =====
source           183/ 183 (100.0%)  [enforced]
test support      97/  97 (100.0%)  [enforced]
test functions    65/ 243 ( 26.7%)  [reported]
documentation check passed
===== task1-mcp-server =====        54 passed
===== task2-security-gateway =====  84 passed
===== task3-pii-redaction-gateway = 219 passed
===== task4-rate-limit-router ===== 100 passed
===== summary =====
all suites passed
```

**457 tests, no network access and no API key required.**

### Running one project

```bash
cd fde-assessment/task1-mcp-server
pip install -r requirements.txt
python server.py          # run it
pytest                    # test it
```

| Project | Run command | Tests |
| --- | --- | --- |
| `task1-mcp-server` | `python server.py` | 54 |
| `task2-security-gateway` | `uvicorn downstream:app --port 9001` + `uvicorn gateway:app --port 9000` | 84 |
| `task3-pii-redaction-gateway` | `uvicorn app:app --port 8000` | 219 |
| `task4-rate-limit-router` | `uvicorn fake_upstream:app --port 9100` + `uvicorn app:app --port 8080` | 100 |

---

## What each project is

### Task 1 — MCP server with strict validation and stdio handling

An MCP server over **stdio** exposing `get_customer_record` and
`trigger_refund`. Every input is validated by a strict Pydantic model, failures
surface as real JSON-RPC error codes, and stdout carries protocol frames and
nothing else.

| File | What it does |
| --- | --- |
| `server.py` | The server: tool definitions, request handlers, error-code mapping, stderr-only logging |
| `models.py` | Strict Pydantic input models — the single source of truth for both validation and the advertised JSON Schemas |
| `datastore.py` | Thread-safe in-memory customer store with seed fixtures |
| `stdio_guard.py` | Wraps the SDK read stream so undecodable frames get a real error response instead of being dropped |

**Key functions**

- `models.GetCustomerRecordInput` / `TriggerRefundInput` — the validation rules.
  `strict=True` (no type coercion), `extra="forbid"` (unknown keys rejected),
  `allow_inf_nan=False` (`NaN`/`Infinity` rejected).
- `models.TriggerRefundInput._reason_must_have_substance` — applies the
  10-character minimum to the *trimmed* string, so whitespace-only reasons fail.
- `server.validate_tool_input(tool_name, arguments)` — resolves the tool, runs
  the model, and raises `-32601` for an unknown tool or `-32602` for bad input.
- `server.on_call_tool(ctx, params)` — validates before any side effect, then
  dispatches; maps a missing record to `-32000` and anything unexpected to a
  bare `-32603`.
- `server.configure_logging()` / `_assert_stdout_claimable()` — the two halves
  of the stdout-purity guarantee.
- `stdio_guard.classify(exc)` — distinguishes unparseable JSON (`-32700`) from
  a valid-JSON/invalid-envelope frame (`-32600`).
- `stdio_guard.MalformedFrameReporter` — read-stream wrapper that answers bad
  frames rather than dropping them.
- `datastore.Datastore.record_refund(...)` — lookup, id allocation and append
  under one lock, so concurrent refunds cannot collide.

### Task 2 — MCP security gateway

An authenticating JSON-RPC reverse proxy. `tools/list` passes through
transparently; `tools/call` for an `admin_`-prefixed tool requires the `admin`
role, and an unauthorized call is answered by the gateway with **no downstream
traffic at all**.

| File | What it does |
| --- | --- |
| `gateway.py` | The proxy: parsing, authorization, forwarding, upstream-error sanitisation |
| `tokens.py` | HMAC-signed bearer tokens: issue, verify, header parsing |
| `downstream.py` | Mock MCP server with a call log and failure injection, so "never called" is provable |

**Key functions**

- `tokens.issue(subject, role, ...)` / `verify(token, ...)` — mint and check a
  `base64url(claims).base64url(hmac_sha256)` token. Constant-time signature
  compare, enforced expiry, and roles restricted to exactly `admin`/`viewer`.
- `tokens.parse_authorization_header(header)` — case-insensitive `Bearer`
  scheme per RFC 7235, case-sensitive token.
- `gateway.authorize(message, principal, auth_failure)` — the policy in one
  function. Raises `Denied` with the code and HTTP status to use.
- `gateway._is_privileged(tool_name)` — the exact, case-sensitive
  `startswith("admin_")` check.
- `gateway._forward(app, payload)` — the only place that talks downstream, and
  the only place upstream failures are converted into a fixed `reason` enum.
- `downstream.CALL_LOG` — records everything that actually arrived; the tests
  assert it stays empty for blocked calls.

### Task 3 — Streaming PII redaction guardrail

A streaming LLM gateway that redacts emails, SSNs, and credit cards **in real
time**, correctly when a pattern is split across chunks, without buffering the
response.

| File | What it does |
| --- | --- |
| `redactor.py` | The sliding-buffer state machine — the core of the task |
| `app.py` | FastAPI `StreamingResponse` endpoint and mid-stream failure handling |
| `providers.py` | Mock, SSE-mock, and real Anthropic providers behind one interface |

**Key functions**

- `redactor.StreamRedactor.feed(chunk)` — absorb a chunk, return the text that
  is now safe to emit.
- `redactor.StreamRedactor.close()` — flush the held tail, redacted. Never
  dropped, never emitted raw.
- `redactor.StreamRedactor._settled_limit()` — computes how much is safe to
  emit: the start of the trailing fragment that could still become PII, floored
  by `len(buffer) - max_hold`.
- `redactor._partial_prefix_start(text)` — the adaptive hold-back. Returns
  `len(text)` for ordinary prose, which is why TTFT stays low.
- `redactor._passes_luhn(digits)` — keeps 16-digit order numbers from being
  redacted as cards.
- `redactor.redact_complete(text)` — the non-streaming oracle the streaming
  path is differentially tested against.
- `app.redacted_stream(source, max_hold)` — redacts, then encodes to UTF-8
  (that ordering is what makes multi-byte text safe), and appends one sanitized
  sentinel if the upstream dies.

### Task 4 — Rate limiting and model fallback

A token-aware sliding-window rate limiter persisted in on-disk SQLite, plus
automatic primary→secondary failover, behind one standardized error shape.

| File | What it does |
| --- | --- |
| `rate_limiter.py` | Sliding window log in SQLite, transactional admission |
| `router.py` | Primary/secondary failover policy |
| `providers.py` | Provider interface; normalises every upstream failure at the boundary |
| `errors.py` | The single client-facing error shape |
| `tokens_estimate.py` | Pre-flight token estimation |
| `app.py` | The HTTP gateway wiring it together |
| `fake_upstream.py` | Fake primary/secondary endpoints with latency and failure injection |

**Key functions**

- `rate_limiter.RateLimiter.try_consume(tenant, tokens)` — evict, sum, decide
  and record inside one `BEGIN IMMEDIATE` transaction. Allows when
  `used + tokens <= limit`, so exactly 50,000 passes and 50,001 does not.
- `rate_limiter.RateLimiter.reconcile(id, actual)` / `release(id)` — correct a
  reservation once real usage is known, or hand the whole budget back.
- `rate_limiter.RateLimiter._retry_after(...)` — walks the tenant's rows
  oldest-first to say when enough usage ages out.
- `router.ModelRouter.complete(...)` — the failover policy; returns the
  completion plus the attempt trail.
- `providers.HttpModelProvider.complete(...)` — the boundary where an httpx
  exception or upstream status becomes `ProviderTimeout` / `ProviderRateLimited`
  / `ProviderUnavailable` / `ProviderRejected`.
- `errors.GatewayError.to_payload()` — the one serialised shape. Its
  `internal_detail` field has no path to the wire.
- `tokens_estimate.estimate_tokens(prompt, max_tokens)` —
  `ceil(len(prompt)/4) + max_tokens`, deliberately erring high.

---

## Requirement traceability

Where each requirement from the assessment brief is implemented and tested.

### Task 1

| Requirement | Implementation | Tests |
| --- | --- | --- |
| `get_customer_record(customer_id)` | `server.TOOLS`, `models.GetCustomerRecordInput` | `test_get_customer_record_happy_path` |
| `trigger_refund(customer_id, amount, reason)` | `models.TriggerRefundInput` | `test_trigger_refund_happy_path` |
| Invalid `customer_id` format | `CUSTOMER_ID_PATTERN` | `test_get_customer_record_rejects_malformed_ids` (9 cases) |
| Malformed amount (negative, non-numeric) | `Field(gt=0)`, `strict=True` | `test_trigger_refund_rejects_non_positive_amount`, `..._wrong_amount_types`, `..._nan_and_infinity` |
| Reason below 10 chars | `min_length` + trim validator | `test_reason_boundary_nine_chars_fails_ten_passes`, `test_whitespace_only_reason_is_rejected` |
| Strict schema validation (Pydantic) | `models.StrictModel` | whole `test_tools.py` |
| stdout reserved for JSON-RPC | `configure_logging`, `_assert_stdout_claimable`, fd-1 claim | `test_stdout_contains_only_jsonrpc_under_mixed_traffic` (30 requests + garbage) |
| Logs exclusively to stderr | stderr-pinned root logger | `test_logging_goes_to_stderr_not_stdout`, `test_startup_banner_never_reaches_stdout` |
| Malformed JSON-RPC payloads | `stdio_guard.MalformedFrameReporter` | `test_invalid_json_gets_parse_error_and_server_survives`, `test_missing_jsonrpc_field_is_invalid_request`, `test_structurally_invalid_frames_...` |
| Standard MCP error codes | `server` error-code table | `test_unknown_tool_is_method_not_found`, `test_unknown_customer_is_a_distinct_error_...` |

### Task 2

| Requirement | Implementation | Tests |
| --- | --- | --- |
| Extract role from Bearer token | `tokens.parse_authorization_header` | `test_tokens.py`, `test_bearer_scheme_is_case_insensitive_per_rfc7235` |
| Missing/malformed Bearer header | `Denied(UNAUTHENTICATED, ..., 401)` | `test_missing_authorization_header`, `test_malformed_authorization_headers_...` (8 cases) |
| Role extraction failures | fail-closed role check in `tokens.verify` | `test_unexpected_role_claims_fail_closed` (7 cases), `test_token_with_no_role_field_fails_closed` |
| `tools/list` passthrough | `UNAUTHENTICATED_METHODS` | `test_tools_list_is_forwarded_transparently`, `..._with_no_token_at_all`, `..._with_a_garbage_token` |
| `tools/call` inspects `params.name` | `gateway.authorize` | `test_tools_call_with_no_name_does_not_crash` |
| `admin_` denied to non-admin, no downstream call | `_is_privileged` + local response | `test_viewer_calling_admin_tool_is_blocked_and_downstream_never_called` (asserts call count 0) |
| JSON-RPC `-32001 Unauthorized Tool Call` | `UNAUTHORIZED_TOOL_CALL` | same test, plus `test_every_admin_tool_is_blocked_for_viewers` |

### Task 3

| Requirement | Implementation | Tests |
| --- | --- | --- |
| Real-time chunk interception | `app.redacted_stream` + `StreamingResponse` | `test_split_pii_is_redacted_over_http` |
| Emails / SSNs / cards detected | `redactor.EMAIL`, `SSN`, `CARD` | `test_email_formats`, `test_ssn_formats`, `test_card_formats` |
| Replace with `[REDACTED]` | `redactor.REPLACEMENT` | `test_all_three_types_in_one_chunk` |
| **PII split across chunks** | `_partial_prefix_start`, `_settled_limit` | `test_every_split_point_of_every_secret` (every position of every secret), `test_secret_streamed_one_character_at_a_time`, `test_fixed_size_splits_match_the_oracle` |
| Partial token sequences mid-stream | held tail + `close()` | `test_stream_ending_mid_buffer_with_pii_in_the_tail`, `..._flushes_it_unmodified` |
| Minimise TTFT | adaptive hold-back | `test_first_chunk_arrives_before_the_stream_finishes`, `test_ttft_is_not_delayed_by_the_hold_back_window` |
| No full-response buffering | bounded buffer | `test_buffer_stays_bounded_over_a_long_stream`, `test_process_memory_does_not_grow_with_stream_length` (tracemalloc), `test_buffer_bounded_even_under_an_adversarial_digit_flood` |

### Task 4

| Requirement | Implementation | Tests |
| --- | --- | --- |
| Token tracking per tenant API key | `RateLimiter.try_consume` | `test_requests_under_the_budget_all_succeed` |
| 50,000 tokens/minute threshold | `DEFAULT_LIMIT_TOKENS` | `test_exactly_the_limit_is_allowed_and_one_more_is_not`, `test_a_single_request_of_50001_is_rejected` |
| Sliding window + state eviction | window log + `DELETE` | `test_old_usage_is_evicted_when_the_window_slides`, `test_window_is_sliding_not_fixed`, `test_expired_rows_are_actually_deleted_not_just_ignored` |
| On-disk SQLite | `sqlite3` + WAL | `test_state_survives_a_restart`, `test_state_survives_a_real_subprocess_restart` |
| Primary 429 → failover | `FAILOVER_ON` | `test_primary_429_triggers_failover` |
| Primary timeout (3000 ms) → failover | provider-level deadline | `test_primary_timeout_triggers_failover`, `test_default_timeout_is_3000ms`, `test_timeout_fires_on_the_correct_side_of_the_threshold` |
| Standardized error payload | `errors.GatewayError` | `test_every_error_path_shares_one_shape` |
| No raw traces / internal details leak | normalisation at provider boundary | `test_no_upstream_detail_leaks_from_either_provider`, `test_both_down_gives_one_sanitized_error`, `test_unexpected_internal_exception_is_sanitized` |
| Async concurrency / race conditions | `BEGIN IMMEDIATE` | `test_concurrent_requests_never_exceed_the_limit` (20 threads), `test_independent_limiter_instances_share_one_budget` |

---

## Performance and production readiness

Every change below was driven by a profile or a measurement, and every one is
guarded by a test that fails if it is reverted. Reproduce the numbers with
`./bench/run_all.sh`.

### Measured improvements

| What | Before | After |
| --- | --- | --- |
| Redaction, digit-heavy content | 0.43 MB/s | **5.24 MB/s** |
| Redaction, fine-grained chunks | 0.01 MB/s | **0.31 MB/s** |
| Rate limiter admissions | 7,080 ops/s | **18,351 ops/s** |
| Gateway throughput (concurrency 64) | 647 req/s | **1,956 req/s** |
| Gateway p50 / p99 latency | 95 / 152 ms | **17 / 42 ms** |

**Task 3 — a quadratic regex.** `find_matches` was 94% of stream time, and the
culprit was the *email* pattern, not the card pattern: its local-part class
includes digits, so on a long digit run the engine consumed the whole run at
every start position then backtracked one character at a time hunting an `@`
that was not there. Fixed with a content gate that selects the narrowest pattern
that can still match, which is provably result-preserving.

**Task 4 — blocking I/O on the event loop.** Admission ran SQLite inside a
coroutine, so one commit spike (28 ms observed) stalled every in-flight request.
`asyncio.to_thread` per call measured *worse* — the hop costs ~260 µs against
54 µs of database work — so the limiter uses **group commit** instead: concurrent
admissions are batched into one transaction and one thread hop, and throughput
improves with concurrency rather than collapsing.

### Hardening

- **Resource limits.** Request body cap (1 MiB) enforced against both
  `Content-Length` *and* the streamed read, batch-size cap (100), prompt caps —
  all rejections, never truncations.
- **Bounded connection pools** on every outbound client, so a burst queues
  instead of exhausting file descriptors.
- **Liveness vs readiness** as separate probes. Liveness is dependency-free so a
  downstream blip cannot trigger restarts; readiness checks the database and
  returns 503 to leave rotation.
- **Graceful shutdown** drains in-flight admission work.
- **Correlation ids** on every service. A streamed response commits its status
  with the first byte, so the request id is the only thing a user can quote when
  a stream fails — it is echoed in the header and stamped on the failure log.

### How performance is tested

Absolute throughput is *measured* in `bench/` and never asserted, because a noisy
CI box is not a regression. What the suites assert is algorithmic behaviour that
holds on any machine:

- work scales linearly, not quadratically, with input size;
- buffers stay bounded regardless of stream length (invariant *and* `tracemalloc`);
- the event loop keeps ticking through a 300 ms database stall;
- batching actually occurs, and never changes an admission decision.

Two optimisations touch correctness-critical code, so both are proved equivalent
to the unoptimised path against randomised oracles: **14,000+** inputs for the
redaction patterns (with an independent Luhn implementation, so oracle and
subject share no code), and **500** randomised batches for admission. Reverting
the redaction optimisation fails 7 of its 10 property tests, including a measured
179x slowdown against an 8x threshold.

---

## Documentation standard

`tools/check_docs.py` enforces it, and `./run_tests.sh` runs it:

- **Source code: 100% required.** Every module, class, function, method and
  property. A gap fails the check.
- **Test support: 100% required.** Fixtures and helpers describe plumbing, not
  behaviour, so their names are not self-explanatory.
- **Test functions: reported, not required.** A well-named test is its own
  specification — `test_exactly_the_limit_is_allowed_and_one_more_is_not` needs
  no prose, and a docstring restating the function name is noise. Docstrings
  are present on the ~37 tests whose intent is not obvious from the name.

Comments throughout explain *why*, not *what*: the reasoning behind a
tradeoff, the attack a check defends against, the failure mode a line prevents.

---

## Why one process per project

Do not run `pytest` across more than one project at a time. These are four
independent projects, and two of them legitimately define a top-level `app.py`
and a top-level `providers.py`. Those names are correct within each project and
irreconcilable across them: Python's `sys.modules` is global, so a single
pytest process binds whichever copy it imported first.

The dangerous part is that this does not reliably error. `from app import
create_app` resolves in both projects, so a combined run can *silently* test
Task 4's gateway against Task 3's application module and report a pass.

A multi-project invocation is therefore refused outright:

```
$ pytest                       # from the repository root
ERROR:
Refusing to collect more than one project in a single pytest process.
...
    /path/to/fde-assessment/run_all_tests.sh
```

Single-project runs are untouched and work from anywhere — `cd task3-… &&
pytest`, `pytest fde-assessment/task2-security-gateway`, a single file, or a
single node id.

---

## Cross-cutting decisions

Each is documented in the relevant task README:

- **Fail closed everywhere.** Unknown role claims, absent tool names, malformed
  tokens, extra tool arguments, and unparseable frames are all rejections,
  never permissive defaults.
- **Case sensitivity is explicit.** Customer-id prefixes, tool names, and the
  `admin_` prefix are matched case-sensitively, with the reasoning and
  near-miss tests written down in each case.
- **Large inputs are rejected, never truncated.** Oversized refund amounts and
  reason strings get a clean `-32602` rather than being silently clipped.
- **Unicode is handled at the right layer.** Task 3 redacts on `str` and
  encodes to UTF-8 afterwards, so a wire chunk boundary can never fall inside a
  multi-byte character; Task 1 validates `reason` by character count.
- **No internal detail reaches a client.** Tasks 2, 3 and 4 each plant a stack
  trace, an internal hostname and a credential fragment in an upstream failure,
  and assert none of it appears in the response.
