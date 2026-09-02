# Task 2 — MCP Security Gateway (Tool Filtering & Auth)

An HTTP/JSON-RPC reverse proxy that authenticates callers, enforces
role-based tool filtering, and forwards everything else to a downstream MCP
server. Unauthorized privileged calls are answered by the gateway itself — the
downstream server is never contacted.

## Files

| File | Purpose |
| --- | --- |
| `gateway.py` | The proxy: parsing, authorization, forwarding, upstream-error sanitisation. Entry point. |
| `tokens.py` | HMAC-signed bearer tokens — issue, verify, header parsing. Also a `mint-token` CLI. |
| `downstream.py` | Mock MCP server with a call log and failure injection |
| `tests/conftest.py` | Wires the gateway to the mock downstream over an in-process ASGI transport |
| `tests/test_gateway.py` | Policy, auth failures, malformed payloads, batches, downstream failures |
| `tests/test_tokens.py` | The token scheme itself |

See the [root README](../../README.md) for the per-function reference.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export GATEWAY_TOKEN_SECRET=some-long-random-string

# terminal 1 — the mock downstream MCP server
uvicorn downstream:app --port 9001

# terminal 2 — the gateway
DOWNSTREAM_URL=http://127.0.0.1:9001/mcp uvicorn gateway:app --port 9000
```

Mint tokens with the built-in CLI:

```bash
python tokens.py admin   root@example.com
python tokens.py viewer  reader@example.com
```

Then:

```bash
TOKEN=$(python tokens.py viewer)
curl -sX POST localhost:9000/mcp -H "Authorization: Bearer $TOKEN" \
     -H 'content-type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key"}}'
# {"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"Unauthorized Tool Call",...}}

