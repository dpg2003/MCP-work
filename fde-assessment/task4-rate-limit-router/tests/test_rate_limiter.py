"""Sliding-window token limiter: budgets, boundaries, eviction, persistence, races."""

from __future__ import annotations

import threading

import pytest

from rate_limiter import DEFAULT_LIMIT_TOKENS, RateLimiter


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------
def test_requests_under_the_budget_all_succeed(limiter):
    """Ten admissions inside the budget, with the window totalling exactly what was consumed."""
    for _ in range(10):
        assert limiter.try_consume("acme", 4_000).allowed
    assert limiter.usage("acme") == 40_000


def test_request_that_pushes_over_the_budget_is_rejected(limiter):
    """A rejection reports the usage and limit, and crucially records nothing."""
    assert limiter.try_consume("acme", 49_000).allowed
    decision = limiter.try_consume("acme", 2_000)
    assert not decision.allowed
    assert decision.used_tokens == 49_000
    assert decision.limit_tokens == DEFAULT_LIMIT_TOKENS
    assert decision.retry_after_seconds is not None
    # A rejected request must not be recorded.
    assert limiter.usage("acme") == 49_000


def test_exactly_the_limit_is_allowed_and_one_more_is_not(limiter):
    """The stated boundary: 50,000 passes, one more token does not."""
    assert limiter.try_consume("acme", 50_000).allowed
    assert limiter.usage("acme") == 50_000
    assert not limiter.try_consume("acme", 1).allowed


def test_a_single_request_of_50001_is_rejected(limiter):
    """A request larger than the whole budget is refused on its own, not partially admitted."""
    assert not limiter.try_consume("acme", 50_001).allowed
    assert limiter.usage("acme") == 0


def test_incremental_fill_to_the_exact_boundary(limiter):
    """Reaching the limit in five steps behaves the same as reaching it in one."""
    for _ in range(5):
        assert limiter.try_consume("acme", 10_000).allowed
    assert limiter.usage("acme") == 50_000
    assert not limiter.try_consume("acme", 1).allowed


def test_zero_token_request_is_allowed_at_the_limit(limiter):
    """A zero-cost request is admissible even at the limit, since it consumes nothing."""
    limiter.try_consume("acme", 50_000)
    assert limiter.try_consume("acme", 0).allowed


def test_negative_tokens_rejected(limiter):
    """A negative cost would refund budget through the admission path and is refused outright."""
    with pytest.raises(ValueError):
        limiter.try_consume("acme", -1)


# --------------------------------------------------------------------------
# Sliding window
# --------------------------------------------------------------------------
def test_old_usage_is_evicted_when_the_window_slides(limiter, clock):
    """Past the window, a fully exhausted tenant regains its whole budget."""
    assert limiter.try_consume("acme", 50_000).allowed
    assert not limiter.try_consume("acme", 1).allowed

    clock.advance(61)
    assert limiter.try_consume("acme", 50_000).allowed
    assert limiter.usage("acme") == 50_000


def test_window_is_sliding_not_fixed(limiter, clock):
    """Usage ages out gradually, not all at once on a calendar boundary."""
    limiter.try_consume("acme", 25_000)
    clock.advance(30)
    limiter.try_consume("acme", 25_000)
    assert limiter.usage("acme") == 50_000
    assert not limiter.try_consume("acme", 1).allowed

    clock.advance(31)  # only the first entry has aged out
    assert limiter.usage("acme") == 25_000
    assert limiter.try_consume("acme", 25_000).allowed
    assert not limiter.try_consume("acme", 1).allowed


def test_boundary_of_the_window_is_exclusive(limiter, clock):
    """Exactly one window later the entry is gone, fixing the eviction boundary."""
    limiter.try_consume("acme", 50_000)
    clock.advance(60.0)  # exactly one window later: the entry is evicted
    assert limiter.usage("acme") == 0
    assert limiter.try_consume("acme", 50_000).allowed


def test_just_inside_the_window_still_counts(limiter, clock):
    """A hair inside the window still counts, fixing the boundary from the other side."""
    limiter.try_consume("acme", 50_000)
    clock.advance(59.9)
    assert not limiter.try_consume("acme", 1).allowed


def test_expired_rows_are_actually_deleted_not_just_ignored(limiter, clock):
    """Eviction physically removes rows rather than filtering them, so the table cannot grow without bound."""
    for _ in range(20):
        limiter.try_consume("acme", 100)
    assert limiter.row_count() == 20
    clock.advance(61)
    limiter.try_consume("acme", 100)
    assert limiter.row_count() == 1, "old rows must be evicted, not accumulated"


def test_retry_after_points_at_when_capacity_frees_up(limiter, clock):
    """The retry hint is computed from when enough usage actually ages out, not a fixed guess."""
    limiter.try_consume("acme", 30_000)
    clock.advance(10)
    limiter.try_consume("acme", 20_000)
    decision = limiter.try_consume("acme", 10_000)
    assert not decision.allowed
    # The first 30k entry ages out 50s from now; that is enough to fit 10k.
    assert decision.retry_after_seconds == pytest.approx(50.0, abs=0.01)


