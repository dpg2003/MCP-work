"""Model routing: an ordered provider chain with circuit breaking and retries.

Failover policy
---------------
=================================  ==========================================
Provider outcome                    Router behaviour
=================================  ==========================================
Success                             Return it. Later providers untouched.
HTTP 429                            Retry, then advance to the next provider.
Timeout at the deadline             Retry, then advance.
Connection error / 5xx / bad body   Retry, then advance.
Non-retryable 4xx (not 429)         Stop. The request is wrong, so another
                                    provider would reject it identically.
=================================  ==========================================

Why a chain rather than primary/secondary
-----------------------------------------
Two hardcoded providers is a special case of an ordered list, and the list
costs nothing extra. ``primary`` and ``secondary`` remain as properties so
existing callers and tests are unaffected.

Why a circuit breaker
---------------------
The earlier version held no health state. That meant a provider that was hard
down was *re-attempted on every request*, so every request paid the full
3000 ms deadline before failing over -- a three-second tax on all traffic for
the duration of the outage. Each provider now has a :class:`CircuitBreaker`:
after a few consecutive failures the router skips that provider in
microseconds and spends the request on the next one, probing the dead provider
once per recovery window instead of once per request.

The breaker is bounded, per-provider state created at construction; it does not
grow with traffic. A non-retryable 4xx is recorded as *indeterminate* -- it says
nothing about provider health, so it neither trips a breaker nor clears a
failure tally.

Why retries before advancing -- and why not for timeouts
--------------------------------------------------------
A 429 or a refused connection fails in milliseconds and is often transient, so
``attempts_per_provider`` (default 2) retries the same provider once, after a
small jittered backoff, before advancing.

Timeouts are excluded (see :data:`RETRY_ON`). A timeout has already spent the
whole deadline, so retrying it doubles the client's wait -- 6 s on a 3 s budget
-- to re-ask a provider that just failed to answer. It advances immediately
instead. Worst-case latency is therefore one deadline per provider, not one per
attempt.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

from circuit_breaker import BreakerPolicy, BreakerState, CircuitBreaker
from errors import UPSTREAM_UNAVAILABLE, GatewayError
from providers import (
    Completion,
    ModelProvider,
    ProviderError,
    ProviderRateLimited,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
)

logger = logging.getLogger("fde.router")

DEFAULT_TIMEOUT_MS = 3000
DEFAULT_ATTEMPTS_PER_PROVIDER = 2
RETRY_BACKOFF_SECONDS = 0.05
RETRY_JITTER_SECONDS = 0.05

# Failures that mean "this provider cannot serve the request right now".
FAILOVER_ON = (ProviderTimeout, ProviderRateLimited, ProviderUnavailable)

# Of those, the ones worth retrying *on the same provider* before advancing.
#
# A timeout is deliberately excluded. Retrying it is the one retry that cannot
# pay for itself: the first attempt already consumed the entire deadline, so a
# second one doubles what the client waits -- 6 s on a 3 s budget -- to re-ask a
# provider that has just proven it is not answering. A 429 or a refused
# connection, by contrast, fails in milliseconds, so retrying those costs
# almost nothing and genuinely absorbs a blip.
RETRY_ON = (ProviderRateLimited, ProviderUnavailable)


@dataclass
class RoutedCompletion:
    """A completion plus how it was obtained.

    ``attempts`` records one gateway-owned label per try, for example
    ``["primary:timeout", "primary:timeout", "secondary:ok"]`` or
    ``["primary:circuit_open", "secondary:ok"]``. The labels are ours, not the
    providers', so this is safe to return to a client.
    """

    completion: Completion
    provider_used: str
    failed_over: bool
    attempts: list[str]


class ModelRouter:
    """Routes a request along an ordered provider chain.

    Holds one :class:`CircuitBreaker` per provider -- bounded state, created
    once, never growing with traffic.
    """

    def __init__(
        self,
        primary: ModelProvider | None = None,
        secondary: ModelProvider | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        providers: list[ModelProvider] | None = None,
        breaker_policy: BreakerPolicy | None = None,
        attempts_per_provider: int = DEFAULT_ATTEMPTS_PER_PROVIDER,
        clock=None,
    ) -> None:
        """Wire up the chain and push the deadline down into each provider.

        Args:
            primary: First provider. Positional for backwards compatibility.
            secondary: Second provider, if any.
            timeout_ms: Per-attempt deadline, set on each provider so it covers
                connect, write, read and pool-acquire time rather than only the
                awaited coroutine.
            providers: Full ordered chain. Supersedes ``primary``/``secondary``.
            breaker_policy: Breaker tuning, shared by every provider.
            attempts_per_provider: Tries per provider before advancing.
            clock: Monotonic clock for the breakers, injectable for tests.

        Raises:
            ValueError: No providers given, or a non-positive attempt count.
        """
        chain = list(providers) if providers is not None else [
            p for p in (primary, secondary) if p is not None
        ]
        if not chain:
            raise ValueError("ModelRouter needs at least one provider")
        if attempts_per_provider < 1:
            raise ValueError("attempts_per_provider must be at least 1")

        self.providers = chain
        self.timeout_ms = timeout_ms
        self.attempts_per_provider = attempts_per_provider
        self.breakers = {
            provider.name: CircuitBreaker(provider.name, breaker_policy, clock=clock)
            for provider in chain
        }
        for provider in chain:
            if hasattr(provider, "timeout_seconds"):
                provider.timeout_seconds = timeout_ms / 1000.0

    # -- backwards-compatible accessors -----------------------------------
    @property
    def primary(self) -> ModelProvider:
        """First provider in the chain."""
        return self.providers[0]

    @property
    def secondary(self) -> ModelProvider | None:
        """Second provider in the chain, or ``None`` for a single-provider chain."""
        return self.providers[1] if len(self.providers) > 1 else None

    # -- routing ----------------------------------------------------------
    async def complete(
        self, prompt: str, max_tokens: int, request_id: str | None = None
    ) -> RoutedCompletion:
        """Complete via the first provider that can serve the request.

        Args:
            prompt: The prompt to complete.
            max_tokens: Output budget.
            request_id: Correlation id, echoed into the logs and any error.

        Returns:
            The completion and the full attempt trail.

        Raises:
            GatewayError: A provider rejected the request non-retryably, or
                every provider in the chain failed or was circuit-open. Either
                way it is one error in the gateway's own format, carrying no
                provider detail.
        """
        attempts: list[str] = []
        first_failure: ProviderError | None = None

        for index, provider in enumerate(self.providers):
            breaker = self.breakers[provider.name]

            if not breaker.allows():
                # The whole point: no call, no socket, no deadline to wait out.
                attempts.append(f"{provider.name}:circuit_open")
                logger.info(
                    "request_id=%s skipping %s: circuit open", request_id, provider.name
                )
                continue

            outcome = await self._try_provider(provider, breaker, prompt, max_tokens,
                                               request_id, attempts)
            if isinstance(outcome, Completion):
                return RoutedCompletion(outcome, provider.name, index > 0, attempts)
            if isinstance(outcome, ProviderRejected):
                # A statement about the request, not the provider.
                logger.warning(
                    "request_id=%s %s rejected the request: %s",
                    request_id, provider.name, outcome.internal_detail,
                )
                raise GatewayError(
                    UPSTREAM_UNAVAILABLE,
                    details={"attempts": attempts},
                    request_id=request_id,
                    internal_detail=outcome.internal_detail,
                )
            if first_failure is None:
                first_failure = outcome

        logger.error(
            "request_id=%s every provider failed: attempts=%s first=%s",
            request_id, attempts, getattr(first_failure, "internal_detail", None),
        )
        raise GatewayError(
            UPSTREAM_UNAVAILABLE,
            details={"attempts": attempts},
            request_id=request_id,
            internal_detail=f"chain exhausted: {attempts}",
        )

    async def _try_provider(
        self,
        provider: ModelProvider,
        breaker: CircuitBreaker,
        prompt: str,
        max_tokens: int,
        request_id: str | None,
        attempts: list[str],
    ) -> Completion | ProviderError:
        """Attempt one provider up to ``attempts_per_provider`` times.

        Returns the :class:`Completion` on success, or the last
        :class:`ProviderError` so the caller can decide whether to advance.
        A breaker admission is consumed once per provider, so a retry does not
        need a second half-open slot.
        """
        last_error: ProviderError | None = None
        for attempt in range(self.attempts_per_provider):
            try:
                completion = await provider.complete(prompt, max_tokens)
            except ProviderRejected as exc:
                breaker.record_indeterminate()
                attempts.append(f"{provider.name}:rejected")
                return exc
            except FAILOVER_ON as exc:
                last_error = exc
                attempts.append(f"{provider.name}:{_reason(exc)}")
                logger.warning(
                    "request_id=%s %s attempt %d/%d failed (%s)",
                    request_id, provider.name, attempt + 1,
                    self.attempts_per_provider, exc.internal_detail,
                )
                retryable = isinstance(exc, RETRY_ON)
                if not retryable or attempt + 1 >= self.attempts_per_provider:
                    # Nothing more to try on this provider: stop here rather
                    # than falling through to another attempt.
                    break
                # Small jittered backoff so a retried burst does not arrive at
                # the provider in lockstep.
                await asyncio.sleep(
                    RETRY_BACKOFF_SECONDS + random.random() * RETRY_JITTER_SECONDS
                )
            else:
                breaker.record_success()
                attempts.append(f"{provider.name}:ok")
                return completion

        breaker.record_failure()
        assert last_error is not None  # the loop only exits here after a failure
        return last_error

    # -- observability ----------------------------------------------------
    def health(self) -> dict:
        """Per-provider breaker state, for a readiness or metrics endpoint."""
        snapshots = [self.breakers[p.name].snapshot() for p in self.providers]
        return {
            "providers": snapshots,
            "available": [s["provider"] for s in snapshots if s["state"] != "open"],
            "timeout_ms": self.timeout_ms,
            "attempts_per_provider": self.attempts_per_provider,
        }

    def reset_breakers(self) -> None:
        """Force every breaker closed. For operator intervention and tests."""
        for breaker in self.breakers.values():
            breaker.reset()


def _reason(exc: ProviderError) -> str:
    """Short, gateway-owned label for an attempt. Never the upstream's text."""
    return {
        ProviderTimeout: "timeout",
        ProviderRateLimited: "rate_limited",
        ProviderUnavailable: "unavailable",
        ProviderRejected: "rejected",
    }.get(type(exc), "error")
