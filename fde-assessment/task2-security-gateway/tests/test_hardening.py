"""Resource limits and operational hardening for the gateway.

These cover the ways an unauthenticated caller could previously consume an
unbounded amount of a worker's memory or CPU with a single request.
"""

from __future__ import annotations

import json

import httpx
import pytest

import gateway
from conftest import DOWNSTREAM_URL, bearer, call, listing

PAYLOAD_TOO_LARGE = -32600


@pytest.fixture
async def small_limit_client(downstream_app):
    """A gateway with deliberately tiny limits, so the caps are cheap to hit."""
    downstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=downstream_app), base_url="http://downstream.internal"
    )
    app = gateway.create_app(
        downstream_url=DOWNSTREAM_URL,
        client=downstream_client,
        max_body_bytes=2_000,
        max_batch_size=5,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    ) as gateway_client:
        async with downstream_client:
            yield gateway_client


# --------------------------------------------------------------------------
# Body size
# --------------------------------------------------------------------------
async def test_oversized_body_is_rejected_with_413(small_limit_client, admin_token, call_log):
    payload = call("get_weather", {"city": "x" * 5_000})
    response = await small_limit_client.post("/mcp", json=payload, headers=bearer(admin_token))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == PAYLOAD_TOO_LARGE
    assert response.json()["error"]["data"]["max_bytes"] == 2_000
    assert call_log.count == 0, "an oversized body must never reach downstream"


async def test_body_just_under_the_cap_is_accepted(small_limit_client, admin_token):
    payload = call("get_weather", {"city": "x" * 1_500})
    response = await small_limit_client.post("/mcp", json=payload, headers=bearer(admin_token))
    assert response.status_code == 200


async def test_lying_content_length_is_still_capped(small_limit_client, admin_token, call_log):
    """A chunked body with no length header must be capped by the streamed read."""
    big = json.dumps(call("get_weather", {"city": "x" * 10_000})).encode()

    async def chunks():
        """Emit the body in pieces, so no content-length is sent."""
        for index in range(0, len(big), 512):
            yield big[index : index + 512]

    response = await small_limit_client.post(
        "/mcp",
        content=chunks(),
        headers={**bearer(admin_token), "content-type": "application/json"},
    )
    assert response.status_code == 413
    assert call_log.count == 0


async def test_non_numeric_content_length_is_rejected(small_limit_client, admin_token):
    response = await small_limit_client.post(
        "/mcp",
        content=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
        headers={**bearer(admin_token), "content-type": "application/json",
                 "content-length": "not-a-number"},
    )
    assert response.status_code in (400, 413)


async def test_oversized_body_is_rejected_before_authentication(small_limit_client, call_log):
    """The cheap check runs first: no token needed to be told the body is too big."""
    payload = call("admin_reset_key", {"tenant": "x" * 5_000})
    response = await small_limit_client.post("/mcp", json=payload)
    assert response.status_code == 413
    assert call_log.count == 0


# --------------------------------------------------------------------------
# Batch size
# --------------------------------------------------------------------------
async def test_oversized_batch_is_rejected(small_limit_client, admin_token, call_log):
    batch = [listing(request_id=i) for i in range(20)]
    response = await small_limit_client.post("/mcp", json=batch, headers=bearer(admin_token))
    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == PAYLOAD_TOO_LARGE
    assert error["data"] == {"max_batch_size": 5, "received": 20}
    assert call_log.count == 0, "an oversized batch must never reach downstream"


async def test_batch_at_the_cap_is_accepted(small_limit_client, admin_token):
    batch = [listing(request_id=i) for i in range(5)]
    response = await small_limit_client.post("/mcp", json=batch, headers=bearer(admin_token))
    assert response.status_code == 200
    assert len(response.json()) == 5


async def test_batch_one_over_the_cap_is_rejected(small_limit_client, admin_token):
    batch = [listing(request_id=i) for i in range(6)]
    response = await small_limit_client.post("/mcp", json=batch, headers=bearer(admin_token))
    assert response.status_code == 413


async def test_oversized_batch_is_rejected_before_any_authorization_work(
    small_limit_client, viewer_token, call_log
):
    """The batch cap must short-circuit, not authorize 10,000 messages first."""
    batch = [call("admin_reset_key", request_id=i) for i in range(50)]
    response = await small_limit_client.post("/mcp", json=batch, headers=bearer(viewer_token))
    assert response.status_code == 413
    assert call_log.count == 0


# --------------------------------------------------------------------------
# Defaults and pooling
# --------------------------------------------------------------------------
def test_default_limits_are_set_and_sane():
    assert gateway.MAX_BODY_BYTES == 1024 * 1024
    assert gateway.MAX_BATCH_SIZE == 100


async def test_downstream_pool_is_bounded():
    """An unbounded pool exhausts file descriptors under a burst.

    The lifespan is driven directly because ``httpx.ASGITransport`` does not run
    it, and the client this asserts on is built there.
    """
    app = gateway.create_app(downstream_url=DOWNSTREAM_URL)
    async with app.router.lifespan_context(app):
        pool = app.state.client._transport._pool
        assert pool._max_connections == gateway.MAX_DOWNSTREAM_CONNECTIONS
        assert pool._max_keepalive_connections == gateway.MAX_KEEPALIVE_CONNECTIONS
    assert 0 < gateway.MAX_DOWNSTREAM_CONNECTIONS <= 1000
    assert gateway.MAX_KEEPALIVE_CONNECTIONS <= gateway.MAX_DOWNSTREAM_CONNECTIONS


async def test_normal_traffic_is_unaffected_by_the_limits(client, admin_token, call_log):
    """A regression guard: the caps must not perturb ordinary requests."""
    response = await client.post("/mcp", json=call("get_weather", {"city": "Oslo"}),
                                 headers=bearer(admin_token))
    assert response.status_code == 200
    assert call_log.tool_calls == ["get_weather"]
