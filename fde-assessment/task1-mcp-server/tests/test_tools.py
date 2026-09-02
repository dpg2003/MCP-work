"""Behavioural tests for the two tools, exercised over real stdio."""

from __future__ import annotations

import json

import pytest

INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601
CUSTOMER_NOT_FOUND = -32000

VALID_ID = "CUST-A1B2C"
VALID_REASON = "duplicate charge on invoice 4471"


def payload(response: dict) -> dict:
    """Decode the structured content of a successful tools/call response."""
    assert "result" in response, response
    return response["result"]["structuredContent"]


def error(response: dict) -> dict:
    """Extract the error object from a failed tools/call response."""
    assert "error" in response, response
    return response["error"]


# --------------------------------------------------------------------------
# tools/list
# --------------------------------------------------------------------------
def test_tools_list_advertises_both_tools(client):
    tools = client.request("tools/list")["result"]["tools"]
    assert {t["name"] for t in tools} == {"get_customer_record", "trigger_refund"}
    refund = next(t for t in tools if t["name"] == "trigger_refund")
    assert set(refund["inputSchema"]["required"]) == {"customer_id", "amount", "reason"}


# --------------------------------------------------------------------------
# get_customer_record
# --------------------------------------------------------------------------
def test_get_customer_record_happy_path(client):
    record = payload(client.call_tool("get_customer_record", {"customer_id": VALID_ID}))
    assert record["customer_id"] == VALID_ID
    assert record["name"] == "Ada Lovelace"
    assert record["plan"] == "enterprise"


@pytest.mark.parametrize(
    "bad_id",
    [
        "CUST-123",        # too short
        "cust-abcde",      # lowercase prefix
        "CUSTOMER-12345",  # wrong prefix
        "A1B2C",           # no prefix
        "",                # empty
        "CUST-A1B2C ",     # trailing whitespace
        "CUST-A1B2CD",     # too long
        "CUST-A1B2!",      # non-alphanumeric payload
        "CUST-A1B2C\nCUST-A1B2C",  # newline injection
    ],
)
def test_get_customer_record_rejects_malformed_ids(client, bad_id):
    err = error(client.call_tool("get_customer_record", {"customer_id": bad_id}))
    assert err["code"] == INVALID_PARAMS
    assert err["data"]["errors"][0]["field"] == "customer_id"


def test_get_customer_record_missing_argument(client):
    err = error(client.call_tool("get_customer_record", {}))
    assert err["code"] == INVALID_PARAMS


def test_get_customer_record_extra_argument_is_rejected(client):
    err = error(
        client.call_tool("get_customer_record", {"customer_id": VALID_ID, "admin": True})
    )
    assert err["code"] == INVALID_PARAMS
    assert any(e["type"] == "extra_forbidden" for e in err["data"]["errors"])


def test_unknown_customer_is_a_distinct_error_not_a_validation_error(client):
    """Well-formed id, no record: -32000, never -32602 and never a crash."""
    err = error(client.call_tool("get_customer_record", {"customer_id": "CUST-ZZZZZ"}))
    assert err["code"] == CUSTOMER_NOT_FOUND
    assert err["code"] != INVALID_PARAMS
    assert err["data"]["customer_id"] == "CUST-ZZZZZ"


# --------------------------------------------------------------------------
# trigger_refund
# --------------------------------------------------------------------------
def test_trigger_refund_happy_path(client):
    result = payload(
        client.call_tool(
            "trigger_refund",
            {"customer_id": VALID_ID, "amount": 42.5, "reason": VALID_REASON},
        )
    )
    assert result["status"] == "accepted"
    assert result["amount"] == 42.5
    assert result["refund_id"].startswith("RF-")


def test_trigger_refund_accepts_integer_amount(client):
    """JSON has no float literal for whole numbers; 100 must still be valid."""
    result = payload(
        client.call_tool(
            "trigger_refund",
            {"customer_id": VALID_ID, "amount": 100, "reason": VALID_REASON},
        )
    )
    assert result["amount"] == 100


@pytest.mark.parametrize("amount", [0, 0.0, -1, -0.01])
def test_trigger_refund_rejects_non_positive_amount(client, amount):
    err = error(
        client.call_tool(
            "trigger_refund",
            {"customer_id": VALID_ID, "amount": amount, "reason": VALID_REASON},
        )
    )
    assert err["code"] == INVALID_PARAMS
    assert err["data"]["errors"][0]["field"] == "amount"


