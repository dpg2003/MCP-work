"""Task 1 assessment cases: strict validation and stdio transport handling."""

from __future__ import annotations

import json

from _harness import Runner, StdioMCP, describe

VALID_ID = "CUST-12345"
GOOD_REASON = "Duplicate charge on order"


def run(runner: Runner) -> None:
    """Execute cases 1.1 - 1.16 against a live server subprocess."""
    client = StdioMCP()

    # 1.16 first in practice: the handshake had to succeed to get here.
    handshake_ok = "result" in client.handshake
    listing = client.request("tools/list")
    tools = sorted(t["name"] for t in listing.get("result", {}).get("tools", []))

    # -- 1.1 ---------------------------------------------------------------
    r = client.call("get_customer_record", {"customer_id": VALID_ID})
    runner.record("1.1", "Valid customer_id format", describe(r), "result" in r)

    # -- 1.2 / 1.3 / 1.4 ---------------------------------------------------
    for number, value, label in (
        ("1.2", "12345", "Invalid customer_id (no prefix)"),
        ("1.3", "cust-12345", "Invalid customer_id (lowercase prefix)"),
        ("1.4", "CUST-1", "Invalid customer_id (wrong length)"),
    ):
        r = client.call("get_customer_record", {"customer_id": value})
        runner.record(number, label, describe(r),
                      r.get("error", {}).get("code") == -32602)

    # -- 1.5 ---------------------------------------------------------------
    r = client.call("get_customer_record", {})
    runner.record("1.5", "Missing customer_id", describe(r),
                  r.get("error", {}).get("code") == -32602)

    # -- 1.6 ---------------------------------------------------------------
    r = client.call("trigger_refund",
                    {"customer_id": VALID_ID, "amount": 25.50, "reason": GOOD_REASON})
    runner.record("1.6", "Valid refund request", describe(r), "result" in r)

    # -- 1.7 / 1.8 / 1.9 ---------------------------------------------------
    for number, amount, label in (
        ("1.7", -10, "Negative refund amount"),
        ("1.8", 0, "Zero refund amount"),
        ("1.9", "abc", "Non-numeric amount"),
    ):
        r = client.call("trigger_refund",
                        {"customer_id": VALID_ID, "amount": amount, "reason": GOOD_REASON})
        error = r.get("error", {})
        detail = ""
        if error.get("data", {}).get("errors"):
            detail = " — " + error["data"]["errors"][0]["error"]
        runner.record(number, label, describe(r) + detail, error.get("code") == -32602)

    # -- 1.10 / 1.11 -------------------------------------------------------
    r = client.call("trigger_refund",
                    {"customer_id": VALID_ID, "amount": 10, "reason": "short"})
    runner.record("1.10", "Reason too short (5 chars)", describe(r),
                  r.get("error", {}).get("code") == -32602)

    r = client.call("trigger_refund",
                    {"customer_id": VALID_ID, "amount": 10, "reason": "1234567890"})
    runner.record("1.11", "Reason exactly 10 chars (boundary)", describe(r), "result" in r)

    # -- 1.12 --------------------------------------------------------------
    r = client.call("trigger_refund", {"customer_id": VALID_ID, "amount": 10})
    runner.record("1.12", "Missing reason field", describe(r),
                  r.get("error", {}).get("code") == -32602)

    # -- 1.13 --------------------------------------------------------------
    r = client.call("no_such_tool", {})
    runner.record("1.13", "Unknown tool call", describe(r),
                  r.get("error", {}).get("code") == -32601)

    # -- 1.14: stdout purity ----------------------------------------------
    # Fire a further burst of mixed valid/invalid traffic plus a garbage frame,
    # then assert every line stdout ever produced parses as JSON-RPC.
    for index in range(10):
        client.call("get_customer_record", {"customer_id": VALID_ID})
        client.call("get_customer_record", {"customer_id": f"bad-{index}"})
    client.send_raw("this is not json at all")
    client.read()   # the -32700 the server answers with

    bad_lines = []
    for line in client.stdout_lines:
        try:
            message = json.loads(line)
            if message.get("jsonrpc") != "2.0":
                bad_lines.append(line)
        except json.JSONDecodeError:
            bad_lines.append(line)
    runner.record(
        "1.14", "Stdout purity",
        f"{len(client.stdout_lines)} stdout lines captured, all valid JSON-RPC 2.0; "
        f"{len(bad_lines)} stray lines",
        not bad_lines,
    )

    stderr = client.close()

    # -- 1.15: stderr logging ---------------------------------------------
    stderr_lines = [line for line in stderr.splitlines() if line.strip()]
    has_logs = any("fde.mcp" in line for line in stderr_lines)
    runner.record(
        "1.15", "Stderr logging",
        f"{len(stderr_lines)} log lines on stderr, 0 on stdout; "
        f"e.g. `{stderr_lines[0][:60] if stderr_lines else ''}…`",
        has_logs and not bad_lines,
    )

    # -- 1.16 --------------------------------------------------------------
    runner.record(
        "1.16", "Transport handshake",
        f"`initialize` OK (protocol {client.handshake.get('result', {}).get('protocolVersion')}); "
        f"`tools/list` returned {tools}",
        handshake_ok and tools == ["get_customer_record", "trigger_refund"],
    )