curl -s localhost:9001/_control/stats
# {"count":0,"tool_calls":[]}   <- the downstream server never saw it
```

## Run the tests

```bash
pip install -r requirements.txt
pytest
```

72 tests. The gateway's httpx client is injectable, so the suite mounts the
downstream FastAPI app straight onto an `ASGITransport` — no ports bound, fully
deterministic, and the downstream call log stays inspectable, which is how
*"the downstream server was never called"* is asserted rather than assumed.
Real network faults (read timeout, connection refused) can't be produced by an
in-process mount, so those are injected with an `httpx.MockTransport`.

## Token scheme, and why

```
base64url(json_claims) "." base64url(hmac_sha256(secret, base64url_claims))
```

JWT-shaped, deliberately **not** JWT:

- The gateway is the only issuer *and* the only verifier, so there is no third
  party that needs to validate with a public key. A symmetric HMAC is the right
  primitive for that topology: no key distribution, no JWKS endpoint, no
  dependency beyond `hmac`.
- Real JWT drags `alg` negotiation along with it, and `alg: none` /
  algorithm-confusion is the single most common JWT vulnerability. This format
  has exactly one algorithm and no header field to lie in, so that entire class
  of attack does not exist.
- Signature comparison uses `hmac.compare_digest`; a byte-by-byte compare leaks
  the signature one byte at a time.
- Migrating to RS256 JWTs later means replacing `tokens.verify()` only —
  everything downstream of it consumes a `Principal`.

Claims: `sub`, `role`, `iat`, `exp`. Expiry is enforced (exclusive bound: a token
is dead *at* `exp`).

## Policy

| Method | Auth |
| --- | --- |
| `tools/list` | Forwarded transparently, token or not |
| `tools/call`, tool name starts with `admin_` | Requires `role == "admin"` |
| `tools/call`, any other tool | Requires a valid token, any known role |
| anything else | Requires a valid token, then forwarded |

## Error codes

| Condition | JSON-RPC | HTTP |
| --- | --- | --- |
| Body is not valid JSON | `-32700` Parse error | 400 |
| Body is not a JSON-RPC request/batch | `-32600` Invalid Request | 400 |
| `tools/call` with missing/non-string `name` | `-32602` Invalid params | 200 |
| Missing / malformed / expired / forged token | `-32002` Unauthorized | 401 |
| Valid token, insufficient role | `-32001` Unauthorized Tool Call | 200 |
| Downstream unreachable, slow, or unparseable | `-32003` Upstream server error | 502 |

Authentication (*who are you*) is an HTTP-layer failure, so it gets a 401 and a
`WWW-Authenticate` header. Authorization (*you're known, but may not do this*)
is an application-layer failure, so it gets a normal 200 carrying a JSON-RPC
error — which is what an MCP client is actually equipped to parse.

## Documented decisions

- **`tools/list` is unauthenticated.** Discovery returns static public metadata;
  refusing it breaks every client's bootstrap without protecting anything.
  Flip `REQUIRE_AUTH_FOR_LIST = True` in `gateway.py` to change this.
- **The `admin_` prefix check is exact and case-sensitive** — a literal
  `name.startswith("admin_")`. So:
  - `notadmin_reset` is **not** privileged. It does not *start with* the prefix;
    a substring check here would be the bug, not the fix.
  - `Admin_Reset` is **not** privileged, and this is safe: the downstream tool
    namespace is itself case-sensitive, so `Admin_Reset` is not the name of any
    real tool and gets a `-32601` from downstream rather than privileged access.
    A case-insensitive check would instead be a footgun — it would silently
    block a legitimately-named tool like `Administrative_notes`.
  - ` admin_reset_key` (leading space) is likewise not the prefix and not a real
    tool. Both are covered by tests.
- **Roles fail closed.** Only the exact strings `admin` and `viewer` are
  accepted. `superadmin`, `ADMIN`, `null`, `1`, and an absent `role` are all
  rejected at token verification — even when the signature is valid. An unknown
  role is never quietly downgraded to "probably a viewer".
- **`params.name` missing.** Guarded explicitly (the `undefined.startsWith`
  case): an absent or non-string name is `-32602`, never a permitted call.
- **Batches are supported**, and each sub-call is authorized independently. The
  token is authenticated once per HTTP request (it is a property of the
  connection), but the `admin_` check runs per message — a batch mixing a
  permitted and a blocked call forwards only the permitted one.
- **Upstream errors are sanitized.** The gateway never includes the downstream
  response body, the exception string, or the target host in what it returns;
  only a fixed `reason` enum (`timeout`, `unreachable`, `bad_status`,
  `invalid_response`). The full detail goes to the gateway's own logs. Tests
  assert that a planted stack trace, password, and internal hostname all fail to
  appear in the client-facing response.

## Production hardening

- **Request body cap** (`GATEWAY_MAX_BODY_BYTES`, default 1 MiB). `Content-Length`
  is rejected up front so an oversized request costs nothing, *and* the streamed
  read is capped independently — a chunked request has no length header and a
  malicious one can understate it. Over the cap returns HTTP 413 with a JSON-RPC
  error, before authentication and without touching the downstream server.
- **Batch size cap** (`GATEWAY_MAX_BATCH_SIZE`, default 100). Authorization is
  per message, so an unbounded batch is unbounded work for a single request.
- **Bounded downstream pool** (`GATEWAY_MAX_CONNECTIONS` / `GATEWAY_MAX_KEEPALIVE`,
  default 100/20), so a burst surfaces as queuing rather than exhausting file
  descriptors.

`tests/test_hardening.py` covers each cap at, just under, and just over the
limit, and asserts the downstream call count stays at zero for every rejection.

## Key design tradeoff

I authorize on the *parsed JSON-RPC envelope* rather than on the raw body, and I
forward only the messages that pass. That costs a full parse/re-serialize of
every request — the gateway can't be a transparent byte pipe — but it's the only
way to authorize each message of a batch independently and to guarantee that a
blocked call produces zero downstream traffic. The second tradeoff is
authenticating once per HTTP request while authorizing per message: a token is a
property of the connection, not of an individual call, so re-verifying an HMAC
for every element of a 100-message batch would be pure waste.
