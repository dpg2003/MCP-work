"""Circuit breaker behaviour, and the latency budget it exists to protect.

The last test in this file is the one that matters most. It asserts a *cost*
rather than a mechanism: during a sustained provider outage, the latency a
client sees must stay far below the per-attempt deadline. The previous
breaker-less router failed that assertion by a factor of a thousand, and no
test in the suite asked the question -- which is why the defect shipped.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from circuit_breaker import BreakerPolicy, BreakerState, CircuitBreaker
from providers import ProviderRejected, ProviderTimeout, StubProvider
from router import ModelRouter


@pytest.fixture
def clock():
    """A manually advanced monotonic clock."""
    return [1_000.0]


@pytest.fixture
def breaker(clock):
    """A breaker that opens after 3 failures and probes after 10 seconds."""
    return CircuitBreaker(
        "p", BreakerPolicy(failure_threshold=3, recovery_seconds=10),
        clock=lambda: clock[0],
    )


# --------------------------------------------------------------------------
# State machine
# --------------------------------------------------------------------------
def test_a_new_breaker_is_closed_and_allows_calls(breaker):
    """Nothing is gated until a provider has actually misbehaved."""
    assert breaker.state is BreakerState.CLOSED
    assert breaker.allows()


def test_failures_below_the_threshold_do_not_open_it(breaker):
    """A couple of blips must not take a provider out of rotation."""
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED
    assert breaker.allows()


def test_the_threshold_opens_it_and_calls_are_refused(breaker):
    """At the threshold the provider is skipped rather than attempted."""
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert not breaker.allows()


def test_one_success_clears_the_failure_tally(breaker):
    """Only *consecutive* failures count, so intermittent errors never trip it."""
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED


def test_it_half_opens_after_the_recovery_window(breaker, clock):
    """Recovery is driven by traffic, so no timer has to be scheduled."""
    for _ in range(3):
        breaker.record_failure()
    clock[0] += 9.9
    assert breaker.state is BreakerState.OPEN
    clock[0] += 0.2
    assert breaker.state is BreakerState.HALF_OPEN


def test_a_successful_probe_closes_it(breaker, clock):
    """One good probe returns the provider to normal service."""
    for _ in range(3):
        breaker.record_failure()
    clock[0] += 11
    assert breaker.allows()
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED


def test_a_failed_probe_reopens_it_and_restarts_the_clock(breaker, clock):
    """A provider that is still down must not be hammered."""
    for _ in range(3):
        breaker.record_failure()
    clock[0] += 11
    assert breaker.allows()
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    clock[0] += 9
    assert breaker.state is BreakerState.OPEN, "the recovery window must restart"


def test_half_open_admits_only_the_configured_number_of_probes(clock):
    """A thundering herd on recovery would re-kill a fragile provider."""
    breaker = CircuitBreaker(
        "p", BreakerPolicy(failure_threshold=1, recovery_seconds=5, half_open_max_calls=1),
        clock=lambda: clock[0],
    )
    breaker.record_failure()
    clock[0] += 6
    assert breaker.allows()
    assert not breaker.allows(), "a second concurrent probe must be refused"


def test_an_indeterminate_outcome_neither_trips_nor_clears(breaker):
    """A 4xx is about the request, so it says nothing about provider health."""
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_indeterminate()
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN, "it must not have cleared the tally"


def test_reset_forces_it_closed(breaker):
    """An operator needs a way to put a provider back immediately."""
    for _ in range(3):
        breaker.record_failure()
    breaker.reset()
    assert breaker.state is BreakerState.CLOSED


@pytest.mark.parametrize(
    "kwargs",
    [{"failure_threshold": 0}, {"recovery_seconds": 0}, {"half_open_max_calls": 0},
     {"success_threshold": 0}],
)
def test_a_policy_that_could_never_work_is_rejected(kwargs):
    """Fail at construction rather than behaving mysteriously in production."""
    with pytest.raises(ValueError):
        BreakerPolicy(**kwargs)


def test_the_snapshot_reports_what_on_call_needs(breaker, clock):
    """An outage should be visible from a health endpoint, not only in logs."""
    for _ in range(3):
        breaker.record_failure()
    snapshot = breaker.snapshot()
    assert snapshot["provider"] == "p"
    assert snapshot["state"] == "open"
    assert snapshot["consecutive_failures"] == 3
    assert 0 < snapshot["retry_in_seconds"] <= 10
    assert snapshot["times_opened"] == 1


def test_counters_are_thread_safe(breaker):
    """The breaker is touched from the thread pool, so its state needs a lock."""
    import threading

    def hammer():
        """Alternate successes and failures from one thread."""
        for index in range(200):
            breaker.allows()
            if index % 2:
                breaker.record_success()
            else:
                breaker.record_failure()

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    stats = breaker.stats
    assert stats.successes + stats.failures == 8 * 200


# --------------------------------------------------------------------------
# What the breaker is for
# --------------------------------------------------------------------------
async def test_the_router_skips_an_open_provider_without_calling_it():
    """Once open, the dead provider must not be contacted at all."""
    dead = StubProvider("primary", error=ProviderTimeout("primary", "down"))
    good = StubProvider("secondary", text="ok")
    router = ModelRouter(dead, good, timeout_ms=1000, attempts_per_provider=1,
                         breaker_policy=BreakerPolicy(failure_threshold=1,
                                                      recovery_seconds=60))
    await router.complete("x", 8)
    calls_after_trip = dead.call_count
    for _ in range(10):
        routed = await router.complete("x", 8)
        assert routed.provider_used == "secondary"
        assert routed.attempts[0] == "primary:circuit_open"
    assert dead.call_count == calls_after_trip, "an open circuit must make no calls"


async def test_sustained_outage_latency_stays_far_below_the_deadline():
    """The cost assertion the breaker exists to satisfy.

    With a 3000 ms per-attempt deadline and a hard-down primary, a router with
    no health state re-attempts it on *every* request, so every client waits
    the full deadline for as long as the outage lasts. Here the breaker must
    confine that to the first couple of requests.
    """
    deadline_seconds = 3.0
    dead = StubProvider("primary", error=ProviderTimeout("primary", "down"),
                        delay_seconds=deadline_seconds)
    good = StubProvider("secondary", text="ok")
    router = ModelRouter(dead, good, timeout_ms=int(deadline_seconds * 1000),
                         attempts_per_provider=1,
                         breaker_policy=BreakerPolicy(failure_threshold=2,
                                                      recovery_seconds=300))

    latencies = []
    for _ in range(12):
        started = time.perf_counter()
        routed = await router.complete("x", 8)
        latencies.append(time.perf_counter() - started)
        assert routed.provider_used == "secondary"

    steady_state = latencies[2:]
    assert max(steady_state) < deadline_seconds / 10, (
        f"steady-state latency {max(steady_state):.2f}s is not far enough below "
        f"the {deadline_seconds}s deadline - the circuit is not short-circuiting"
    )
    # And the outage cost is bounded: only the probes before it tripped paid it.
    assert sum(1 for value in latencies if value >= deadline_seconds * 0.9) <= 2


async def test_recovery_restores_the_primary_without_a_restart():
    """An outage must heal on its own once the provider comes back."""
    clock = [1_000.0]
    flaky = StubProvider("primary", error=ProviderTimeout("primary", "down"))
    good = StubProvider("secondary", text="backup")
    router = ModelRouter(flaky, good, timeout_ms=500, attempts_per_provider=1,
                         breaker_policy=BreakerPolicy(failure_threshold=1,
                                                      recovery_seconds=30),
                         clock=lambda: clock[0])
    assert (await router.complete("x", 8)).provider_used == "secondary"
    assert (await router.complete("x", 8)).attempts[0] == "primary:circuit_open"

    flaky.error = None            # the provider recovers
    clock[0] += 31                # the recovery window elapses
    routed = await router.complete("x", 8)
    assert routed.provider_used == "primary"
    assert router.health()["providers"][0]["state"] == "closed"


async def test_a_non_retryable_rejection_does_not_open_the_circuit():
    """A bad request must not take a healthy provider out of rotation."""
    picky = StubProvider("primary", error=ProviderRejected("primary", "400"))
    router = ModelRouter(picky, StubProvider("secondary"), timeout_ms=500,
                         attempts_per_provider=1,
                         breaker_policy=BreakerPolicy(failure_threshold=1,
                                                      recovery_seconds=60))
    for _ in range(5):
        with pytest.raises(Exception):
            await router.complete("x", 8)
    assert router.health()["providers"][0]["state"] == "closed"
