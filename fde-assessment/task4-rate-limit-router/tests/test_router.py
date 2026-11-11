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
    """A fresh fake-provider app with both endpoints healthy."""
    fake_upstream.reset()
    return fake_upstream.create_app()


@pytest.fixture
async def upstream_url(upstream):
    """The fake providers, served on a real socket."""
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
    """Set one fake endpoint's failure mode and injected latency."""
    fake_upstream.STATE[which].update({"mode": mode, "latency_seconds": latency})


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------
async def test_primary_success_never_calls_the_secondary(http_router):
    """A healthy primary means the secondary is untouched, asserted by call count on both sides."""
    routed = await http_router.complete("hello", 32)
    assert routed.provider_used == "primary"
    assert routed.failed_over is False
    assert http_router.secondary.call_count == 0
    assert fake_upstream.STATE["secondary"]["calls"] == 0


# --------------------------------------------------------------------------
# Failover triggers
# --------------------------------------------------------------------------
async def test_primary_429_triggers_failover(http_router):
    """A rate-limited primary fails over and the client still gets a real answer."""
    set_mode("primary", "429")
    routed = await http_router.complete("hello", 32)
    assert routed.provider_used == "secondary"
    assert routed.failed_over is True
    assert routed.completion.text.startswith("[secondary]")
    # The primary is retried before the chain advances, so the count is the
    # configured attempt budget rather than one.
    assert http_router.primary.call_count == http_router.attempts_per_provider
    assert http_router.secondary.call_count == 1


async def test_primary_timeout_triggers_failover(http_router):
    """A hung primary is abandoned at the deadline and the secondary serves the request."""
    set_mode("primary", "hang")
    routed = await asyncio.wait_for(http_router.complete("hello", 32), timeout=10)
    assert routed.provider_used == "secondary"
    # A timeout is not retried: the first attempt already spent the whole
    # deadline, so retrying it would double what the client waits.
    assert routed.attempts == ["primary:timeout", "secondary:ok"]


async def test_primary_500_triggers_failover(http_router):
    """A server error is failover-worthy for the same reason a timeout is."""
    set_mode("primary", "500")
    routed = await http_router.complete("hello", 32)
    assert routed.provider_used == "secondary"
    assert routed.attempts[-1] == "secondary:ok"
    assert all(a.startswith("primary:unavailable") for a in routed.attempts[:-1])


async def test_primary_undecodable_body_triggers_failover(http_router):
    """A response that cannot be parsed is a failed provider, not a failed request."""
    set_mode("primary", "garbage")
    routed = await http_router.complete("hello", 32)
    assert routed.provider_used == "secondary"


async def test_connection_error_triggers_failover():
    """A refused connection fails over too; a router that handles timeouts but not refusals has a hole in it."""
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
    """The shipped deadline is the documented one, independent of what any test overrides."""
    assert DEFAULT_TIMEOUT_MS == 3000
    router = ModelRouter(StubProvider("p"), StubProvider("s"))
    assert router.timeout_ms == 3000


async def test_router_pushes_its_deadline_down_into_http_providers(upstream_url):
    """The deadline is enforced in the provider so it covers connect and read, not just the awaited coroutine."""
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
    """Failover happens at the deadline rather than when the upstream eventually gives up."""
    set_mode("primary", "hang")
    start = time.perf_counter()
    await http_router.complete("hello", 32)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"took {elapsed:.2f}s for a 300ms deadline"


# --------------------------------------------------------------------------
# Both providers down
# --------------------------------------------------------------------------
async def test_both_providers_failing_gives_one_standardized_error(http_router):
    """Two different failures collapse into a single error in the gateway's own shape, with the attempt trail preserved."""
    set_mode("primary", "429")
    set_mode("secondary", "500")
    with pytest.raises(GatewayError) as raised:
        await http_router.complete("hello", 32)
    error = raised.value
    payload = error.to_payload()
    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"type", "message", "request_id", "details"}
    assert payload["error"]["type"] == UPSTREAM_UNAVAILABLE
    attempts = payload["error"]["details"]["attempts"]
    assert attempts[0].startswith("primary:rate_limited")
    assert attempts[-1].startswith("secondary:unavailable")


async def test_both_providers_timing_out_gives_the_same_shape(http_router):
    """The shape does not vary with the kind of failure."""
    set_mode("primary", "hang")
    set_mode("secondary", "hang")
    with pytest.raises(GatewayError) as raised:
        await asyncio.wait_for(http_router.complete("hello", 32), timeout=10)
    attempts = raised.value.to_payload()["error"]["details"]["attempts"]
    assert {a.split(":")[0] for a in attempts} == {"primary", "secondary"}
    assert all(a.endswith("timeout") for a in attempts)


async def test_no_upstream_detail_leaks_from_either_provider(http_router):
    """Six planted secrets, from either provider, must all be absent from the client-facing payload."""
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
    """A hundred and fifty failovers reuse pooled sockets rather than accumulating them, and the router keeps no growing state."""
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
        # The router's state is bounded: one breaker per provider, created at
        # construction. It must not grow with traffic.
        assert set(router.breakers) == {"primary", "secondary"}
        assert len(router.breakers) == len(router.providers) == 2


