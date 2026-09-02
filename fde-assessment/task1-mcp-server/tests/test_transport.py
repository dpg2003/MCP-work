"""Transport-level tests: stdout purity, malformed envelopes, concurrency.

These are the hard pass/fail criteria for Task 1.
"""

from __future__ import annotations

import json
import time

import pytest

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601

VALID_ID = "CUST-A1B2C"
VALID_REASON = "duplicate charge on invoice 4471"


def assert_valid_jsonrpc_line(line: str) -> dict:
    """Every stdout line must be a complete, well-formed JSON-RPC frame."""
    message = json.loads(line)  # raises on stray text / partial writes
    assert isinstance(message, dict), f"not a JSON object: {line!r}"
    assert message.get("jsonrpc") == "2.0", f"missing jsonrpc version: {line!r}"
    assert ("result" in message) ^ ("error" in message) or "method" in message, (
        f"neither a response nor a notification: {line!r}"
    )
    return message


# --------------------------------------------------------------------------
# stdout purity
# --------------------------------------------------------------------------
def test_stdout_contains_only_jsonrpc_under_mixed_traffic(client):
    """Fire 20+ mixed valid/invalid requests; every stdout line must parse."""
    requests: list[tuple[int | None, str]] = []

    def enqueue(params):
        """Send one tools/call request and remember its id."""
        request_id = client.next_id()
        client.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params})
        requests.append((request_id, "response"))

    for index in range(6):
        enqueue({"name": "get_customer_record", "arguments": {"customer_id": VALID_ID}})
        enqueue({"name": "get_customer_record", "arguments": {"customer_id": f"bad-{index}"}})
        enqueue(
            {
                "name": "trigger_refund",
                "arguments": {"customer_id": VALID_ID, "amount": 1.5, "reason": VALID_REASON},
            }
        )
        enqueue({"name": "trigger_refund", "arguments": {"customer_id": VALID_ID, "amount": -1}})
        enqueue({"name": f"ghost_tool_{index}", "arguments": {}})

    # Garbage frames interleaved with the structured ones.
    client.send_raw("this is definitely not json")
    client.send_raw("{ unbalanced json")
    client.send_raw('{"jsonrpc": "1.0", "id": 1, "method": "tools/list"}')
    client.send_raw("")

    for request_id, _ in requests:
        client.wait_for(request_id)
    time.sleep(0.5)  # let any trailing frames land

    assert len(requests) >= 20
    assert client.stdout_lines, "server produced no output at all"
    for line in client.stdout_lines:
        assert line.strip(), "blank line on stdout"
        assert_valid_jsonrpc_line(line)


def test_logging_goes_to_stderr_not_stdout(client):
    client.call_tool("get_customer_record", {"customer_id": VALID_ID})
    time.sleep(0.3)
    assert any("tools/call" in line for line in client.stderr_lines), client.stderr_lines
    joined = "\n".join(client.stdout_lines)
    assert "INFO" not in joined and "fde.mcp" not in joined


