"""Tests for batch admission and the group-commit controller.

Group commit is an optimisation applied to a *correctness* control, so the bar
here is higher than "it is faster": the batched path must produce byte-identical
decisions to the serial path, must never admit past the limit, and must keep the
event loop free. Each of those is asserted directly.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from admission import DEFAULT_MAX_BATCH, AdmissionController, _GroupCommitQueue
from rate_limiter import RateLimiter


@pytest.fixture
def controller(limiter):
    """An admission controller over the temp-database limiter."""
    return AdmissionController(limiter)


def serial_oracle(db_path, clock, requests, limit_tokens=50_000):
    """Decide ``requests`` one at a time -- the semantics batching must match."""
    reference = RateLimiter(db_path=db_path, clock=clock, limit_tokens=limit_tokens)
    try:
        return [reference.try_consume(t, n).allowed for t, n in requests]
    finally:
        reference.close()


# --------------------------------------------------------------------------
# Batch == serial, exactly
# --------------------------------------------------------------------------
def test_batch_matches_serial_for_a_simple_sequence(limiter, db_path, clock, tmp_path):
    """A hand-checked sequence, compared against both a serial oracle and the expected answer."""
    requests = [("a", 20_000), ("a", 20_000), ("a", 20_000), ("b", 50_000), ("b", 1)]
    batched = [d.allowed for d in limiter.try_consume_many(requests)]
    expected = serial_oracle(str(tmp_path / "oracle.sqlite3"), clock, requests)
    assert batched == expected == [True, True, False, True, False]


def test_batch_decides_in_arrival_order(limiter):
    """A later request must see everything admitted earlier in the same batch."""
    decisions = limiter.try_consume_many([("a", 30_000), ("a", 30_000)])
    assert [d.allowed for d in decisions] == [True, False]
    assert decisions[0].used_tokens == 30_000
    assert decisions[1].used_tokens == 30_000  # the rejected one did not count


def test_batch_never_exceeds_the_limit(limiter):
    """Twenty requests in one batch admit exactly as many as fit and no more."""
    decisions = limiter.try_consume_many([("a", 7_000)] * 20)
    assert sum(d.allowed for d in decisions) == 7          # 7 * 7000 = 49,000
    assert limiter.usage("a") == 49_000


def test_batch_isolates_tenants(limiter):
    """Batching does not blur tenants together; each budget is tracked separately within the transaction."""
    requests = [("a", 50_000), ("a", 1), ("b", 50_000), ("b", 1)]
    assert [d.allowed for d in limiter.try_consume_many(requests)] == [True, False, True, False]
    assert limiter.usage("a") == limiter.usage("b") == 50_000


def test_empty_batch_is_a_no_op(limiter):
    """An empty batch does no work rather than opening a pointless transaction."""
    assert limiter.try_consume_many([]) == []
    assert limiter.reconcile_many([]) == []


def test_batch_rejects_negative_tokens_without_partial_application(limiter):
    """A bad entry rejects the whole batch, leaving nothing partially applied."""
    with pytest.raises(ValueError):
        limiter.try_consume_many([("a", 10), ("a", -1)])
    assert limiter.usage("a") == 0, "a rejected batch must apply nothing"


def test_randomised_batches_match_the_serial_oracle(limiter, db_path, clock, tmp_path):
    """500 random batches, each compared against one-at-a-time decisions."""
    import random

    rng = random.Random(20240301)
    for index in range(500):
        requests = [
            (rng.choice(["a", "b", "c"]), rng.choice([1, 100, 5_000, 20_000, 49_999, 50_001]))
            for _ in range(rng.randint(1, 12))
        ]
        fresh = RateLimiter(db_path=str(tmp_path / f"b{index}.sqlite3"), clock=clock)
        oracle = serial_oracle(str(tmp_path / f"o{index}.sqlite3"), clock, requests)
        try:
            assert [d.allowed for d in fresh.try_consume_many(requests)] == oracle, requests
        finally:
            fresh.close()


def test_batch_reservations_are_reconcilable(limiter):
    """Reservations created in a batch can be corrected afterwards like any other."""
    decisions = limiter.try_consume_many([("a", 10_000), ("a", 10_000)])
    usage = limiter.reconcile_many(
        [(decisions[0].reservation_id, 5, "a"), (decisions[1].reservation_id, 7, "a")]
    )
    assert usage == [12, 12]
    assert limiter.usage("a") == 12


def test_reconcile_many_rejects_negatives_without_partial_application(limiter):
    """The same all-or-nothing guarantee on the reconciliation path."""
    decision = limiter.try_consume_many([("a", 10_000)])[0]
    with pytest.raises(ValueError):
        limiter.reconcile_many([(decision.reservation_id, 5, "a"), (0, -1, None)])
    assert limiter.usage("a") == 10_000


# --------------------------------------------------------------------------
# The async controller
# --------------------------------------------------------------------------
async def test_concurrent_submissions_never_exceed_the_limit(controller):
    """Thirty concurrent submissions admit exactly the number that fit, which is the property batching must not break."""
    decisions = await asyncio.gather(*[controller.try_consume("a", 5_000) for _ in range(30)])
    assert sum(d.allowed for d in decisions) == 10
    assert await controller.usage("a") == 50_000
    await controller.aclose()


async def test_concurrent_submissions_across_tenants_are_independent(controller):
    """Interleaved tenants in one batch each get their own budget honoured."""
    decisions = await asyncio.gather(
        *[controller.try_consume(f"tenant-{i % 4}", 20_000) for i in range(20)]
    )
    for tenant in range(4):
        assert await controller.usage(f"tenant-{tenant}") == 40_000
    assert sum(d.allowed for d in decisions) == 8
    await controller.aclose()


async def test_a_lone_request_is_not_delayed_waiting_for_a_batch(controller):
    """Batching must never introduce latency when there is nothing to batch with."""
    start = time.perf_counter()
    decision = await controller.try_consume("a", 10)
    elapsed = time.perf_counter() - start
    assert decision.allowed
    assert elapsed < 0.25, f"single request took {elapsed*1000:.1f}ms"
    await controller.aclose()


async def test_results_are_returned_to_the_right_caller(controller):
    """Each future must resolve with its own decision, not a neighbour's."""
    sizes = [1, 2, 3, 4, 5, 6, 7, 8]
    decisions = await asyncio.gather(*[controller.try_consume("a", n) for n in sizes])
    assert [d.requested_tokens for d in decisions] == sizes
    await controller.aclose()


