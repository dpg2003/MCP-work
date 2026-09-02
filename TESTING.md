# Testing this yourself

Everything below needs **VS Code and nothing else**. No local Python, no manual
dependency install, no API keys, no network access.

---

## The quickest path: open in a Dev Container

1. Install the **Dev Containers** extension (`ms-vscode-remote.remote-containers`).
2. Open this folder in VS Code.
3. When prompted, choose **Reopen in Container** (or Command Palette →
   *Dev Containers: Reopen in Container*).

The container builds Python 3.11 and runs `./setup.sh` for you. When it
finishes, everything below works.

> **Not using containers?** You need Python 3.11+ on your machine; run
> `./setup.sh` once in a terminal. Everything else is identical.

---

## Run the tests

**Command Palette → Tasks: Run Task**, then pick one:

| Task | What it does |
| --- | --- |
| **Tests: ALL** | Doc check + config check + all four suites (~2 min) |
| Tests: Task 1 — MCP server | 54 tests |
| Tests: Task 2 — security gateway | 84 tests |
| Tests: Task 3 — PII redaction | 253 tests |
| Tests: Task 4 — rate limit router | 100 tests |
| Benchmarks: run all | Prints throughput and latency numbers |
| Docs: coverage check | Enforces 100% documentation on source |
| **Assessment: run the 54-case matrix** | Runs every case from the assessment test-case document and writes `ASSESSMENT_RESULTS.md` |

*Tests: ALL* is the default test task, so <kbd>Ctrl/Cmd</kbd>+<kbd>Shift</kbd>+<kbd>P</kbd>
→ *Tasks: Run Test Task* runs it directly.

**Testing panel.** VS Code's test explorer runs one project at a time (the four
cannot share a pytest process — see the root README). It is pointed at Task 4 by
default; change `python.testing.cwd` in `.vscode/settings.json` to explore
another, or just use the tasks above, which run each in its own process.

---

## Reproduce the assessment scorecard

**Tasks: Run Task → Assessment: run the 54-case matrix**, or:

```bash
python tools/assessment/run_matrix.py
```

This executes all 54 cases from the assessment test-case document against the
real implementations — the MCP server as a subprocess over a real pipe, the HTTP
services on real sockets, the timeout cases with real wall-clock latency — and
writes the filled-in table to [`ASSESSMENT_RESULTS.md`](ASSESSMENT_RESULTS.md).
It exits non-zero if any case fails, so it is safe to run in CI.

Current result: **54 / 54 passed**.

## See it actually work

**Tasks: Run Task → Demo: end-to-end walkthrough of all four.**

One command starts every service on its own port, exercises the behaviour each
task is assessed on, prints the result, and shuts everything down. Nothing is
left running. You should see:

- **Task 1** — a valid call, a `-32602` on a malformed id, a `-32000` on an
  unknown customer, and a `-32700` on a garbage frame; every stdout line valid
  JSON-RPC, with the log line on stderr.
- **Task 2** — a viewer blocked from `admin_reset_key` with `-32001`, an admin
  allowed through, and the downstream call log proving the blocked call never
  reached it.
- **Task 3** — an email, SSN and card redacted mid-stream even though the mock
  upstream splits each across chunk boundaries, with the ticket number, IP
  address and version string preserved.
- **Task 4** — a healthy call, a 429 triggering failover, both providers down
  yielding one sanitized error, and a token budget being exhausted into a clean
  429.

---

## Run and debug the services

**Run and Debug** (<kbd>Ctrl/Cmd</kbd>+<kbd>Shift</kbd>+<kbd>D</kbd>) has a
launch configuration per service, with breakpoints working throughout:

- Task 1: MCP server (stdio)
- Task 2: gateway + downstream *(compound — starts both)*
- Task 3: PII redaction gateway
- Task 4: router + providers *(compound — starts both)*

Or start them without the debugger from **Tasks: Run Task → Run: …**. Ports are
forwarded and labelled in the Ports view.

---

## Try the MCP server from an MCP client

The Task 1 server speaks MCP over stdio, so any MCP client can drive it.

### VS Code's built-in MCP client

`.vscode/mcp.json` is already configured. Command Palette → **MCP: List
Servers** → `fde-customer-ops` → **Start**. Then use it from Copilot Chat's
agent mode. Try:

- `get_customer_record` with `CUST-A1B2C` → a record
- `get_customer_record` with `cust-abcde` → rejected, `-32602`
- `get_customer_record` with `CUST-ZZZZZ` → `-32000`, distinct from validation
- `trigger_refund` with `amount: 0` or a 9-character `reason` → rejected

### MCP Inspector (a UI, no client needed)

**Tasks: Run Task → MCP Inspector: open Task 1 server.** Needs Node available
for `npx`; it opens a browser UI where you can list and call the tools directly.

### Claude Desktop

Copy the `mcpServers` block from `examples/claude_desktop_config.json` into your
own `claude_desktop_config.json`, replacing `ABSOLUTE_PATH_TO_REPO` with the full
path to this checkout, then restart Claude Desktop.

---

## Poke at the services by hand

With the run tasks started:

```bash
# Task 2 - mint a token, then get blocked
TOKEN=$(cd fde-assessment/task2-security-gateway && \
        GATEWAY_TOKEN_SECRET=dev-secret-change-me .venv/bin/python tokens.py viewer)
curl -sX POST localhost:9000/mcp -H "Authorization: Bearer $TOKEN" \
     -H 'content-type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key"}}'

# Task 3 - watch PII get redacted as it streams
curl -sN -X POST localhost:8000/v1/generate \
     -H 'content-type: application/json' -d '{"prompt":"summarise the customer"}'

# Task 4 - force a failover
curl -sX POST localhost:9100/_control/primary \
     -H 'content-type: application/json' -d '{"mode":"429"}'
curl -sX POST localhost:8080/v1/complete -H 'content-type: application/json' \
     -H 'X-API-Key: key-acme' -d '{"prompt":"hello","max_tokens":100}'
```

The fake providers expose `/_control/{primary,secondary}` with modes `ok`,
`429`, `500`, `400`, `garbage`, and `hang`, plus a `latency_seconds`, so you can
reproduce any failure path by hand.

---

## If something does not work

| Symptom | Cause |
| --- | --- |
| `no virtualenv at … run ./setup.sh first` | Setup has not run. Run the **Setup** task. |
| `Refusing to collect more than one project` | You ran `pytest` at the repo root. That is deliberate — use a per-project task; the root README explains why. |
| A port is already in use | A previous run task is still going. Kill it in the Terminal panel. |
| MCP server shows no tools | Check the interpreter path in `.vscode/mcp.json` resolves — it assumes `./setup.sh` has run. |
