# FDE Assessment — MCP & LLM Gateways

Four runnable projects, each self-contained with its own `requirements.txt`,
pytest suite, and README.

| | Project | What it is | Tests |
| --- | --- | --- | --- |
| 1 | [`task1-mcp-server`](task1-mcp-server/) | MCP server over stdio with strict Pydantic validation and guaranteed stdout purity | 54 |
| 2 | [`task2-security-gateway`](task2-security-gateway/) | Authenticating JSON-RPC reverse proxy with role-based `admin_*` tool filtering | 72 |
| 3 | [`task3-pii-redaction-gateway`](task3-pii-redaction-gateway/) | Streaming LLM gateway redacting PII across chunk boundaries | 170 |
| 4 | [`task4-rate-limit-router`](task4-rate-limit-router/) | Token-aware sliding-window rate limiter (SQLite) with model failover | 67 |

363 tests total, none requiring network access or an API key.

## Run everything

```bash
python -m venv .venv && source .venv/bin/activate
for task in fde-assessment/task*/; do
  pip install -q -r "$task/requirements.txt"
  (cd "$task" && pytest)
done
```

Each task's README documents its single run command, its test command, and the
key design tradeoff behind it.

## The hard pass/fail criteria

- **Task 1 — stdout purity.** Enforced twice over: a stderr-pinned root logger,
  and the transport's fd-1 claim (which points fd 1 at stderr so a stray
  `print()` anywhere in the process physically cannot interleave with a protocol
  frame). A test fires 30 mixed valid/invalid requests plus raw garbage frames
  and asserts every stdout line parses as well-formed JSON-RPC.
- **Task 3 — correctness across chunk boundaries.** An adaptive hold-back keeps
  only the suffix that could still grow into a match. Tests split every secret
  at every position, stream each one character at a time, and diff 500 random
  chunkings against a whole-string oracle.

## Cross-cutting decisions

Every one of these is documented in the relevant task README:

- **Fail closed everywhere.** Unknown role claims, absent tool names, malformed
  tokens, extra tool arguments, and unparseable frames are all rejections, never
  permissive defaults.
- **Case sensitivity is explicit.** Customer-id prefixes, tool names, and the
  `admin_` prefix are all matched case-sensitively, with the reasoning (and the
  near-miss tests) written down in each case.
- **Large inputs are rejected, never truncated.** Oversized refund amounts and
  reason strings get a clean `-32602` rather than being silently clipped.
- **Unicode is handled at the right layer.** Task 3 redacts on `str` and encodes
  to UTF-8 afterwards, so a wire chunk boundary can never fall inside a
  multi-byte character; Task 1 validates `reason` by character count.
- **No internal detail reaches a client.** Tasks 2, 3 and 4 each plant a stack
  trace, an internal hostname, and a credential fragment in an upstream failure
  and assert none of it appears in the response.