async def test_concurrent_requests_during_a_flap_all_resolve():
    """Fifty concurrent requests against an intermittently failing primary all resolve to a real answer."""
    primary = StubProvider("primary", delay_seconds=0.01)
    secondary = StubProvider("secondary", text="backup")
    router = ModelRouter(primary, secondary, timeout_ms=1000)

    async def one(index: int):
        """Issue one request, flipping the primary's health on odd indices."""
        primary.error = ProviderRateLimited("primary", "429") if index % 2 else None
        return await router.complete(f"r{index}", 8)

    results = await asyncio.gather(*[one(index) for index in range(50)])
    assert len(results) == 50
    assert all(r.completion.text in {"ok", "backup"} for r in results)


# --------------------------------------------------------------------------
# An ordered chain, not a hardcoded pair
# --------------------------------------------------------------------------
async def test_a_chain_of_more_than_two_providers_is_walked_in_order():
    """Two providers was a special case of a list; the list costs nothing."""
    from circuit_breaker import BreakerPolicy

    down = [StubProvider(f"p{i}", error=ProviderTimeout(f"p{i}", "down")) for i in range(3)]
    up = StubProvider("p3", text="served")
    router = ModelRouter(providers=[*down, up], timeout_ms=500,
                         attempts_per_provider=1,
                         breaker_policy=BreakerPolicy(failure_threshold=99,
                                                      recovery_seconds=60))
    routed = await router.complete("x", 8)
    assert routed.provider_used == "p3"
    assert routed.failed_over is True
    assert [a.split(":")[0] for a in routed.attempts] == ["p0", "p1", "p2", "p3"]


async def test_a_single_provider_chain_is_valid():
    """Not every deployment has a second provider to fall back to."""
    router = ModelRouter(providers=[StubProvider("only", text="ok")], timeout_ms=500)
    assert (await router.complete("x", 8)).provider_used == "only"
    assert router.secondary is None


async def test_an_empty_chain_is_refused_at_construction():
    """A router with nothing to route to should fail loudly, not at request time."""
    with pytest.raises(ValueError):
        ModelRouter(providers=[])


async def test_a_non_positive_attempt_budget_is_refused():
    """An attempt budget of zero would never call anything; reject it at construction."""
    with pytest.raises(ValueError):
        ModelRouter(StubProvider("p"), attempts_per_provider=0)


async def test_primary_and_secondary_still_name_the_first_two():
    """Backwards compatibility for callers written against the old shape."""
    first, second = StubProvider("a"), StubProvider("b")
    router = ModelRouter(providers=[first, second, StubProvider("c")])
    assert router.primary is first
    assert router.secondary is second


async def test_a_cheap_transient_failure_is_retried_before_advancing():
    """A 429 fails in milliseconds, so retrying it absorbs a blip for free."""
    flaky = StubProvider("primary", text="recovered")
    calls = {"n": 0}
    original = flaky.complete

    async def sometimes(prompt, max_tokens):
        """Fail the first call only, with a cheap failure."""
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderRateLimited("primary", "429")
        return await original(prompt, max_tokens)

    flaky.complete = sometimes
    router = ModelRouter(flaky, StubProvider("secondary", text="backup"),
                         timeout_ms=500, attempts_per_provider=2)
    routed = await router.complete("x", 8)
    assert routed.provider_used == "primary", "a retry should have recovered it"
    assert routed.failed_over is False
    assert routed.completion.text == "recovered"


async def test_health_reports_every_provider():
    """On-call needs to see which providers are being skipped."""
    router = ModelRouter(StubProvider("a"), StubProvider("b"), timeout_ms=500)
    health = router.health()
    assert [p["provider"] for p in health["providers"]] == ["a", "b"]
    assert health["available"] == ["a", "b"]
    assert health["timeout_ms"] == 500


async def test_reset_breakers_returns_every_provider_to_service():
    """An operator needs a way to force a provider back without a restart."""
    from circuit_breaker import BreakerPolicy

    dead = StubProvider("primary", error=ProviderTimeout("primary", "down"))
    router = ModelRouter(dead, StubProvider("secondary"), timeout_ms=500,
                         attempts_per_provider=1,
                         breaker_policy=BreakerPolicy(failure_threshold=1,
                                                      recovery_seconds=600))
    await router.complete("x", 8)
    assert router.health()["providers"][0]["state"] == "open"
    router.reset_breakers()
    assert router.health()["providers"][0]["state"] == "closed"


async def test_a_single_timeout_costs_one_deadline_not_two():
    """The retry policy's whole justification, asserted.

    Retrying a timeout would make one slow provider cost the client two full
    deadlines before it ever reached the secondary. The budget here is one
    deadline plus a margin.
    """
    deadline = 1.0
    slow = StubProvider("primary", error=ProviderTimeout("primary", "timed out"),
                        delay_seconds=deadline)
    router = ModelRouter(slow, StubProvider("secondary", text="ok"),
                         timeout_ms=int(deadline * 1000), attempts_per_provider=2)
    started = time.perf_counter()
    routed = await router.complete("x", 8)
    elapsed = time.perf_counter() - started
    assert routed.provider_used == "secondary"
    assert elapsed < deadline * 1.6, (
        f"failover took {elapsed:.2f}s against a {deadline:.0f}s deadline - "
        "a timeout is being retried"
    )
    assert slow.call_count == 1, "a timeout must not be retried"
