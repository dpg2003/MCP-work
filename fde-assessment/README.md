# fde-assessment

The four assessment projects. **Start at the [repository root README](../README.md)** —
it carries the quick start, the per-file/per-function reference, and the
requirement traceability tables.

| | Project | What it is | Tests |
| --- | --- | --- | --- |
| 1 | [`task1-mcp-server`](task1-mcp-server/) | MCP server over stdio with strict Pydantic validation and guaranteed stdout purity | 54 |
| 2 | [`task2-security-gateway`](task2-security-gateway/) | Authenticating JSON-RPC reverse proxy with role-based `admin_*` tool filtering | 105 |
| 3 | [`task3-pii-redaction-gateway`](task3-pii-redaction-gateway/) | Streaming LLM gateway redacting PII across chunk boundaries | 319 |
| 4 | [`task4-rate-limit-router`](task4-rate-limit-router/) | Token-aware sliding-window rate limiter (SQLite) with model failover | 209 |

Each project's own README documents how to run it, how to test it, its design
decisions, and its key tradeoff.

From this folder:

```bash
../setup.sh && ../run_tests.sh     # everything (setup.sh needs network access for PyPI)

source ../.venv/bin/activate       # then, for one project:
cd task1-mcp-server && pytest
```

`run_all_tests.sh` in this folder runs each suite in its own process; see the
root README's *Why one process per project* for why that matters. It uses
`python` from your `PATH` (override with `PYTHON=`), so activate the virtualenv
first or run `../run_tests.sh`, which passes the virtualenv's interpreter.
