"""Per-provider circuit breaker.

Why this exists
---------------
Without one, a router that fails over on timeout pays the *full* timeout on
every single request for as long as a provider is down. With a 3000 ms deadline
and a primary that is hard-down, that is a three-second tax on 100% of traffic,
indefinitely. The breaker converts that into one failed probe per recovery
window; every other request skips the dead provider in microseconds.

States
------
``CLOSED``     Normal. Failures are counted; ``failure_threshold`` consecutive
               failures trip the breaker open.
``OPEN``       Calls are refused immediately, with no network attempt and no
               timeout to wait out. After ``recovery_seconds`` the breaker
               moves itself to half-open on the next enquiry.
``HALF_OPEN``  A limited number of probe calls are admitted. A success closes
               the breaker; a failure re-opens it and restarts the clock.

Only *consecutive* failures count. One success in ``CLOSED`` resets the tally,
so an occasional blip never trips a healthy provider.

Not every failure is the provider's fault: a malformed request (4xx that is not
429) tells you nothing about provider health, so the router records those as
neither success nor failure.

The clock is injectable and monotonic, so recovery can be tested without
sleeping and cannot be skewed by a wall-clock adjustment.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class BreakerState(str, Enum):
    """Lifecycle state of a single provider's breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class BreakerPolicy:
    """Tuning for a breaker.

    Attributes:
        failure_threshold: Consecutive failures that trip CLOSED -> OPEN.
        recovery_seconds: How long OPEN refuses calls before probing.
        half_open_max_calls: Concurrent probes allowed while HALF_OPEN.
        success_threshold: Probe successes needed to close again.
    """

    failure_threshold: int = 5
    recovery_seconds: float = 30.0
    half_open_max_calls: int = 1
    success_threshold: int = 1

    def __post_init__(self) -> None:
        """Reject a policy that could never open or never recover."""
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if self.recovery_seconds <= 0:
            raise ValueError("recovery_seconds must be positive")
        if self.half_open_max_calls < 1:
            raise ValueError("half_open_max_calls must be at least 1")
        if self.success_threshold < 1:
            raise ValueError("success_threshold must be at least 1")


@dataclass
class BreakerStats:
    """Counters for observability. Monotonic for the breaker's lifetime."""

    calls_allowed: int = 0
    calls_refused: int = 0
    successes: int = 0
    failures: int = 0
    opened: int = 0


class CircuitBreaker:
    """Tracks one provider's health and gates calls to it."""

    def __init__(
        self,
        name: str,
        policy: BreakerPolicy | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Create a closed breaker.

        Args:
            name: Provider name, used in logs and snapshots.
            policy: Tuning; the defaults suit a user-facing gateway.
            clock: Monotonic time source, injectable for tests.
        """
        self.name = name
        self.policy = policy or BreakerPolicy()
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._half_open_successes = 0
        self._half_open_in_flight = 0
        self._opened_at = 0.0
        self.stats = BreakerStats()

    # -- state ------------------------------------------------------------
    @property
    def state(self) -> BreakerState:
        """Current state, after applying any pending recovery transition."""
        with self._lock:
            self._maybe_recover()
            return self._state

    def _maybe_recover(self) -> None:
        """Move OPEN -> HALF_OPEN once the recovery window has elapsed.

        Called under the lock from every state enquiry, so recovery is driven
        by traffic rather than by a background timer -- nothing to schedule,
        nothing to leak.
        """
        if self._state is BreakerState.OPEN:
            if self._clock() - self._opened_at >= self.policy.recovery_seconds:
                self._state = BreakerState.HALF_OPEN
                self._half_open_successes = 0
                self._half_open_in_flight = 0

    # -- gating -----------------------------------------------------------
    def allows(self) -> bool:
        """Whether a call may be attempted right now.

        Returns ``False`` in microseconds while OPEN -- that is the entire
        point: no socket, no deadline, no three-second wait.
        """
        with self._lock:
            self._maybe_recover()
            if self._state is BreakerState.CLOSED:
                self.stats.calls_allowed += 1
                return True
            if self._state is BreakerState.HALF_OPEN:
                if self._half_open_in_flight < self.policy.half_open_max_calls:
                    self._half_open_in_flight += 1
                    self.stats.calls_allowed += 1
                    return True
                self.stats.calls_refused += 1
                return False
            self.stats.calls_refused += 1
            return False

    def record_success(self) -> None:
        """Register a successful call."""
        with self._lock:
            self.stats.successes += 1
            if self._state is BreakerState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                self._half_open_successes += 1
                if self._half_open_successes >= self.policy.success_threshold:
                    self._state = BreakerState.CLOSED
                    self._consecutive_failures = 0
                    self._half_open_successes = 0
            else:
                # One success clears the tally, so a blip never trips a
                # provider that is otherwise healthy.
                self._consecutive_failures = 0

    def record_failure(self) -> None:
        """Register a failed call, tripping or re-opening the breaker if due."""
        with self._lock:
            self.stats.failures += 1
            if self._state is BreakerState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                self._trip()
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.policy.failure_threshold:
                self._trip()

    def _trip(self) -> None:
        """Open the breaker and restart the recovery clock. Lock held."""
        if self._state is not BreakerState.OPEN:
            self.stats.opened += 1
        self._state = BreakerState.OPEN
        self._opened_at = self._clock()
        self._half_open_successes = 0
        self._half_open_in_flight = 0

    def record_indeterminate(self) -> None:
        """Register a call whose outcome says nothing about provider health.

        A non-retryable 4xx is about the *request*, not the provider, so it
        must neither trip the breaker nor clear an existing failure tally.
        """
        with self._lock:
            if self._state is BreakerState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)

    def reset(self) -> None:
        """Force the breaker closed. For operator intervention and tests."""
        with self._lock:
            self._state = BreakerState.CLOSED
            self._consecutive_failures = 0
            self._half_open_successes = 0
            self._half_open_in_flight = 0

    # -- observability ----------------------------------------------------
    def snapshot(self) -> dict:
        """JSON-safe view of this breaker, for a health endpoint."""
        with self._lock:
            self._maybe_recover()
            retry_in = 0.0
            if self._state is BreakerState.OPEN:
                elapsed = self._clock() - self._opened_at
                retry_in = max(0.0, round(self.policy.recovery_seconds - elapsed, 3))
            return {
                "provider": self.name,
                "state": self._state.value,
                "consecutive_failures": self._consecutive_failures,
                "retry_in_seconds": retry_in,
                "calls_allowed": self.stats.calls_allowed,
                "calls_refused": self.stats.calls_refused,
                "successes": self.stats.successes,
                "failures": self.stats.failures,
                "times_opened": self.stats.opened,
            }
