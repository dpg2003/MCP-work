"""Per-tenant in-flight limiting: the ceiling, isolation, and bookkeeping.

These are the unit tests for :mod:`concurrency` in isolation. The tests that
prove the gateway actually *applies* the limit -- and gives the slot back on
every exit path -- live in ``test_concurrency_e2e.py``, because a limiter that is
correct but unwired is the defect this module was written to fix.
"""

from __future__ import annotations

import pytest

from concurrency import CONCURRENCY_RETRY_AFTER_SECONDS, ConcurrencyLimiter, Slot


@pytest.fixture
def gate():
    """A concurrency limiter with nothing in flight."""
    return ConcurrencyLimiter()


# --------------------------------------------------------------------------
# The ceiling
# --------------------------------------------------------------------------


def test_a_fresh_tenant_has_nothing_in_flight(gate):
    """An unseen tenant reports zero rather than raising."""
    assert gate.in_flight("tenant-a") == 0


def test_acquire_grants_a_slot_below_the_ceiling(gate):
    """The first request of a tenant with room is admitted."""
    slot = gate.try_acquire("tenant-a", max_concurrent=2)
    assert slot is not None
    assert slot.tenant == "tenant-a"


def test_acquire_reports_the_depth_including_itself(gate):
    """``Slot.in_flight`` counts the slot just granted."""
    first = gate.try_acquire("tenant-a", max_concurrent=3)
    second = gate.try_acquire("tenant-a", max_concurrent=3)
    assert (first.in_flight, second.in_flight) == (1, 2)


def test_exactly_the_ceiling_is_allowed(gate):
    """A ceiling of N admits N concurrent requests."""
    slots = [gate.try_acquire("tenant-a", max_concurrent=3) for _ in range(3)]
    assert all(slot is not None for slot in slots)
    assert gate.in_flight("tenant-a") == 3


def test_one_past_the_ceiling_is_shed(gate):
    """The N+1th concurrent request is rejected rather than queued."""
    for _ in range(3):
        gate.try_acquire("tenant-a", max_concurrent=3)
    assert gate.try_acquire("tenant-a", max_concurrent=3) is None


def test_a_shed_request_does_not_increment_the_depth(gate):
    """Rejection must not leak a count, or the tenant would never recover."""
    for _ in range(2):
        gate.try_acquire("tenant-a", max_concurrent=2)
    for _ in range(5):
        assert gate.try_acquire("tenant-a", max_concurrent=2) is None
    assert gate.in_flight("tenant-a") == 2


def test_a_ceiling_of_one_serialises_the_tenant(gate):
    """The degenerate ceiling still admits one and sheds the rest."""
    assert gate.try_acquire("tenant-a", max_concurrent=1) is not None
    assert gate.try_acquire("tenant-a", max_concurrent=1) is None


# --------------------------------------------------------------------------
# Release
# --------------------------------------------------------------------------


def test_release_frees_a_slot_for_the_next_request(gate):
    """A saturated tenant is admitted again once a request finishes."""
    slot = gate.try_acquire("tenant-a", max_concurrent=1)
    assert gate.try_acquire("tenant-a", max_concurrent=1) is None
    gate.release(slot)
    assert gate.try_acquire("tenant-a", max_concurrent=1) is not None


def test_release_of_none_is_a_no_op(gate):
    """Callers can release unconditionally in a ``finally`` with no branch."""
    gate.release(None)
    assert gate.in_flight("tenant-a") == 0


def test_release_drops_the_key_at_zero(gate):
    """An idle tenant leaves no entry, so the map cannot grow without bound."""
    slot = gate.try_acquire("tenant-a", max_concurrent=2)
    gate.release(slot)
    assert gate.snapshot() == {}


def test_release_never_drives_the_count_negative(gate):
    """A stray extra release cannot create phantom capacity."""
    slot = gate.try_acquire("tenant-a", max_concurrent=2)
    for _ in range(5):
        gate.release(slot)
    assert gate.in_flight("tenant-a") == 0
    assert gate.snapshot() == {}