@pytest.mark.parametrize("amount", ["12.50", None, True, [1], {"v": 1}])
def test_trigger_refund_rejects_wrong_amount_types(client, amount):
    err = error(
        client.call_tool(
            "trigger_refund",
            {"customer_id": VALID_ID, "amount": amount, "reason": VALID_REASON},
        )
    )
    assert err["code"] == INVALID_PARAMS


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_trigger_refund_rejects_nan_and_infinity(client, literal):
    """Python's json module emits/accepts these; they must never reach the tool.

    Either the frame is rejected at parse time (-32700) or by the model
    (-32602). Both are clean; an unhandled exception or a success is not.
    """
    request_id = client.next_id()
    client.send_raw(
        '{"jsonrpc": "2.0", "id": %d, "method": "tools/call", "params": '
        '{"name": "trigger_refund", "arguments": {"customer_id": "%s", '
        '"amount": %s, "reason": "%s"}}}' % (request_id, VALID_ID, literal, VALID_REASON)
    )
    try:
        response = client.wait_for(request_id, timeout=5)
    except TimeoutError:
        response = client.wait_for_null_id(timeout=5)
    assert "error" in response, response
    assert response["error"]["code"] in (INVALID_PARAMS, -32700)


def test_trigger_refund_amount_above_documented_cap_is_rejected_not_truncated(client):
    err = error(
        client.call_tool(
            "trigger_refund",
            {"customer_id": VALID_ID, "amount": 1e12, "reason": VALID_REASON},
        )
    )
    assert err["code"] == INVALID_PARAMS
    assert err["data"]["errors"][0]["field"] == "amount"


def test_reason_boundary_nine_chars_fails_ten_passes(client):
    nine = error(
        client.call_tool(
            "trigger_refund", {"customer_id": VALID_ID, "amount": 1.0, "reason": "a" * 9}
        )
    )
    assert nine["code"] == INVALID_PARAMS

    ten = payload(
        client.call_tool(
            "trigger_refund", {"customer_id": VALID_ID, "amount": 1.0, "reason": "a" * 10}
        )
    )
    assert ten["reason"] == "a" * 10


def test_whitespace_only_reason_is_rejected(client):
    """Documented decision: the 10-character minimum is applied to the trimmed string."""
    err = error(
        client.call_tool(
            "trigger_refund",
            {"customer_id": VALID_ID, "amount": 1.0, "reason": "   \t\n      "},
        )
    )
    assert err["code"] == INVALID_PARAMS
    assert err["data"]["errors"][0]["field"] == "reason"


def test_padded_reason_is_trimmed_before_storing(client):
    result = payload(
        client.call_tool(
            "trigger_refund",
            {"customer_id": VALID_ID, "amount": 1.0, "reason": f"   {VALID_REASON}   "},
        )
    )
    assert result["reason"] == VALID_REASON


def test_unicode_reason_is_preserved(client):
    reason = "重複請求 — refund 🙏 requested by the customer"
    result = payload(
        client.call_tool(
            "trigger_refund", {"customer_id": VALID_ID, "amount": 1.0, "reason": reason}
        )
    )
    assert result["reason"] == reason


def test_oversized_reason_is_rejected_not_truncated(client):
    err = error(
        client.call_tool(
            "trigger_refund",
            {"customer_id": VALID_ID, "amount": 1.0, "reason": "x" * 5000},
        )
    )
    assert err["code"] == INVALID_PARAMS
    assert err["data"]["errors"][0]["field"] == "reason"


def test_refund_for_unknown_customer_is_not_found(client):
    err = error(
        client.call_tool(
            "trigger_refund",
            {"customer_id": "CUST-QQQQQ", "amount": 5.0, "reason": VALID_REASON},
        )
    )
    assert err["code"] == CUSTOMER_NOT_FOUND


# --------------------------------------------------------------------------
# tool dispatch
# --------------------------------------------------------------------------
def test_unknown_tool_is_method_not_found(client):
    err = error(client.call_tool("delete_everything", {}))
    assert err["code"] == METHOD_NOT_FOUND
    assert err["data"]["tool"] == "delete_everything"


def test_tool_names_are_case_sensitive(client):
    err = error(client.call_tool("Get_Customer_Record", {"customer_id": VALID_ID}))
    assert err["code"] == METHOD_NOT_FOUND


def test_arguments_must_be_an_object(client):
    err = error(client.call_tool("get_customer_record", ["CUST-A1B2C"]))
    assert err["code"] == INVALID_PARAMS
