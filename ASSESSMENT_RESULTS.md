# FDE Assessment — Measured Test Results

Every case from the assessment test-case document, executed against the
implementation and filled in automatically.

- **Generated:** 2026-09-02 18:09:40 UTC
- **Regenerate:** `python tools/assessment/run_matrix.py`

Nothing here is asserted by hand. The MCP server is driven as a real
subprocess over a real pipe; the HTTP services run on real sockets; the
timeout cases use real wall-clock latency against the documented 3000 ms
threshold. Rows marked `n/a` are informational rather than pass/fail.

---

## Task 1: Custom MCP Server (Strict Validation & Transport Handling)

| # | Test Case | Measured Result | Pass/Fail |
|---|---|---|---|
| 1.1 | Valid customer_id format | OK — record for CUST-12345 (Alan Turing) | **Pass** |
| 1.2 | Invalid customer_id (no prefix) | `-32602` Invalid params | **Pass** |
| 1.3 | Invalid customer_id (lowercase prefix) | `-32602` Invalid params | **Pass** |
| 1.4 | Invalid customer_id (wrong length) | `-32602` Invalid params | **Pass** |
| 1.5 | Missing customer_id | `-32602` Invalid params | **Pass** |
| 1.6 | Valid refund request | OK — refund RF-000001, amount 25.5 | **Pass** |
| 1.7 | Negative refund amount | `-32602` Invalid params — Input should be greater than 0 | **Pass** |
| 1.8 | Zero refund amount | `-32602` Invalid params — Input should be greater than 0 | **Pass** |
| 1.9 | Non-numeric amount | `-32602` Invalid params — Input should be a valid number | **Pass** |
| 1.10 | Reason too short (5 chars) | `-32602` Invalid params | **Pass** |
| 1.11 | Reason exactly 10 chars (boundary) | OK — refund RF-000002, amount 10.0 | **Pass** |
| 1.12 | Missing reason field | `-32602` Invalid params | **Pass** |
| 1.13 | Unknown tool call | `-32601` Unknown tool | **Pass** |
| 1.14 | Stdout purity | 36 stdout lines captured, all valid JSON-RPC 2.0; 0 stray lines | **Pass** |
| 1.15 | Stderr logging | 36 log lines on stderr, 0 on stdout; e.g. `2026-09-02 18:09:41,439 INFO fde.mcp.server: fde-customer-op…` | **Pass** |
| 1.16 | Transport handshake | `initialize` OK (protocol 2025-06-18); `tools/list` returned ['get_customer_record', 'trigger_refund'] | **Pass** |

---

## Task 2: MCP Security Gateway Proxy (Tool Filtering & Auth)

| # | Test Case | Measured Result | Pass/Fail |
|---|---|---|---|
| 2.1 | tools/list passthrough | HTTP 200, forwarded — 4 tools listed | **Pass** |
| 2.2 | tools/list with no token | HTTP 200, forwarded — 4 tools listed (documented: unauthenticated) | **Pass** |
| 2.3 | Admin tool call, admin role | HTTP 200, forwarded — admin_reset_key executed | **Pass** |
| 2.4 | Admin tool call, viewer role | HTTP 200, `-32001` Unauthorized Tool Call | **Pass** |
| 2.5 | Admin tool call, no token | HTTP 401, `-32002` Unauthorized (`-32002` = unauthenticated, distinct from `-32001` unauthorized) | **Pass** |
| 2.6 | Admin tool call, malformed token | HTTP 401, `-32002` Unauthorized (no crash) | **Pass** |
| 2.7 | Non-admin tool call, viewer role | HTTP 200, forwarded — get_weather executed | **Pass** |
| 2.8 | Non-admin tool call, admin role | HTTP 200, forwarded — get_weather executed | **Pass** |
| 2.9 | Tool name edge case — `administrator_tool` | HTTP 200, `-32601` Unknown tool: administrator_tool; forwarded downstream: True. Strict `admin_` prefix, so this is NOT treated as privileged; downstream rejects it as an unknown tool. | **Pass** |
| 2.10 | Downstream bypassed on rejection | downstream request count before=3, after=3 (delta 0) across cases 2.4/2.5/2.6 | **Pass** |
| 2.11 | Malformed JSON-RPC payload | HTTP 400, `-32700` Parse error; forwarded downstream: False | **Pass** |
| 2.12 | Unknown method | HTTP 200, `-32601` Method not found: tools/unknown_method; forwarded downstream: True. Documented: only `tools/list` and `tools/call` are inspected; anything else is authenticated then forwarded, so downstream owns the vocabulary. | **Pass** |

---

## Task 3: LLM Gateway Streaming Guardrail (PII Redaction)