def test_release_of_an_unknown_tenant_is_harmless(gate):
    """Releasing a slot the limiter never issued does not raise."""
    gate.release(Slot(tenant="tenant-ghost", in_flight=1))
    assert gate.in_flight("tenant-ghost") == 0


def test_acquire_release_cycles_do_not_drift(gate):
    """Depth returns to zero after balanced traffic."""
    for _ in range(200):
        slot = gate.try_acquire("tenant-a", max_concurrent=4)
        gate.release(slot)
    assert gate.snapshot() == {}


# --------------------------------------------------------------------------
# Isolation between tenants
# --------------------------------------------------------------------------


def test_one_tenant_saturating_does_not_shed_another(gate):
    """The whole point of per-tenant limiting: bursts stay local."""
    for _ in range(4):
        gate.try_acquire("tenant-noisy", max_concurrent=4)
    assert gate.try_acquire("tenant-noisy", max_concurrent=4) is None
    assert gate.try_acquire("tenant-quiet", max_concurrent=4) is not None


def test_each_tenant_gets_its_own_ceiling(gate):
    """Depths are tracked per tenant, not globally."""
    gate.try_acquire("tenant-a", max_concurrent=1)
    gate.try_acquire("tenant-b", max_concurrent=1)
    assert gate.snapshot() == {"tenant-a": 1, "tenant-b": 1}


def test_releasing_one_tenant_does_not_free_another(gate):
    """A release is scoped to the slot's own tenant."""
    a = gate.try_acquire("tenant-a", max_concurrent=1)
    gate.try_acquire("tenant-b", max_concurrent=1)
    gate.release(a)
    assert gate.try_acquire("tenant-b", max_concurrent=1) is None


# --------------------------------------------------------------------------
# The ceiling is read per call
# --------------------------------------------------------------------------


def test_a_raised_ceiling_takes_effect_immediately(gate):
    """An upgraded tier applies on the next request, with no rebuild."""
    gate.try_acquire("tenant-a", max_concurrent=1)
    assert gate.try_acquire("tenant-a", max_concurrent=1) is None
    assert gate.try_acquire("tenant-a", max_concurrent=2) is not None


def test_a_lowered_ceiling_sheds_without_disturbing_requests_in_flight(gate):
    """Downgrading stops new work; it does not cancel work already admitted."""
    slots = [gate.try_acquire("tenant-a", max_concurrent=4) for _ in range(3)]
    assert all(slot is not None for slot in slots)
    # Tier dropped to 2 while three are still running.
    assert gate.try_acquire("tenant-a", max_concurrent=2) is None
    assert gate.in_flight("tenant-a") == 3


# --------------------------------------------------------------------------
# Misconfiguration
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ceiling", [0, -1, -100])
def test_a_non_positive_ceiling_is_rejected_loudly(gate, ceiling):
    """"Admit nothing" is a config bug, so it raises instead of shedding."""
    with pytest.raises(ValueError, match="must be positive"):
        gate.try_acquire("tenant-a", max_concurrent=ceiling)


def test_a_rejected_ceiling_does_not_record_the_tenant(gate):
    """A raising call leaves no state behind."""
    with pytest.raises(ValueError):
        gate.try_acquire("tenant-a", max_concurrent=0)
    assert gate.snapshot() == {}


# --------------------------------------------------------------------------
# Observability
# --------------------------------------------------------------------------


def test_snapshot_is_a_copy(gate):
    """Mutating the snapshot must not corrupt the limiter's own state."""
    gate.try_acquire("tenant-a", max_concurrent=2)
    snapshot = gate.snapshot()
    snapshot["tenant-a"] = 99
    snapshot["tenant-injected"] = 1
    assert gate.in_flight("tenant-a") == 1
    assert gate.in_flight("tenant-injected") == 0


def test_retry_after_is_short_because_contention_clears_fast(gate):
    """A concurrency rejection clears in one request, not one window."""
    assert 0 < CONCURRENCY_RETRY_AFTER_SECONDS <= 5