async def test_reconcile_is_batched_and_correct(controller):
    """Ten concurrent reconciliations agree on the resulting usage."""
    decisions = await asyncio.gather(*[controller.try_consume("a", 1_000) for _ in range(10)])
    usages = await asyncio.gather(
        *[controller.reconcile(d.reservation_id, 3, "a") for d in decisions]
    )
    assert all(u == 30 for u in usages)
    assert await controller.usage("a") == 30
    await controller.aclose()


async def test_release_hands_the_whole_reservation_back(controller):
    """The async facade releases as completely as the synchronous path."""
    decision = await controller.try_consume("a", 10_000)
    await controller.release(decision.reservation_id)
    assert await controller.usage("a") == 0
    await controller.aclose()


async def test_grouping_actually_happens_under_concurrency(limiter):
    """Assert the optimisation is real: concurrent calls share transactions."""
    batch_sizes: list[int] = []
    original = limiter.try_consume_many

    def spy(requests):
        """Record each batch size, then delegate."""
        batch_sizes.append(len(requests))
        return original(requests)

    limiter.try_consume_many = spy
    controller = AdmissionController(limiter)
    await asyncio.gather(*[controller.try_consume("a", 1) for _ in range(64)])
    await controller.aclose()

    assert sum(batch_sizes) == 64
    assert max(batch_sizes) > 1, f"no grouping occurred: {batch_sizes}"
    assert len(batch_sizes) < 64, f"one transaction per request: {batch_sizes}"


