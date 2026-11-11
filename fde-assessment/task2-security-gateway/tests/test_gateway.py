"""End-to-end tests for the MCP security gateway."""

from __future__ import annotations

import time

import httpx
import pytest

import tokens
from conftest import TEST_SECRET, bearer, call, listing

UNAUTHORIZED_TOOL_CALL = -32001
UNAUTHENTICATED = -32002
UPSTREAM_ERROR = -32003
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602


async def post(client, payload, headers=None):
    """POST a JSON-RPC payload to the gateway."""
    return await client.post("/mcp", json=payload, headers=headers or {})


# --------------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------------
async def test_admin_may_call_admin_tool(client, admin_token, call_log):
    """The privileged happy path: an admin reaches the admin tool and downstream records the call."""
    response = await post(client, call("admin_reset_key", {"tenant": "acme"}), bearer(admin_token))
    assert response.status_code == 200
    assert response.json()["result"]["structuredContent"]["tool"] == "admin_reset_key"
    assert call_log.tool_calls == ["admin_reset_key"]


async def test_viewer_may_call_regular_tool(client, viewer_token, call_log):
    """A viewer is authenticated and unrestricted on non-privileged tools."""
    response = await post(client, call("get_weather", {"city": "Oslo"}), bearer(viewer_token))
    assert response.status_code == 200
    assert response.json()["result"]["structuredContent"]["tool"] == "get_weather"
    assert call_log.tool_calls == ["get_weather"]


async def test_tools_list_is_forwarded_transparently(client, viewer_token, call_log):
    """Discovery passes through unmodified, including the admin tools a viewer may not call."""
    response = await post(client, listing(), bearer(viewer_token))
    assert response.status_code == 200
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert "admin_reset_key" in names and "get_weather" in names
    assert call_log.count == 1


async def test_tools_list_works_with_no_token_at_all(client, call_log):
    """Documented decision: discovery is unauthenticated."""
    response = await post(client, listing())
    assert response.status_code == 200
    assert "result" in response.json()
    assert call_log.count == 1


