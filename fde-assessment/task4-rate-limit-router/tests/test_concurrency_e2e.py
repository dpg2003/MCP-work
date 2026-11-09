"""The gateway applies the concurrency ceiling, and always gives the slot back.

``max_concurrent`` was advertised over ``/v1/quota`` and enforced nowhere, so
these tests are deliberately about the *wiring* rather than the arithmetic --
the arithmetic is covered in ``test_concurrency.py``. A slot that leaks is worse
than no limit at all: the tenant's depth would ratchet up until every subsequent
request was shed permanently, so each exit path from the handler gets its own
test.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx
import pytest

from app import create_app
from errors import RATE_LIMIT_EXCEEDED
from providers import Completion, ProviderUnavailable, StubProvider
from rate_limiter import RateLimiter
from router import ModelRouter
from tenants import TenantRegistry

# Two concurrent requests per tenant, so saturation is reachable in a test
# without opening sixteen sockets.
MAX_CONCURRENT = 2

CONFIG = {
    "tiers": {"test": {"tokens_per_minute": 50_000, "requests_per_minute": 300,
                       "max_concurrent": MAX_CONCURRENT}},
    "tenants": [
        {"tenant_id": "tenant-acme", "tier": "test", "api_keys": ["key-acme"]},
        {"tenant_id": "tenant-globex", "tier": "test", "api_keys": ["key-globex"]},
    ],
}


@dataclass
class GatedProvider:
    """A provider that parks every call until the test opens the gate.

    This is what makes the tests deterministic: ``StubProvider.delay_seconds``
    would require guessing a sleep long enough to overlap requests, whereas an
    explicit gate means a request is provably in flight when the next one
    arrives.
    """

    name: str = "gated"
    tokens_used: int = 100
    error: Exception | None = None
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    entered: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(0))
    calls: int = 0

    async def complete(self, prompt: str, max_tokens: int) -> Completion:
        """Record arrival, release the waiting test, then block on the gate."""
        self.calls += 1
        self.entered.release()
        await self.gate.wait()
        if self.error is not None:
            raise self.error
        return Completion(text="ok", tokens_used=self.tokens_used, provider=self.name)

    async def wait_for_arrivals(self, count: int) -> None:
        """Block until ``count`` calls have reached the provider."""
        for _ in range(count):
            await asyncio.wait_for(self.entered.acquire(), timeout=5)

    def open(self) -> None:
        """Let every parked call return."""
        self.gate.set()


def build(provider, db_path, clock):
    """A gateway whose only provider is the one supplied."""
    limiter = RateLimiter(db_path=db_path, clock=clock)
    app = create_app(
        limiter=limiter,
        router=ModelRouter(provider, provider, timeout_ms=5_000),
        registry=TenantRegistry.from_dict(CONFIG),
    )
    app.state.test_limiter = limiter
    return app


@pytest.fixture
def provider():
    """A provider that parks until the test opens it."""
    return GatedProvider()


@pytest.fixture
def gateway(provider, db_path, clock):
    """A gateway with a 2-concurrent ceiling and a gated provider."""
    app = build(provider, db_path, clock)
    try:
        yield app
    finally:
        app.state.test_limiter.close()


@pytest.fixture
async def client(gateway):
    """An httpx client wired to the gateway app."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway.test"
    ) as instance:
        yield instance


# Every request these tests await to completion is one the gateway is supposed
# to answer promptly -- either shed before routing, or served by a provider that
# is already open or already failing. If enforcement regresses, a request that
# should have been shed reaches the parked gate instead and blocks forever, so
# every await is bounded: a broken ceiling has to fail the suite, not hang it.
RESPONSE_TIMEOUT_SECONDS = 5


def post(client, key="key-acme"):
    """Start one completion request without awaiting it."""
    return asyncio.create_task(
        client.post(
            "/v1/complete",
            json={"prompt": "hello world", "max_tokens": 64},
            headers={"X-API-Key": key},
        )
    )


async def post_now(client, key="key-acme"):
    """POST one request that must be answered promptly, and return it.

    Raises:
        TimeoutError: The gateway parked the request instead of answering it,
            which is the signature of a ceiling that is no longer enforced.
    """
    return await asyncio.wait_for(post(client, key), timeout=RESPONSE_TIMEOUT_SECONDS)


def assert_shed_for_concurrency(response):
    """Assert this is a 429 naming the concurrency dimension, and return it."""
    assert response.status_code == 429
    error = response.json()["error"]
    assert error["type"] == RATE_LIMIT_EXCEEDED
    details = error["details"]
    assert details["limit"] == "concurrency"
    assert details["limit_concurrent"] == MAX_CONCURRENT
    assert details["retry_after_seconds"] > 0
    assert "Traceback" not in response.text
    return details


# --------------------------------------------------------------------------
# The ceiling is enforced
# --------------------------------------------------------------------------