| # | Test Case | Measured Result | Pass/Fail |
|---|---|---|---|
| 3.1 | Email in single chunk | `Contact me at [REDACTED] please` | **Pass** |
| 3.2 | SSN in single chunk | `SSN: [REDACTED]` | **Pass** |
| 3.3 | Credit card in single chunk | dashed → `[REDACTED]`; bare → `[REDACTED]` | **Pass** |
| 3.4 | PII split across two chunks | `my email is [REDACTED] thanks` | **Pass** |
| 3.5 | PII split mid-token (SSN) | `SSN [REDACTED] is mine` | **Pass** |
| 3.6 | Multiple PII types across chunks | `Reach [REDACTED], ssn [REDACTED], card [REDACTED]. Ticket 123456789 stays.` | **Pass** |
| 3.7 | No PII present | unchanged: True | **Pass** |
| 3.8 | False-positive check | `Order number 123-45-6789X` → unchanged; `call 555-123-4567` → unchanged; `host 192.168.1.1` → unchanged; `order 1234567812345678` → unchanged; `product code 123456789` → unchanged | **Pass** |
| 3.9 | Memory — no full buffering | 2.8 MB streamed; retained growth 1.7 KiB; peak buffer 81 chars (cap 256 + one chunk) | **Pass** |
| 3.10 | Time to First Token | TTFT 93 ms of 548 ms total (upstream emits a chunk every 50 ms, so ~one chunk of delay, not the whole stream) | **Pass** |
| 3.11 | Stream remains chunked | 8 separate chunks delivered, first at 93 ms, last at 548 ms | **Pass** |
| 3.12 | Redaction at end-of-stream buffer flush | PII in held tail → `Here it is: [REDACTED]`; clean tail → `Nothing sensitive in this tail` (nothing dropped) | **Pass** |

---

## Task 4: Rate-Limiting & Model Fallback Router

| # | Test Case | Measured Result | Pass/Fail |
|---|---|---|---|
| 4.1 | Under rate limit | limiter: 4 x 10,000 tokens → allowed=[True, True, True, True], window usage 40000. End to end: HTTP 200, provider=primary, failed_over=False | **Pass** |
| 4.2 | At rate limit boundary | request landing on exactly 50,000 → allowed=True (usage now 50000); one more token → allowed=False. Documented: inclusive, `used + tokens <= limit`. | **Pass** |
| 4.3 | Over rate limit | rejected with retry_after=60.0s, used=50000, limit=50000; nothing recorded | **Pass** |
| 4.4 | Sliding window eviction | clock +61 s → usage evicted to 0, new 50,000-token request allowed=True; stored rows now 1 | **Pass** |
| 4.5 | Per-tenant isolation | tenant-a exhausted (usage 50000); tenant-b 50,000-token request allowed=True | **Pass** |
| 4.6 | Primary returns HTTP 429 | HTTP 200, provider=secondary, failed_over=True | **Pass** |
| 4.7 | Primary timeout (>3000 ms) | primary held 5,000 ms → failed over after 3087 ms, provider=secondary | **Pass** |
| 4.8 | Primary responds just under timeout (~2900 ms) | responded in 2981 ms → provider=primary, failed_over=False | **Pass** |
| 4.9 | Primary responds just over timeout (~3100 ms) | cut off at 3079 ms → provider=secondary, failed_over=True | **Pass** |
| 4.10 | Both primary and secondary fail | HTTP 502, single error `upstream_unavailable`, attempts=['primary:rate_limited', 'secondary:unavailable'] | **Pass** |
| 4.11 | Error payload sanitization | upstream body contained a stack trace, an API key fragment and an internal hostname; leaked into the response: none. Client sees only `{"type": "upstream_unavailable", "message": "No model provider was able to serve…` | **Pass** |
| 4.12 | Concurrent requests race condition | 32 simultaneous 5,000-token requests → exactly 10 admitted, final usage 50000 (limit 50,000). Serialised by `BEGIN IMMEDIATE`, so check-then-write cannot interleave. | **Pass** |
| 4.13 | SQLite persistence across restart | process 1: 45,000 allowed (usage 45000); process 2 (fresh interpreter): 10,000 → allowed=False; process 3: 5,000 → allowed=True (usage 50000). Not reset to zero. | **Pass** |
| 4.14 | SQLite concurrent writes | same 32-thread burst completed with no SQLITE_BUSY or corruption; journal_mode=wal, busy_timeout=30000 ms | **Pass** |

---

## Summary Scorecard

| Task | Total Cases | Passed | Failed |
|---|---|---|---|
| Task 1 | 16 | 16 | 0 |
| Task 2 | 12 | 12 | 0 |
| Task 3 | 12 | 12 | 0 |
| Task 4 | 14 | 14 | 0 |
| **Total** | **54** | **54** | **0** |