async def test_tools_list_works_with_a_garbage_token(client, call_log):
    """An unusable token is ignored rather than fatal on an unauthenticated route."""
    response = await post(client, listing(), {"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 200
    assert "result" in response.json()
    assert call_log.count == 1


# --------------------------------------------------------------------------
# The core interception
# --------------------------------------------------------------------------
async def test_viewer_calling_admin_tool_is_blocked_and_downstream_never_called(
    client, viewer_token, call_log
):
    """The core requirement: -32001 returned by the gateway itself, with the downstream call count proving nothing was forwarded."""
    response = await post(client, call("admin_reset_key", {"tenant": "acme"}), bearer(viewer_token))
    assert response.status_code == 200
    error = response.json()["error"]
    assert error["code"] == UNAUTHORIZED_TOOL_CALL
    assert error["message"] == "Unauthorized Tool Call"
    assert error["data"]["required_role"] == "admin"
    # The whole point: nothing reached the downstream server.
    assert call_log.count == 0
    assert call_log.tool_calls == []


async def test_every_admin_tool_is_blocked_for_viewers(client, viewer_token, call_log):
    """The prefix rule applies to every privileged tool, including the bare prefix itself."""
    for name in ("admin_reset_key", "admin_delete_tenant", "admin_"):
        response = await post(client, call(name), bearer(viewer_token))
        assert response.json()["error"]["code"] == UNAUTHORIZED_TOOL_CALL, name
    assert call_log.count == 0


async def test_blocked_call_preserves_the_request_id(client, viewer_token):
    """A locally-generated rejection still correlates with the client's request."""
    response = await post(client, call("admin_reset_key", request_id=4242), bearer(viewer_token))
    assert response.json()["id"] == 4242


# --------------------------------------------------------------------------
# Prefix-matching semantics (documented decision)
# --------------------------------------------------------------------------
async def test_prefix_check_is_exact_not_substring(client, viewer_token, call_log):
    """'notadmin_reset' does not *start with* 'admin_', so it is not privileged."""
    response = await post(client, call("notadmin_reset"), bearer(viewer_token))
    assert response.status_code == 200
    # Forwarded; downstream rejects it as an unknown tool, which is correct.
    assert response.json()["error"]["code"] == -32601
    assert call_log.tool_calls == ["notadmin_reset"]


async def test_prefix_check_is_case_sensitive_and_that_is_safe(client, viewer_token, call_log):
    """'Admin_Reset' is not the admin prefix — and is not a real tool either.

    The case-sensitive check lets it through to the downstream server, which
    answers -32601. No privileged tool is reachable, because the downstream
    namespace is itself case-sensitive.
    """
    response = await post(client, call("Admin_Reset"), bearer(viewer_token))
    assert response.json()["error"]["code"] == -32601
    assert call_log.tool_calls == ["Admin_Reset"]
    # And crucially: the real tool stays blocked.
    blocked = await post(client, call("admin_reset_key"), bearer(viewer_token))
    assert blocked.json()["error"]["code"] == UNAUTHORIZED_TOOL_CALL


async def test_leading_whitespace_does_not_smuggle_a_privileged_call(
    client, viewer_token, call_log
):
    """A leading space does not match the prefix and is not a real tool either, so nothing privileged becomes reachable."""
    response = await post(client, call(" admin_reset_key"), bearer(viewer_token))
    # Not privileged by prefix, but also not a real tool downstream.
    assert response.json()["error"]["code"] == -32601
    assert call_log.tool_calls == [" admin_reset_key"]


# --------------------------------------------------------------------------
# Authentication failures
# --------------------------------------------------------------------------
async def test_missing_authorization_header(client, call_log):
    """No credentials is a 401 with a WWW-Authenticate challenge, and no downstream traffic."""
    response = await post(client, call("get_weather"))
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer")
    assert response.json()["error"]["code"] == UNAUTHENTICATED
    assert call_log.count == 0


@pytest.mark.parametrize(
    "header",
    [
        "Bearer",                       # scheme only
        "Bearer ",                      # empty credentials
        "token abc",                    # wrong scheme
        "Basic YWRtaW46YWRtaW4=",       # wrong scheme
        "abcdef",                       # no scheme
        "Bearer ....",                  # structurally wrong
        "Bearer a.b.c",                 # too many segments
        "Bearer !!!!.!!!!",             # undecodable
    ],
)
async def test_malformed_authorization_headers_are_rejected_before_downstream(
    client, header, call_log
):
    """Eight malformed header shapes, each rejected cleanly rather than raising, and none forwarded."""
    response = await client.post("/mcp", json=call("get_weather"), headers={"Authorization": header})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == UNAUTHENTICATED
    assert "error" in response.json() and "Traceback" not in response.text
    assert call_log.count == 0


async def test_bearer_scheme_is_case_insensitive_per_rfc7235(client, admin_token, call_log):
    """RFC 7235 makes the scheme case-insensitive; the token itself stays case-sensitive."""
    response = await client.post(
        "/mcp", json=call("admin_reset_key"), headers={"Authorization": f"bEaReR {admin_token}"}
    )
    assert response.status_code == 200
    assert "result" in response.json()


async def test_tampered_token_is_rejected(client, admin_token, call_log):
    """Replacing the signature invalidates the token, so a client cannot mint its own."""
    payload, signature = admin_token.split(".")
    tampered = f"{payload}.{'A' * len(signature)}"
    response = await post(client, call("admin_reset_key"), bearer(tampered))
    assert response.status_code == 401
    assert call_log.count == 0


async def test_token_signed_with_the_wrong_secret_is_rejected(client, call_log):
    """A structurally perfect token from another issuer is refused."""
    forged = tokens.issue("mallory@example.com", "admin", secret=b"the-wrong-secret")
    response = await post(client, call("admin_reset_key"), bearer(forged))
    assert response.status_code == 401
    assert call_log.count == 0


async def test_payload_swapped_to_admin_without_resigning_is_rejected(client, viewer_token, call_log):
    """Classic attack: rewrite the claims, keep the old signature."""
    import base64
    import json

    _, signature = viewer_token.split(".")
    claims = {"sub": "reader@example.com", "role": "admin", "iat": 0, "exp": 9999999999}
    forged_payload = (
        base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
        .rstrip(b"=")
        .decode()
    )
    response = await post(client, call("admin_reset_key"), bearer(f"{forged_payload}.{signature}"))
    assert response.status_code == 401
    assert call_log.count == 0


async def test_expired_token_is_rejected(client, call_log):
    """Expiry is enforced, and the reason is reported so a client knows to refresh rather than re-auth."""
    expired = tokens.issue(
        "stale@example.com", "admin", ttl_seconds=60, secret=TEST_SECRET,
        issued_at=int(time.time()) - 3600,
    )
    response = await post(client, call("admin_reset_key"), bearer(expired))
    assert response.status_code == 401
    assert response.json()["error"]["data"]["reason"] == "token expired"
    assert call_log.count == 0


@pytest.mark.parametrize("role", ["superadmin", "ADMIN", "Admin", None, "", "root", 1])
async def test_unexpected_role_claims_fail_closed(client, role, call_log):
    """A role the gateway does not recognise is never treated as privileged."""
    import base64
    import hmac
    import json
    from hashlib import sha256

    claims = {"sub": "x@example.com", "role": role, "iat": 0, "exp": 9999999999}
    segment = (
        base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
        .rstrip(b"=")
        .decode()
    )
    signature = (
        base64.urlsafe_b64encode(hmac.new(TEST_SECRET, segment.encode(), sha256).digest())
        .rstrip(b"=")
        .decode()
    )
    # Correctly signed, so this really is testing the role check, not the HMAC.
    response = await post(client, call("admin_reset_key"), bearer(f"{segment}.{signature}"))
    assert response.status_code == 401
    assert call_log.count == 0


async def test_token_with_no_role_field_fails_closed(client, call_log):
    """An absent role is not treated as a default role; it is rejected even with a valid signature."""
    import base64
    import hmac
    import json
    from hashlib import sha256

    claims = {"sub": "x@example.com", "exp": 9999999999}
    segment = (
        base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode())
        .rstrip(b"=")
        .decode()
    )
    signature = (
        base64.urlsafe_b64encode(hmac.new(TEST_SECRET, segment.encode(), sha256).digest())
        .rstrip(b"=")
        .decode()
    )
    response = await post(client, call("get_weather"), bearer(f"{segment}.{signature}"))
    assert response.status_code == 401
    assert call_log.count == 0


# --------------------------------------------------------------------------
# Malformed payloads
# --------------------------------------------------------------------------
async def test_invalid_json_body(client, admin_token, call_log):
    """An undecodable body is -32700 at HTTP 400, and never forwarded."""
    response = await client.post(
        "/mcp", content=b"{not json", headers={**bearer(admin_token), "content-type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == PARSE_ERROR
    assert call_log.count == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"id": 1, "method": "tools/list"},                       # no jsonrpc
        {"jsonrpc": "1.0", "id": 1, "method": "tools/list"},     # wrong version
        {"jsonrpc": "2.0", "id": 1},                             # no method
        {"jsonrpc": "2.0", "id": 1, "method": 5},                # method not a string
        "a string",
        42,
    ],
)
async def test_non_jsonrpc_payloads_are_invalid_requests(client, admin_token, payload, call_log):
    """Six payloads that parse as JSON but are not JSON-RPC requests, all refused before authorization runs."""
    response = await post(client, payload, bearer(admin_token))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == INVALID_REQUEST
    assert call_log.count == 0