# --------------------------------------------------------------------------
# Tenant isolation
# --------------------------------------------------------------------------
def test_tenants_have_independent_budgets(limiter):
    """One tenant exhausting its budget leaves another's untouched."""
    assert limiter.try_consume("acme", 50_000).allowed
    assert not limiter.try_consume("acme", 1).allowed
    # Globex is untouched.
    assert limiter.try_consume("globex", 50_000).allowed
    assert limiter.usage("globex") == 50_000
    assert limiter.usage("acme") == 50_000


def test_one_tenant_exhausting_the_window_does_not_evict_another(limiter, clock):
    """Eviction is global housekeeping but must never drop a different tenant's live usage."""
    limiter.try_consume("acme", 40_000)
    limiter.try_consume("globex", 40_000)
    clock.advance(30)
    assert limiter.usage("acme") == 40_000
    assert limiter.usage("globex") == 40_000
    assert not limiter.try_consume("acme", 20_000).allowed
    assert not limiter.try_consume("globex", 20_000).allowed


# --------------------------------------------------------------------------
# Persistence across process restarts
# --------------------------------------------------------------------------
def test_state_survives_a_restart(db_path, clock):
    """A fresh limiter object on the same file enforces against the usage already recorded."""
    first = RateLimiter(db_path=db_path, clock=clock)
    assert first.try_consume("acme", 45_000).allowed
    first.close()

    # A brand new limiter object with empty in-memory state, same file.
    second = RateLimiter(db_path=db_path, clock=clock)
    try:
        assert second.usage("acme") == 45_000, "usage reset to zero after restart"
        assert not second.try_consume("acme", 10_000).allowed
        assert second.try_consume("acme", 5_000).allowed
    finally:
        second.close()


def test_state_survives_a_real_subprocess_restart(db_path):
    """Belt and braces: a genuinely separate interpreter, not just a new object."""
    import subprocess
    import sys
    import textwrap
    from pathlib import Path

    project_root = str(Path(__file__).resolve().parent.parent)
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {project_root!r})
        from rate_limiter import RateLimiter
        limiter = RateLimiter(db_path={db_path!r}, clock=lambda: 1_700_000_000.0)
        print(limiter.try_consume(sys.argv[1], int(sys.argv[2])).allowed)
        print(limiter.usage(sys.argv[1]))
        """
    )
    def run(tenant, tokens):
        """Run one admission attempt in a separate interpreter."""
        result = subprocess.run(
            [sys.executable, "-c", script, tenant, str(tokens)],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.split()

    assert run("acme", 45_000) == ["True", "45000"]
    assert run("acme", 10_000) == ["False", "45000"]
    assert run("acme", 5_000) == ["True", "50000"]


def test_persisted_entries_still_expire_after_a_restart(db_path, clock):
    """Persistence does not freeze the clock: restored entries still age out normally."""
    first = RateLimiter(db_path=db_path, clock=clock)
    first.try_consume("acme", 50_000)
    first.close()

    clock.advance(61)
    second = RateLimiter(db_path=db_path, clock=clock)
    try:
        assert second.usage("acme") == 0
        assert second.try_consume("acme", 50_000).allowed
    finally:
        second.close()


# --------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------
def test_concurrent_requests_never_exceed_the_limit(limiter):
    """The check-then-write race: without a transaction this over-admits."""
    results: list[bool] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(20)

    def attempt():
        """Consume 5,000 tokens, releasing all threads together first."""
        barrier.wait()  # maximise the overlap
        decision = limiter.try_consume("acme", 5_000)
        with results_lock:
            results.append(decision.allowed)

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(results) == 10, f"expected exactly 10 admissions, got {sum(results)}"
    assert limiter.usage("acme") == 50_000
    assert limiter.usage("acme") <= limiter.limit_tokens


def test_independent_limiter_instances_share_one_budget(db_path):
    """Four limiter objects on one database file, hammered concurrently.

    This is the multi-worker case: each instance has its own connection and its
    own process-local lock, so only the BEGIN IMMEDIATE transaction is keeping
    them honest.
    """
    limiters = [RateLimiter(db_path=db_path, clock=lambda: 1_700_000_000.0) for _ in range(4)]
    try:
        admitted: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(40)

        def attempt(index: int):
            """Consume 2,500 tokens through one of the limiter instances."""
            barrier.wait()
            decision = limiters[index % len(limiters)].try_consume("acme", 2_500)
            with lock:
                admitted.append(decision.allowed)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(admitted) == 20
        assert limiters[0].usage("acme") == 50_000
    finally:
        for instance in limiters:
            instance.close()


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------
def test_reconcile_corrects_an_over_estimate(limiter):
    """The reservation is corrected down to the real cost once the provider answers."""
    decision = limiter.try_consume("acme", 10_000)
    assert limiter.usage("acme") == 10_000
    limiter.reconcile(decision.reservation_id, 1_200)
    assert limiter.usage("acme") == 1_200


def test_release_gives_the_whole_reservation_back(limiter):
    """A request that produced nothing returns its entire reservation to the budget."""
    decision = limiter.try_consume("acme", 10_000)
    limiter.release(decision.reservation_id)
    assert limiter.usage("acme") == 0


def test_reconcile_does_not_disturb_other_rows(limiter):
    """Correcting one reservation leaves every other row untouched."""
    first = limiter.try_consume("acme", 10_000)
    limiter.try_consume("acme", 10_000)
    limiter.reconcile(first.reservation_id, 0)
    assert limiter.usage("acme") == 10_000
