"""Per-tenant in-flight request limiting.

Why this exists
---------------
:class:`tenants.Quota` carries three dimensions. Two of them -- tokens per
minute and requests per minute -- are enforced by the sliding-window limiter in
:mod:`rate_limiter`. The third, ``max_concurrent``, was advertised over
``/v1/quota`` and enforced nowhere, so the API documented a limit that did not
exist. This module is that limit.

The dimensions protect against different failures, which is why a window
limiter cannot stand in for this one. A token window bounds *total work over
time*; concurrency bounds *simultaneous work right now*. A tenant that opens 500
sockets at once is inside a 50,000-token minute for a while, yet it has already
taken every upstream connection in the pool and every other tenant is queued
behind it. Concurrency is the dimension that keeps one tenant's burst from
becoming everyone's latency.

Reject, do not queue
--------------------
The obvious implementation is one :class:`asyncio.Semaphore` per tenant, with
callers awaiting a slot. That is the wrong shape here, for two reasons:

* **Queueing converts a concurrency limit into unbounded latency.** A caller
  that waits is still holding a client socket and a request-handler task. Under
  a sustained burst the queue grows without bound and every request eventually
  times out -- the gateway degrades instead of shedding. Returning 429 at once
  lets the client back off and keeps the queue depth bounded by the limit.
* **A semaphore's bound is fixed at construction.** Quotas are per tenant and
  change when a tenant's tier changes, so the ceiling has to be read at
  admission time rather than baked into the primitive.

So the primitive is a counter plus a comparison, and callers are rejected rather
than parked.

Concurrency safety
------------------
:meth:`ConcurrencyLimiter.try_acquire` reads the counter and writes it back with
no ``await`` in between. On a single-threaded event loop that sequence cannot be
interleaved -- the loop only switches tasks at a suspension point -- so the
check-then-increment is atomic without a lock. This is deliberate rather than
incidental: adding an ``asyncio.Lock`` would introduce a suspension point and
make the operation genuinely racy in the window it was meant to protect. The
class is *not* safe across OS threads, and does not need to be: one gateway
worker owns one event loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("fde.concurrency")

# How long a client should wait before retrying a request shed for concurrency.
# Unlike a window rejection -- which cannot clear until the window rolls -- a
# concurrency rejection clears as soon as any in-flight request of this tenant
# finishes, which is typically a single request's latency. Advertising the
# window's remaining seconds here would park clients for far longer than the
# contention actually lasts.
CONCURRENCY_RETRY_AFTER_SECONDS = 1


@dataclass(frozen=True)
class Slot:
    """A granted concurrency slot, and the record needed to give it back.

    Attributes:
        tenant: Tenant the slot was granted to.
        in_flight: Depth *including* this slot, i.e. the value after the
            increment. Reported for observability; nothing keys off it.
    """

    tenant: str
    in_flight: int


class ConcurrencyLimiter:
    """Counts in-flight requests per tenant and sheds those over the ceiling.

    The limiter holds no quota of its own. Every call passes the ceiling that
    applies to that tenant right now, so a tier change takes effect on the next
    request rather than needing the limiter to be rebuilt.
    """

    def __init__(self) -> None:
        """Start with no tenants in flight."""
        self._in_flight: dict[str, int] = {}

    def try_acquire(self, tenant: str, max_concurrent: int) -> Slot | None:
        """Grant a slot if ``tenant`` is below its ceiling, else return ``None``.

        Args:
            tenant: Tenant identifier.
            max_concurrent: This tenant's current ceiling. Must be positive; a
                non-positive ceiling would mean "admit nothing", which is a
                misconfiguration rather than a runtime state, so it is rejected
                loudly instead of silently shedding every request.

        Returns:
            A :class:`Slot` to pass to :meth:`release`, or ``None`` when the
            tenant already has ``max_concurrent`` requests in flight.

        Raises:
            ValueError: If ``max_concurrent`` is not positive.
        """
        if max_concurrent <= 0:
            raise ValueError(f"max_concurrent must be positive, got {max_concurrent}")

        # No await between the read and the write -- see the module docstring on
        # why that is what makes this safe, and why a lock would break it.
        current = self._in_flight.get(tenant, 0)
        if current >= max_concurrent:
            logger.warning(
                "tenant=%s shed for concurrency in_flight=%d limit=%d",
                tenant,
                current,
                max_concurrent,
            )
            return None

        depth = current + 1
        self._in_flight[tenant] = depth
        return Slot(tenant=tenant, in_flight=depth)

    def release(self, slot: Slot | None) -> None:
        """Give a slot back. Accepts ``None`` so callers need no branch.

        Releasing is idempotent in the only sense that matters for a ``finally``
        block: a tenant's count never goes below zero, and the key is dropped at
        zero so an idle tenant leaves nothing behind. Without that cleanup the
        dict would grow once per tenant ever seen and never shrink.

        Args:
            slot: The slot returned by :meth:`try_acquire`, or ``None``.
        """
        if slot is None:
            return

        current = self._in_flight.get(slot.tenant, 0)
        if current <= 1:
            self._in_flight.pop(slot.tenant, None)
        else:
            self._in_flight[slot.tenant] = current - 1

    def in_flight(self, tenant: str) -> int:
        """Return how many requests ``tenant`` currently has in flight."""
        return self._in_flight.get(tenant, 0)

    def snapshot(self) -> dict[str, int]:
        """Return a copy of the per-tenant depths, for health endpoints.

        Returns a copy rather than the live dict so a caller iterating the
        result cannot be hit by a mutation from a concurrent request.
        """
        return dict(self._in_flight)
