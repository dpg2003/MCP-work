"""Failover behaviour, timeout precision, and error sanitisation."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

import fake_upstream
from conftest import live_server
from errors import UPSTREAM_UNAVAILABLE, GatewayError
from providers import (
    HttpModelProvider,
    ProviderRateLimited,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
    StubProvider,
)
from router import DEFAULT_TIMEOUT_MS, ModelRouter

@pytest.fixture
def upstream():
    fake_upstream.reset()
    return fake_upstream.create_app()


@pytest.fixture
async def upstream_url(upstream):
    async with live_server(upstream) as base_url:
        yield base_url


@pytest.fixture
async def http_router(upstream_url):
    """Router over the fake HTTP endpoints, with a short deadline for speed."""
    async with httpx.AsyncClient(base_url=upstream_url) as client:
        primary = HttpModelProvider("primary", f"{upstream_url}/primary/v1/complete", client)
        secondary = HttpModelProvider("secondary", f"{upstream_url}/secondary/v1/complete", client)
        yield ModelRouter(primary, secondary, timeout_ms=300)


def set_mode(which: str, mode: str, latency: float = 0.0) -> None:
    fake_upstream.STATE[which].update({"mode": mode, "latency_seconds": latency})


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------
async def test_primary_success_never_calls_the_secondary(http_router):
    routed = await http_router.complete("hello", 32)
    assert routed.provider_used == "primary"
    assert routed.failed_over is False
    assert http_router.secondary.call_count == 0
    assert fake_upstream.STATE["secondary"]["calls"] == 0


# --------------------------------------------------------------------------
# Failover triggers
# --------------------------------------------------------------------------
async def test_primary_429_triggers_failover(http_router):
    set_mode("primary", "429")
    routed = await http_router.complete("hello", 32)
    assert routed.provider_used == "secondary"
    assert routed.failed_over is True
    assert routed.completion.text.startswith("[secondary]")
    assert http_router.primary.call_count == 1
    assert http_router.secondary.call_count == 1


async def test_primary_timeout_triggers_failover(http_router):
    set_mode("primary", "hang")
    routed = await asyncio.wait_for(http_router.complete("hello", 32), timeout=10)
    assert routed.provider_used == "secondary"
    assert routed.attempts == ["primary:timeout", "secondary:ok"]


async def test_primary_500_triggers_failover(http_router):
    set_mode("primary", "500")
    routed = await http_router.complete("hello", 32)
    assert routed.provider_used == "secondary"
    assert routed.attempts == ["primary:unavailable", "secondary:ok"]


async def test_primary_undecodable_body_triggers_failover(http_router):
    set_mode("primary", "garbage")
    routed = await http_router.complete("hello", 32)
    assert routed.provider_used == "secondary"


async def test_connection_error_triggers_failover():
    primary = StubProvider("primary", error=ProviderUnavailable("primary", "connect refused"))
    secondary = StubProvider("secondary", text="from-secondary")
    routed = await ModelRouter(primary, secondary).complete("hi", 8)
    assert routed.completion.text == "from-secondary"


async def test_non_retryable_4xx_does_not_fail_over(http_router):
    """A 400 is a statement about the request; retrying elsewhere just burns quota."""
    set_mode("primary", "400")
    with pytest.raises(GatewayError) as raised:
        await http_router.complete("hello", 32)
    assert raised.value.error_type == UPSTREAM_UNAVAILABLE
    assert http_router.secondary.call_count == 0


# --------------------------------------------------------------------------
# Timeout precision
# --------------------------------------------------------------------------
def test_default_timeout_is_3000ms():
    assert DEFAULT_TIMEOUT_MS == 3000
    router = ModelRouter(StubProvider("p"), StubProvider("s"))
    assert router.timeout_ms == 3000


async def test_router_pushes_its_deadline_down_into_http_providers(upstream_url):
    async with httpx.AsyncClient(base_url=upstream_url) as client:
        primary = HttpModelProvider("primary", f"{upstream_url}/primary/v1/complete", client)
        secondary = HttpModelProvider("secondary", f"{upstream_url}/secondary/v1/complete", client)
        ModelRouter(primary, secondary, timeout_ms=1500)
        assert primary.timeout_seconds == 1.5
        assert secondary.timeout_seconds == 1.5


@pytest.mark.parametrize("latency_ms, expect_failover", [(150, False), (600, True)])
async def test_timeout_fires_on_the_correct_side_of_the_threshold(
    http_router, latency_ms, expect_failover
):
    """Just under the deadline succeeds on primary; just over fails over.

    The deadline is 300 ms here rather than 3000 ms so the suite stays fast.
    The threshold semantics are identical, and the default is asserted above.
    """
    set_mode("primary", "ok", latency=latency_ms / 1000)
    routed = await asyncio.wait_for(http_router.complete("hello", 32), timeout=10)
    assert routed.failed_over is expect_failover


async def test_timeout_is_not_flaky_across_repeats(http_router):
    """Ten consecutive runs on each side of the deadline, same outcome each time."""
    for _ in range(10):
        set_mode("primary", "ok", latency=0.05)
        assert (await http_router.complete("x", 8)).failed_over is False
    for _ in range(10):
        set_mode("primary", "ok", latency=0.8)
        assert (await http_router.complete("x", 8)).failed_over is True


async def test_timeout_returns_promptly_rather_than_waiting_out_the_upstream(http_router):
    set_mode("primary", "hang")
    start = time.perf_counter()
    await http_router.complete("hello", 32)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"took {elapsed:.2f}s for a 300ms deadline"


# --------------------------------------------------------------------------
# Both providers down
# --------------------------------------------------------------------------
async def test_both_providers_failing_gives_one_standardized_error(http_router):
    set_mode("primary", "429")
    set_mode("secondary", "500")
    with pytest.raises(GatewayError) as raised:
        await http_router.complete("hello", 32)
    error = raised.value
    payload = error.to_payload()
    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"type", "message", "request_id", "details"}
    assert payload["error"]["type"] == UPSTREAM_UNAVAILABLE
    assert payload["error"]["details"]["attempts"] == ["primary:rate_limited", "secondary:unavailable"]


async def test_both_providers_timing_out_gives_the_same_shape(http_router):
    set_mode("primary", "hang")
    set_mode("secondary", "hang")
    with pytest.raises(GatewayError) as raised:
        await asyncio.wait_for(http_router.complete("hello", 32), timeout=10)
    assert raised.value.to_payload()["error"]["details"]["attempts"] == [
        "primary:timeout",
        "secondary:timeout",
    ]


async def test_no_upstream_detail_leaks_from_either_provider(http_router):
    set_mode("primary", "429")
    set_mode("secondary", "500")
    with pytest.raises(GatewayError) as raised:
        await http_router.complete("hello", 32)
    serialized = repr(raised.value.to_payload())
    for secret in (
        "Traceback", "sk-live-9f2c31aa", "inference-primary.internal",
        "/opt/inference/server.py", "pool exhausted", "RuntimeError",
    ):
        assert secret not in serialized, secret


# --------------------------------------------------------------------------
# Flapping
# --------------------------------------------------------------------------
async def test_rapid_flapping_never_wedges_the_router(http_router):
    """Alternate primary health 200 times; every request must be routed correctly."""
    for index in range(200):
        healthy = index % 2 == 0
        set_mode("primary", "ok" if healthy else "429")
        routed = await http_router.complete(f"request {index}", 8)
        assert routed.provider_used == ("primary" if healthy else "secondary"), index
    # And it recovers instantly at the end: no sticky "primary is bad" state.
    set_mode("primary", "ok")
    assert (await http_router.complete("final", 8)).provider_used == "primary"


async def test_flapping_does_not_leak_connections(upstream_url):
    async with httpx.AsyncClient(base_url=upstream_url) as client:
        router = ModelRouter(
            HttpModelProvider("primary", f"{upstream_url}/primary/v1/complete", client),
            HttpModelProvider("secondary", f"{upstream_url}/secondary/v1/complete", client),
            timeout_ms=300,
        )
        for index in range(150):
            set_mode("primary", "ok" if index % 3 else "500")
            await router.complete("x", 8)

        # Sockets are pooled and reused, not accumulated one per failover.
        pool = client._transport._pool
        assert len(pool.connections) < 20, f"{len(pool.connections)} pooled connections"
        # And the router itself holds no per-request state that could grow.
        assert set(vars(router)) == {"primary", "secondary", "timeout_ms"}


async def test_concurrent_requests_during_a_flap_all_resolve():
    primary = StubProvider("primary", delay_seconds=0.01)
    secondary = StubProvider("secondary", text="backup")
    router = ModelRouter(primary, secondary, timeout_ms=1000)

    async def one(index: int):
        primary.error = ProviderRateLimited("primary", "429") if index % 2 else None
        return await router.complete(f"r{index}", 8)

    results = await asyncio.gather(*[one(index) for index in range(50)])
    assert len(results) == 50
    assert all(r.completion.text in {"ok", "backup"} for r in results)
