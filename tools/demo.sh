#!/usr/bin/env bash
# End-to-end walkthrough of all four projects.
#
# Starts every service on a private port, exercises the behaviour each task is
# assessed on, prints what happened, and shuts everything down again. Nothing is
# left running and no state is left behind.
#
# This is a demonstration, not a test: the pytest suites are the source of truth.
# It exists so the behaviour can be seen rather than inferred from a green bar.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="${VENV:-$here/.venv}/bin/python"
assessment="$here/fde-assessment"

if [ ! -x "$python" ]; then
    echo "no virtualenv -- run ./setup.sh first" >&2
    exit 2
fi

export GATEWAY_TOKEN_SECRET="${GATEWAY_TOKEN_SECRET:-demo-secret}"
db="$(mktemp -u /tmp/demo-rate-limit-XXXXXX.sqlite3)"
pids=()

cleanup() {
    for pid in "${pids[@]:-}"; do kill "$pid" 2>/dev/null; done
    wait 2>/dev/null
    rm -f "$db" "$db"-wal "$db"-shm
}
trap cleanup EXIT

start() {           # start <dir> <uvicorn target> <port> [env assignments...]
    local dir="$1" target="$2" port="$3"; shift 3
    ( cd "$dir" && env "$@" "$python" -m uvicorn "$target" --port "$port" --log-level warning ) \
        >/dev/null 2>&1 &
    pids+=($!)
}

wait_for() {        # wait_for <url>
    for _ in $(seq 1 60); do
        curl -sf "$1" >/dev/null 2>&1 && return 0
        sleep 0.25
    done
    echo "  !! service at $1 never became ready" >&2
    return 1
}

hr() { printf '\n\033[1m%s\033[0m\n' "$1"; }
# -w "\n" because curl emits no trailing newline, which would otherwise run
# the next line of output onto the end of a JSON body.
post() { curl -s -w "\n" -X POST "$@"; }

# --------------------------------------------------------------------------
hr "TASK 1 - MCP server: stdout carries JSON-RPC and nothing else"
cd "$assessment/task1-mcp-server"
{
  echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"demo","version":"1"}}}'
  echo '{"jsonrpc":"2.0","method":"notifications/initialized"}'
  echo '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_customer_record","arguments":{"customer_id":"CUST-A1B2C"}}}'
  echo '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_customer_record","arguments":{"customer_id":"cust-abcde"}}}'
  echo '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_customer_record","arguments":{"customer_id":"CUST-ZZZZZ"}}}'
  echo 'this line is not json at all'
  sleep 1
} | "$python" server.py 2>/tmp/demo-t1.err | while read -r line; do
      echo "$line" | "$python" -c "
import json,sys
m = json.load(sys.stdin)
if 'error' in m: print(f\"  id={m['id']}  ERROR {m['error']['code']}  {m['error']['message']}\")
elif 'result' in m and 'structuredContent' in m.get('result', {}):
    print(f\"  id={m['id']}  OK     {m['result']['structuredContent'].get('name', m['result']['structuredContent'])}\")
else: print(f\"  id={m.get('id')}  OK     (handshake)\")
"
    done
echo "  every stdout line above parsed as JSON-RPC; logs went to stderr:"
echo "    $(head -1 /tmp/demo-t1.err)"

# --------------------------------------------------------------------------
hr "TASK 2 - security gateway: admin_ tools blocked without reaching downstream"
start "$assessment/task2-security-gateway" downstream:app 9001
start "$assessment/task2-security-gateway" gateway:app 9000 "DOWNSTREAM_URL=http://127.0.0.1:9001/mcp"
wait_for http://127.0.0.1:9000/healthz || exit 1
viewer=$(cd "$assessment/task2-security-gateway" && "$python" tokens.py viewer)
admin=$(cd "$assessment/task2-security-gateway" && "$python" tokens.py admin)

echo "  viewer -> admin_reset_key :"
post localhost:9000/mcp -H "Authorization: Bearer $viewer" -H 'content-type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key"}}' | sed 's/^/    /'
echo "  admin  -> admin_reset_key :"
post localhost:9000/mcp -H "Authorization: Bearer $admin" -H 'content-type: application/json' \
     -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"admin_reset_key"}}' | sed 's/^/    /'
echo "  no token -> get_weather   :"
post localhost:9000/mcp -H 'content-type: application/json' \
     -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_weather"}}' | sed 's/^/    /'
echo "  downstream call log (the blocked call is absent):"
curl -s -w '\n' localhost:9001/_control/stats | sed 's/^/    /'

# --------------------------------------------------------------------------
hr "TASK 3 - PII redaction: split across chunk boundaries, redacted in stream"
start "$assessment/task3-pii-redaction-gateway" app:app 8000
wait_for http://127.0.0.1:8000/healthz || exit 1
post localhost:8000/v1/generate -H 'content-type: application/json' \
     -d '{"prompt":"summarise the customer"}' -N | sed 's/^/    /'
echo "  (the upstream splits the email, SSN and card across chunks; the ticket"
echo "   number, IP and version string are near-misses and are preserved)"

# --------------------------------------------------------------------------
hr "TASK 4 - rate limiting and failover"
start "$assessment/task4-rate-limit-router" fake_upstream:app 9100
start "$assessment/task4-rate-limit-router" app:app 8080 \
      "RATE_LIMIT_DB=$db" "RATE_LIMIT_TOKENS=150" \
      "PRIMARY_URL=http://127.0.0.1:9100/primary/v1/complete" \
      "SECONDARY_URL=http://127.0.0.1:9100/secondary/v1/complete"
wait_for http://127.0.0.1:8080/healthz || exit 1

complete() { post localhost:8080/v1/complete -H 'content-type: application/json' \
             -H 'X-API-Key: key-acme' -d '{"prompt":"hello","max_tokens":100}'; }

echo "  healthy primary            :"; complete | sed 's/^/    /'
post localhost:9100/_control/primary -H 'content-type: application/json' -d '{"mode":"429"}' >/dev/null
echo "  primary 429 -> failover    :"; complete | sed 's/^/    /'
post localhost:9100/_control/primary -H 'content-type: application/json' -d '{"mode":"hang"}' >/dev/null
post localhost:9100/_control/secondary -H 'content-type: application/json' -d '{"mode":"500"}' >/dev/null
echo "  both down -> one clean error (no upstream detail):"; complete | sed 's/^/    /'
post localhost:9100/_control/primary -H 'content-type: application/json' -d '{"mode":"ok"}' >/dev/null
post localhost:9100/_control/secondary -H 'content-type: application/json' -d '{"mode":"ok"}' >/dev/null
echo "  exhausting a 150-token budget:"
for i in 1 2 3 4; do printf '    request %d -> HTTP %s\n' "$i" \
    "$(curl -s -o /dev/null -w '%{http_code}' -X POST localhost:8080/v1/complete \
        -H 'content-type: application/json' -H 'X-API-Key: key-acme' \
        -d '{"prompt":"hello","max_tokens":100}')"; done
echo "  the 429 body:"; complete | sed 's/^/    /'
echo "  readiness probe:"; curl -s -w '\n' localhost:8080/readyz | sed 's/^/    /'

hr "demo complete - all services stopped"