async def test_requests_up_to_the_ceiling_are_admitted(client, provider):
    """Two concurrent requests both reach the provider."""
    tasks = [post(client) for _ in range(MAX_CONCURRENT)]
    await provider.wait_for_arrivals(MAX_CONCURRENT)
    provider.open()
    responses = await asyncio.gather(*tasks)
    assert [r.status_code for r in responses] == [200] * MAX_CONCURRENT


async def test_one_past_the_ceiling_is_shed_with_a_clean_429(client, provider):
    """The third concurrent request is rejected while the first two are parked."""
    held = [post(client) for _ in range(MAX_CONCURRENT)]
    await provider.wait_for_arrivals(MAX_CONCURRENT)

    shed = await post_now(client)
    details = assert_shed_for_concurrency(shed)
    assert details["in_flight"] == MAX_CONCURRENT

    provider.open()
    await asyncio.gather(*held)


async def test_a_shed_request_never_reaches_a_provider(client, provider):
    """Shedding happens before routing, so no upstream capacity is spent."""
    held = [post(client) for _ in range(MAX_CONCURRENT)]
    await provider.wait_for_arrivals(MAX_CONCURRENT)
    assert provider.calls == MAX_CONCURRENT

    await post_now(client)
    assert provider.calls == MAX_CONCURRENT, "shed request called the provider"

    provider.open()
    await asyncio.gather(*held)


async def test_one_tenant_saturating_does_not_shed_another(client, provider):
    """A noisy tenant cannot consume another tenant's concurrency."""
    held = [post(client, key="key-acme") for _ in range(MAX_CONCURRENT)]
    await provider.wait_for_arrivals(MAX_CONCURRENT)

    assert (await post_now(client, key="key-acme")).status_code == 429

    other = post(client, key="key-globex")
    await provider.wait_for_arrivals(1)
    provider.open()
    assert (await other).status_code == 200
    await asyncio.gather(*held)


# --------------------------------------------------------------------------
# The slot comes back on every exit path
# --------------------------------------------------------------------------


async def test_the_slot_is_released_after_a_successful_request(client, provider, gateway):
    """A completed request leaves nothing in flight."""
    provider.open()
    assert (await post_now(client)).status_code == 200
    assert gateway.state.concurrency.snapshot() == {}


async def test_the_slot_is_released_when_every_provider_fails(client, db_path, clock, gateway):
    """A routing failure must not ratchet the tenant's depth up."""
    failing = GatedProvider(error=ProviderUnavailable("gated", "down"))
    failing.open()
    app = build(failing, db_path, clock)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
        ) as instance:
            assert (await post_now(instance)).status_code == 502
            assert app.state.concurrency.snapshot() == {}
    finally:
        app.state.test_limiter.close()


async def test_the_slot_is_released_on_an_unexpected_internal_error(client, db_path, clock):
    """Even a bug in the gateway gives the slot back."""
    exploding = GatedProvider(error=RuntimeError("boom in provider"))
    exploding.open()
    app = build(exploding, db_path, clock)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
        ) as instance:
            response = await post_now(instance)
            assert response.status_code == 500
            assert "boom in provider" not in response.text
            assert app.state.concurrency.snapshot() == {}
    finally:
        app.state.test_limiter.close()


async def test_the_slot_is_released_when_the_token_window_rejects(client, gateway, provider):
    """A window rejection returns before routing; the slot still comes back."""
    gateway.state.test_limiter.try_consume("tenant-acme", 50_000)
    provider.open()

    response = await post_now(client)
    assert response.status_code == 429
    assert response.json()["error"]["details"]["limit"] != "concurrency"
    assert gateway.state.concurrency.snapshot() == {}


async def test_shedding_does_not_leak_a_slot(client, provider):
    """A tenant shed repeatedly still recovers once its real work drains."""
    held = [post(client) for _ in range(MAX_CONCURRENT)]
    await provider.wait_for_arrivals(MAX_CONCURRENT)
    for _ in range(5):
        assert (await post_now(client)).status_code == 429

    provider.open()
    await asyncio.gather(*held)

    # Depth is back to zero, so the next request is admitted.
    assert (await post_now(client)).status_code == 200


# --------------------------------------------------------------------------
# Observability
# --------------------------------------------------------------------------


async def test_quota_reports_the_ceiling_and_the_live_depth(client, provider):
    """``/v1/quota`` reports a limit that is now real, plus current usage."""
    held = [post(client) for _ in range(MAX_CONCURRENT)]
    await provider.wait_for_arrivals(MAX_CONCURRENT)

    body = (await client.get("/v1/quota", headers={"X-API-Key": "key-acme"})).json()
    assert body["limits"]["max_concurrent"] == MAX_CONCURRENT
    assert body["in_flight"] == MAX_CONCURRENT

    provider.open()
    await asyncio.gather(*held)

    idle = (await client.get("/v1/quota", headers={"X-API-Key": "key-acme"})).json()
    assert idle["in_flight"] == 0