async def test_tools_call_with_no_name_does_not_crash(client, viewer_token, call_log):
    """The 'undefined.startsWith' case."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}
    response = await post(client, payload, bearer(viewer_token))
    assert response.status_code == 200
    assert response.json()["error"]["code"] == INVALID_PARAMS
    assert call_log.count == 0


@pytest.mark.parametrize("name", [None, 5, {"a": 1}, ["admin_reset_key"], ""])
async def test_tools_call_with_non_string_name_is_rejected(client, viewer_token, name, call_log):
    """A non-string tool name is invalid params, so the prefix check never runs on a non-string."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name}}
    response = await post(client, payload, bearer(viewer_token))
    assert response.json()["error"]["code"] == INVALID_PARAMS
    assert call_log.count == 0


async def test_tools_call_with_non_object_params_is_rejected(client, viewer_token, call_log):
    """A list where an object belongs is refused rather than indexed into."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["admin_reset_key"]}
    response = await post(client, payload, bearer(viewer_token))
    assert response.json()["error"]["code"] == INVALID_PARAMS
    assert call_log.count == 0


# --------------------------------------------------------------------------
# Batches
# --------------------------------------------------------------------------
async def test_batch_authorizes_each_sub_call_independently(client, viewer_token, call_log):
    """The permitted messages of a batch are forwarded and the privileged one is rejected, in a single response."""
    batch = [
        listing(request_id=1),
        call("get_weather", {"city": "Oslo"}, request_id=2),
        call("admin_reset_key", {"tenant": "acme"}, request_id=3),
    ]
    response = await post(client, batch, bearer(viewer_token))
    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()}
    assert "result" in by_id[1]
    assert "result" in by_id[2]
    assert by_id[3]["error"]["code"] == UNAUTHORIZED_TOOL_CALL
    # Only the two permitted messages were forwarded.
    assert call_log.tool_calls == ["get_weather"]


async def test_batch_with_admin_token_forwards_everything(client, admin_token, call_log):
    """An admin's batch passes through whole, in order."""
    batch = [
        call("admin_reset_key", {"tenant": "a"}, request_id=1),
        call("get_weather", {"city": "b"}, request_id=2),
    ]
    response = await post(client, batch, bearer(admin_token))
    assert all("result" in item for item in response.json())
    assert call_log.tool_calls == ["admin_reset_key", "get_weather"]


