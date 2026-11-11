"""Fixtures wiring the gateway to the mock downstream server.

The gateway's httpx client is injectable, so the tests mount the downstream
FastAPI app directly onto an ``ASGITransport``. No ports are bound, the tests
are fast and deterministic, and the downstream call log stays inspectable —
which is how "the downstream server was never called" is asserted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import downstream  # noqa: E402
import gateway  # noqa: E402
import tokens  # noqa: E402

DOWNSTREAM_URL = "http://downstream.internal/mcp"
TEST_SECRET = b"unit-test-secret"


@pytest.fixture(autouse=True)
def _pin_secret(monkeypatch):
    """Pin the signing secret so tests never depend on the dev default."""
    monkeypatch.setenv("GATEWAY_TOKEN_SECRET", TEST_SECRET.decode())


@pytest.fixture
def downstream_app():
    """A fresh mock downstream server with a cleared call log."""
    downstream.CALL_LOG.reset()
    downstream.FAILURE.update({"mode": "none", "delay_seconds": 0.0})
    return downstream.create_app()


@pytest.fixture
def call_log():
    """The downstream call log, for asserting a blocked call never forwarded."""
    return downstream.CALL_LOG


@pytest_asyncio.fixture
async def client(downstream_app):
    """An httpx client talking to the gateway, which talks to the mock downstream."""
    downstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=downstream_app), base_url="http://downstream.internal"
    )
    app = gateway.create_app(downstream_url=DOWNSTREAM_URL, client=downstream_client)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    ) as gateway_client:
        async with downstream_client:
            yield gateway_client


@pytest_asyncio.fixture
async def broken_upstream_client_factory():
    """Builds a gateway whose downstream transport fails in a chosen way.

    Real network faults (timeouts, connection refusals) cannot be produced by
    an in-process ASGI mount, so they are injected at the transport layer.
    """

    created: list[httpx.AsyncClient] = []

    def factory(exc: Exception | None = None, status_code: int | None = None, body: str = ""):
        """Build a gateway whose downstream fails in the requested way."""
        def handler(request: httpx.Request) -> httpx.Response:
            """Serve the configured exception or canned response."""
            if exc is not None:
                raise exc
            return httpx.Response(status_code or 200, text=body)

        downstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        created.append(downstream_client)
        app = gateway.create_app(downstream_url=DOWNSTREAM_URL, client=downstream_client)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
        )

    yield factory
    for created_client in created:
        await created_client.aclose()


@pytest.fixture
def admin_token():
    """A valid token carrying the ``admin`` role."""
    return tokens.issue("root@example.com", "admin", secret=TEST_SECRET)


@pytest.fixture
def viewer_token():
    """A valid token carrying the ``viewer`` role."""
    return tokens.issue("reader@example.com", "viewer", secret=TEST_SECRET)


def bearer(token: str) -> dict[str, str]:
    """Build an ``Authorization: Bearer`` header for ``token``."""
    return {"Authorization": f"Bearer {token}"}


def call(name: str, arguments=None, request_id: int = 1) -> dict:
    """Build a ``tools/call`` JSON-RPC request."""
    params: dict = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}


def listing(request_id: int = 1) -> dict:
    """Build a ``tools/list`` JSON-RPC request."""
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/list"}