async def test_max_batch_is_respected(limiter):
    """The configured ceiling bounds how long one transaction can hold the write lock."""
    sizes: list[int] = []
    original = limiter.try_consume_many

    def spy(requests):
        """Record each batch size, then delegate."""
        sizes.append(len(requests))
        return original(requests)

    limiter.try_consume_many = spy
    controller = AdmissionController(limiter, max_batch=4)
    await asyncio.gather(*[controller.try_consume("a", 1) for _ in range(40)])
    await controller.aclose()
    assert max(sizes) <= 4
    assert sum(sizes) == 40


# --------------------------------------------------------------------------
# The event loop must stay free
# --------------------------------------------------------------------------
async def test_event_loop_is_not_blocked_during_a_slow_database_call(limiter):
    """The regression guard for "SQLite on the event loop".

    The batch call is made deliberately slow. A heartbeat coroutine must keep
    ticking throughout; if admission ran on the loop it would not tick at all.
    """
    original = limiter.try_consume_many

    def slow(requests):
        """Simulate a 300ms database stall."""
        time.sleep(0.30)
        return original(requests)

    limiter.try_consume_many = slow
    controller = AdmissionController(limiter)

    ticks = 0
    stop = False

    async def heartbeat():
        """Tick every 10ms; the count proves the loop kept running."""
        nonlocal ticks
        while not stop:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    await controller.try_consume("a", 1)
    stop = True
    await beat
    await controller.aclose()

    # ~30 ticks are possible in 300ms; anything above a handful proves the loop ran.
    assert ticks >= 10, f"event loop only ticked {ticks} times during a 300ms DB call"


# --------------------------------------------------------------------------
# Failure and shutdown
# --------------------------------------------------------------------------
async def test_a_failing_batch_fails_every_waiter_rather_than_hanging(limiter):
    """A database failure propagates to all five waiters; none is left awaiting a future that never resolves."""
    def boom(requests):
        """Fail every batch."""
        raise RuntimeError("database is on fire")

    limiter.try_consume_many = boom
    controller = AdmissionController(limiter)
    results = await asyncio.gather(
        *[controller.try_consume("a", 1) for _ in range(5)], return_exceptions=True
    )
    assert len(results) == 5
    assert all(isinstance(r, RuntimeError) for r in results)
    await controller.aclose()


async def test_the_controller_recovers_after_a_failed_batch(limiter):
    """One failure does not kill the worker; the next request is served normally."""
    calls = {"n": 0}
    original = limiter.try_consume_many

    def flaky(requests):
        """Fail the first batch only, then behave."""
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return original(requests)

    limiter.try_consume_many = flaky
    controller = AdmissionController(limiter)
    with pytest.raises(RuntimeError):
        await controller.try_consume("a", 1)
    decision = await controller.try_consume("a", 1)     # worker must still be alive
    assert decision.allowed
    await controller.aclose()


async def test_submitting_after_close_raises_rather_than_hanging(controller):
    """Post-shutdown submissions fail fast instead of blocking forever."""
    await controller.aclose()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(controller.try_consume("a", 1), timeout=5)


async def test_aclose_is_idempotent(controller):
    """Closing twice is safe, so shutdown paths need not coordinate."""
    await controller.try_consume("a", 1)
    await controller.aclose()
    await controller.aclose()


async def test_close_without_any_traffic_is_safe(controller):
    """Shutting down a controller that never started a worker is a no-op, not an error."""
    await controller.aclose()


async def test_default_max_batch_is_sane():
    """The shipped ceiling is a real bound rather than one or unbounded."""
    assert 1 < DEFAULT_MAX_BATCH <= 1024


async def test_queue_worker_restarts_if_it_was_lost(limiter):
    """A cancelled worker must not wedge the queue permanently."""
    queue = _GroupCommitQueue(limiter.try_consume_many, "test")
    assert (await queue.submit(("a", 1))).allowed
    queue._worker.cancel()
    try:
        await queue._worker
    except asyncio.CancelledError:
        pass
    assert (await asyncio.wait_for(queue.submit(("a", 1)), timeout=5)).allowed
    await queue.aclose()