async def test_batch_where_everything_is_blocked_never_touches_downstream(
    client, viewer_token, call_log
):
    """When no message survives authorization, the gateway answers alone."""
    batch = [call("admin_reset_key", request_id=1), call("admin_delete_tenant", request_id=2)]
    response = await post(client, batch, bearer(viewer_token))
    assert [item["error"]["code"] for item in response.json()] == [UNAUTHORIZED_TOOL_CALL] * 2
    assert call_log.count == 0


async def test_batch_mixes_invalid_and_valid_messages(client, admin_token, call_log):
    """A malformed entry does not poison its neighbours; the valid one is still served."""
    batch = [{"nonsense": True}, call("get_weather", {"city": "x"}, request_id=2)]
    response = await post(client, batch, bearer(admin_token))
    codes = [item.get("error", {}).get("code") for item in response.json()]
    assert INVALID_REQUEST in codes
    assert call_log.tool_calls == ["get_weather"]


async def test_empty_batch_is_an_invalid_request(client, admin_token, call_log):
    """An empty array is a malformed batch per JSON-RPC 2.0, not a no-op success."""
    response = await post(client, [], bearer(admin_token))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == INVALID_REQUEST
    assert call_log.count == 0


# --------------------------------------------------------------------------
# Downstream failures
# --------------------------------------------------------------------------
async def test_downstream_500_becomes_a_clean_upstream_error(client, admin_token, downstream_app):
    """An upstream 5xx becomes a fixed reason code, with the upstream's own body withheld."""
    import downstream as downstream_module

    downstream_module.FAILURE["mode"] = "error"
    response = await post(client, call("get_weather"), bearer(admin_token))
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == UPSTREAM_ERROR
    assert error["data"]["reason"] == "bad_status"
    # No upstream body, no traceback, no internal detail.
    assert "exploded" not in response.text
    assert "Traceback" not in response.text


async def test_downstream_non_json_body_becomes_a_clean_error(client, admin_token):
    """An HTML error page from a proxy is a decode failure, not something to relay."""
    import downstream as downstream_module

    downstream_module.FAILURE["mode"] = "garbage"
    response = await post(client, call("get_weather"), bearer(admin_token))
    assert response.status_code == 502
    assert response.json()["error"]["data"]["reason"] == "invalid_response"
    assert "<html>" not in response.text


async def test_downstream_timeout_does_not_hang_the_client(
    broken_upstream_client_factory, admin_token
):
    """A slow downstream becomes a prompt 502 rather than an open connection, and the target host from the exception never reaches the client."""
    gateway_client = broken_upstream_client_factory(
        exc=httpx.ReadTimeout("timed out reading from 10.0.3.7:9001")
    )
    async with gateway_client:
        response = await gateway_client.post(
            "/mcp", json=call("get_weather"), headers=bearer(admin_token)
        )
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == UPSTREAM_ERROR
    assert error["data"]["reason"] == "timeout"
    # The internal hostname/port from the exception must not leak.
    assert "10.0.3.7" not in response.text


async def test_downstream_connection_refused_is_sanitized(
    broken_upstream_client_factory, admin_token
):
    """A refused connection reports a fixed reason; the internal service name in the error is withheld."""
    gateway_client = broken_upstream_client_factory(
        exc=httpx.ConnectError("[Errno 111] Connection refused to mcp-internal.svc:9001")
    )
    async with gateway_client:
        response = await gateway_client.post(
            "/mcp", json=call("get_weather"), headers=bearer(admin_token)
        )
    assert response.status_code == 502
    assert response.json()["error"]["data"]["reason"] == "unreachable"
    assert "mcp-internal.svc" not in response.text


async def test_upstream_stack_trace_never_reaches_the_client(
    broken_upstream_client_factory, admin_token
):
    """A planted traceback, password and internal hostname must all be absent from the response the client sees."""
    leaky_body = (
        'Traceback (most recent call last):\n  File "/srv/app/handler.py", line 91\n'
        "  RuntimeError: DB_PASSWORD=hunter2 at db-primary.internal:5432"
    )
    gateway_client = broken_upstream_client_factory(status_code=500, body=leaky_body)
    async with gateway_client:
        response = await gateway_client.post(
            "/mcp", json=call("get_weather"), headers=bearer(admin_token)
        )
    assert response.status_code == 502
    for secret in ("Traceback", "hunter2", "db-primary.internal", "/srv/app/handler.py"):
        assert secret not in response.text


async def test_gateway_stays_healthy_after_a_downstream_failure(client, admin_token):
    """A downstream outage is not sticky: the next request succeeds once downstream recovers."""
    import downstream as downstream_module

    downstream_module.FAILURE["mode"] = "error"
    assert (await post(client, call("get_weather"), bearer(admin_token))).status_code == 502
    downstream_module.FAILURE["mode"] = "none"
    ok = await post(client, call("get_weather"), bearer(admin_token))
    assert ok.status_code == 200 and "result" in ok.json()
