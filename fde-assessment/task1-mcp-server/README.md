# Task 1 — MCP Server with Strict Validation & Transport Handling

An MCP server speaking **stdio** that exposes two tools, `get_customer_record`
and `trigger_refund`, with strict Pydantic validation on every input and
guaranteed stdout purity.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python server.py          # serves MCP over stdin/stdout
```

The process speaks newline-delimited JSON-RPC on stdin/stdout and logs to
stderr. Point any MCP client at it, e.g.:

```json
{ "mcpServers": { "fde-customer-ops": { "command": "python", "args": ["server.py"] } } }
```

## Run the tests

```bash
pip install -r requirements.txt
pytest
```

The suite spawns `server.py` as a real subprocess and speaks raw JSON-RPC over
the pipe — no in-process shortcuts — so every assertion is about bytes that
actually crossed the transport.

## Tools

| Tool | Inputs | Rules |
| --- | --- | --- |
| `get_customer_record` | `customer_id` | `^CUST-[A-Za-z0-9]{5}$` |
| `trigger_refund` | `customer_id`, `amount`, `reason` | id as above; `0 < amount <= 1_000_000` and finite; `reason` 10–2000 chars **after trimming** |

## Error codes

| Condition | Code |
| --- | --- |
| Frame is not valid JSON | `-32700` Parse error |
| Valid JSON, invalid JSON-RPC envelope | `-32600` Invalid Request |
| Unknown method / unknown tool name | `-32601` Method not found |
| Tool input fails validation | `-32602` Invalid params |
| Well-formed id with no record behind it | `-32000` Customer not found |
| Unexpected internal failure | `-32603` Internal error |

`-32000` sits in the JSON-RPC implementation-defined server-error range. Keeping
"customer does not exist" off `-32602` lets a client tell *"your request was
garbage, retrying is pointless"* apart from *"that customer isn't here, another
one might be"*.

## How stdout purity is guaranteed

Two independent mechanisms, because this is a hard pass/fail criterion:

1. **Logging.** `configure_logging()` installs a single
   `StreamHandler(sys.stderr)` on the *root* logger and clears every other
   handler, so neither this code nor any dependency can log to stdout.
2. **File-descriptor claim.** `mcp.server.stdio.stdio_server()` duplicates the
   real stdout pipe onto a private descriptor that only the transport writes to,
   then points fd 1 itself at stderr for the lifetime of the server. A stray
   `print()` anywhere in the process — including from a third-party library —
   lands on stderr and physically cannot interleave with a protocol frame.
   `_assert_stdout_claimable()` verifies the precondition for that mechanism at
   startup and refuses to serve if it does not hold, rather than starting up
   into a silently unprotected state.

`tests/test_transport.py` fires 30 mixed valid/invalid requests plus raw garbage
frames and asserts that *every* stdout line parses as a well-formed JSON-RPC
object.

## Handling malformed frames

The SDK's stdio transport pushes a decode failure onto the read stream as an
exception, and the default dispatcher logs and drops it — a client that sent one
bad line would wait forever. `stdio_guard.MalformedFrameReporter` wraps the read
stream and converts those into real `-32700` / `-32600` responses, echoing the
request `id` when it can be recovered from the offending payload and `null`
otherwise.

## Documented decisions

- **Case sensitivity.** The `CUST-` prefix and tool names are matched
  case-sensitively. `cust-abcde` and `Get_Customer_Record` are rejected. Customer
  ids are datastore keys; accepting several spellings of one key is how you end
  up with two records for one customer.
- **Whitespace-only `reason`.** Rejected. The 10-character minimum is applied to
  the *trimmed* string, and the trimmed value is what gets stored. The minimum
  exists to force an auditable justification, and whitespace carries none.
- **Extra arguments.** `extra="forbid"`. An unexpected key is a `-32602`, not a
  silent drop — fail closed.
- **Type coercion.** `strict=True`. `"12.50"` is not a float and `true` is not a
  number. `allow_inf_nan=False` rejects `NaN`/`Infinity`, which Python's `json`
  module will happily parse off the wire.
- **Large inputs.** `amount` above 1,000,000 and `reason` above 2000 characters
  are *rejected* with `-32602`. Nothing is ever silently truncated.
- **Unicode.** `reason` is validated by character count, not bytes, so emoji and
  CJK text behave predictably and round-trip unchanged.

## Key design tradeoff

I used the low-level `Server` API rather than the `FastMCP` decorator style
because FastMCP converts tool failures into `isError` *results*, and the
assessment requires validation failures to surface as JSON-RPC `-32602`
*errors*. Validating explicitly in `on_call_tool` and raising `MCPError` costs a
few lines of dispatch boilerplate but buys exact control over the wire error
code. The second tradeoff is wrapping the SDK's read stream instead of writing a
bespoke stdio transport: I keep the SDK's battle-tested fd-claim logic (which is
what actually delivers stdout purity) and only add the ~40 lines of
malformed-frame reporting the SDK deliberately leaves out.
