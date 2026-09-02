"""Primary/secondary model routing with automatic failover.

Failover policy
---------------
=================================  ==========================================
Primary outcome                     Router behaviour
=================================  ==========================================
Success                             Return it. Secondary is never called.
HTTP 429                            Fail over to secondary.
Timeout at the deadline (3000 ms)   Fail over to secondary.
Connection error / 5xx / bad body   Fail over to secondary.
Non-retryable 4xx (not 429)         Do **not** fail over; the secondary would
                                    reject the same request. Standardized
                                    error straight back.
=================================  ==========================================

Connection errors and 5xx are treated as failover-worthy alongside the two
required triggers: they mean the same thing operationally ("this provider
cannot serve the request right now") and a router that fails over on a timeout
but not on a connection refusal is a router with a hole in it. A 4xx is
different in kind — it is a statement about the *request*, so retrying it
elsewhere just burns the second provider's quota.

Statelessness is deliberate
---------------------------
There is no circuit breaker and no cached health flag. A flapping primary
therefore cannot wedge the router into a stale state: every request re-evaluates
from scratch, so recovery is immediate and the failure mode of a stuck-open
breaker does not exist. The cost is that during a sustained primary outage every
request pays one failed attempt before failing over. That is the right trade at
this scale; the note in the README says what would change it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

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

# Failures that mean "try somewhere else".
FAILOVER_ON = (ProviderTimeout, ProviderRateLimited, ProviderUnavailable)


@dataclass
class RoutedCompletion:
    completion: Completion
    provider_used: str
    failed_over: bool
    attempts: list[str]


class ModelRouter:
    def __init__(
        self,
        primary: ModelProvider,
        secondary: ModelProvider,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self.timeout_ms = timeout_ms
        for provider in (primary, secondary):
            # Providers own their own deadline so it covers connect+read, not
            # just the awaited coroutine.
            if hasattr(provider, "timeout_seconds"):
                provider.timeout_seconds = timeout_ms / 1000.0

    async def complete(
        self, prompt: str, max_tokens: int, request_id: str | None = None
    ) -> RoutedCompletion:
        attempts: list[str] = []
        first_failure: ProviderError | None = None

        try:
            completion = await self.primary.complete(prompt, max_tokens)
            attempts.append(f"{self.primary.name}:ok")
            return RoutedCompletion(completion, self.primary.name, False, attempts)
        except ProviderRejected as exc:
            attempts.append(f"{self.primary.name}:rejected")
            logger.warning("request_id=%s primary rejected: %s", request_id, exc.internal_detail)
            raise GatewayError(
                UPSTREAM_UNAVAILABLE,
                details={"attempts": attempts},
                request_id=request_id,
                internal_detail=exc.internal_detail,
            ) from exc
        except FAILOVER_ON as exc:
            first_failure = exc
            attempts.append(f"{self.primary.name}:{_reason(exc)}")
            logger.warning(
                "request_id=%s failing over from %s (%s)",
                request_id, self.primary.name, exc.internal_detail,
            )

        try:
            completion = await self.secondary.complete(prompt, max_tokens)
            attempts.append(f"{self.secondary.name}:ok")
            return RoutedCompletion(completion, self.secondary.name, True, attempts)
        except ProviderError as exc:
            attempts.append(f"{self.secondary.name}:{_reason(exc)}")
            # Both providers failed. ONE error, in the gateway's own format,
            # carrying neither provider's error body.
            logger.error(
                "request_id=%s all providers failed: primary=%s secondary=%s",
                request_id,
                getattr(first_failure, "internal_detail", None),
                exc.internal_detail,
            )
            raise GatewayError(
                UPSTREAM_UNAVAILABLE,
                details={"attempts": attempts},
                request_id=request_id,
                internal_detail=f"primary={first_failure}; secondary={exc}",
            ) from exc


def _reason(exc: ProviderError) -> str:
    """Short, gateway-owned label for an attempt. Never the upstream's text."""
    return {
        ProviderTimeout: "timeout",
        ProviderRateLimited: "rate_limited",
        ProviderUnavailable: "unavailable",
        ProviderRejected: "rejected",
    }.get(type(exc), "error")