def test_startup_banner_never_reaches_stdout(raw_client):
    """Even before initialize, stdout must be empty of chatter."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if any("listening on stdio" in line for line in raw_client.stderr_lines):
            break
        time.sleep(0.05)
    else:  # pragma: no cover - only on a hung startup
        pytest.fail(f"server never announced startup: {raw_client.stderr_lines}")
    assert raw_client.stdout_lines == []


# --------------------------------------------------------------------------
# malformed envelopes
# --------------------------------------------------------------------------
def test_invalid_json_gets_parse_error_and_server_survives(client):
    client.send_raw("<<<not json at all>>>")
    response = client.wait_for_null_id()
    assert response["error"]["code"] == PARSE_ERROR
    assert response["id"] is None

    # Still alive and answering afterwards.
    ok = client.call_tool("get_customer_record", {"customer_id": VALID_ID})
    assert "result" in ok


def test_missing_jsonrpc_field_is_invalid_request(client):
    client.send_raw('{"id": 77, "method": "tools/list"}')
    response = client.wait_for(77)
    assert response["error"]["code"] == INVALID_REQUEST
    assert response["id"] == 77


def test_missing_id_on_a_request_is_treated_as_a_notification_not_a_crash(client):
    """A frame with a method but no id is a valid JSON-RPC notification.

    It must be absorbed silently (no response is permitted for a notification)
    and must not wedge the connection.
    """
    client.send_raw('{"jsonrpc": "2.0", "method": "tools/list"}')
    ok = client.call_tool("get_customer_record", {"customer_id": VALID_ID})
    assert "result" in ok


@pytest.mark.parametrize(
    "frame",
    [
        '{"jsonrpc": "2.0", "id": 91, "method": 42}',            # method not a string
        '{"jsonrpc": "2.0", "id": 92}',                          # no method at all
        '{"jsonrpc": "2.0", "id": {"a": 1}, "method": "tools/list"}',  # illegal id type
        '[]',                                                     # empty batch
        '"just a string"',                                        # not an object
        '12345',                                                  # not an object
    ],
)
def test_structurally_invalid_frames_get_an_error_and_do_not_kill_the_server(client, frame):
    client.send_raw(frame)
    time.sleep(0.3)
    # Some of these carry a recoverable id, some do not; either way the server
    # must still be answering.
    ok = client.call_tool("get_customer_record", {"customer_id": VALID_ID})
    assert "result" in ok
    for line in client.stdout_lines:
        assert_valid_jsonrpc_line(line)


def test_unknown_method_is_method_not_found(client):
    response = client.request("resources/read", {"uri": "file:///etc/passwd"})
    assert response["error"]["code"] == METHOD_NOT_FOUND


def test_server_does_not_hang_on_a_flood_of_garbage(client):
    for index in range(50):
        client.send_raw(f"garbage frame {index} }}{{")
    ok = client.call_tool("get_customer_record", {"customer_id": VALID_ID}, timeout=15)
    assert "result" in ok


# --------------------------------------------------------------------------
# concurrency
# --------------------------------------------------------------------------
def test_rapid_fire_requests_do_not_interleave_or_corrupt_frames(client):
    """Pipeline 60 requests without waiting; every response must be intact."""
    expected: dict[int, str] = {}
    for index in range(60):
        request_id = client.next_id()
        if index % 3 == 0:
            client.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {
                        "name": "trigger_refund",
                        "arguments": {
                            "customer_id": VALID_ID,
                            "amount": 1.0 + index,
                            "reason": f"pipelined refund number {index:04d}",
                        },
                    },
                }
            )
            expected[request_id] = "result"
        elif index % 3 == 1:
            client.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {
                        "name": "get_customer_record",
                        "arguments": {"customer_id": "CUST-99999"},
                    },
                }
            )
            expected[request_id] = "result"
        else:
            client.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": "get_customer_record", "arguments": {"customer_id": "nope"}},
                }
            )
            expected[request_id] = "error"

    for request_id, kind in expected.items():
        response = client.wait_for(request_id, timeout=20)
        assert kind in response, response
        assert response["id"] == request_id

    for line in client.stdout_lines:
        assert_valid_jsonrpc_line(line)

    # Each id answered exactly once.
    ids = [json.loads(line).get("id") for line in client.stdout_lines]
    answered = [i for i in ids if i in expected]
    assert len(answered) == len(set(answered)) == len(expected)


def test_concurrent_refunds_all_get_unique_ids(client):
    ids = []
    for index in range(25):
        ids.append(client.next_id())
        client.send(
            {
                "jsonrpc": "2.0",
                "id": ids[-1],
                "method": "tools/call",
                "params": {
                    "name": "trigger_refund",
                    "arguments": {
                        "customer_id": VALID_ID,
                        "amount": 2.0,
                        "reason": "concurrent refund stress test",
                    },
                },
            }
        )
    refund_ids = {
        client.wait_for(request_id, timeout=20)["result"]["structuredContent"]["refund_id"]
        for request_id in ids
    }
    assert len(refund_ids) == 25
