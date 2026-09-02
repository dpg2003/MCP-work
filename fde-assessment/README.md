# fde-assessment

The four assessment projects. **Start at the [repository root README](../README.md)** —
it carries the quick start, the per-file/per-function reference, and the
requirement traceability tables.

| | Project | What it is | Tests |
| --- | --- | --- | --- |
| 1 | [`task1-mcp-server`](task1-mcp-server/) | MCP server over stdio with strict Pydantic validation and guaranteed stdout purity | 54 |
| 2 | [`task2-security-gateway`](task2-security-gateway/) | Authenticating JSON-RPC reverse proxy with role-based `admin_*` tool filtering | 72 |
| 3 | [`task3-pii-redaction-gateway`](task3-pii-redaction-gateway/) | Streaming LLM gateway redacting PII across chunk boundaries | 170 |
| 4 | [`task4-rate-limit-router`](task4-rate-limit-router/) | Token-aware sliding-window rate limiter (SQLite) with model failover | 67 |

Each project's own README documents how to run it, how to test it, its design
decisions, and its key tradeoff.

```bash
../setup.sh && ../run_tests.sh     # everything

cd task1-mcp-server && pytest      # one project
```

`run_all_tests.sh` in this folder runs each suite in its own process; see the
root README's *Why one process per project* for why that matters.
