"""Group-commit front-end for the rate limiter.

The problem
-----------
Calling SQLite straight from a coroutine blocks the event loop -- one commit
spike stalls every in-flight request on the worker. The obvious fix,
``asyncio.to_thread`` per call, trades that for a different problem: the thread
hop costs roughly 260us on this machine, against 54us of actual database work.
Per-request hops make the limiter *slower* than blocking the loop, just politer.

The fix
-------
Group commit, the same technique a database WAL uses. Concurrent admissions are
queued; a single worker task drains everything pending, hands the whole group to
one thread, and that thread decides the batch inside one transaction. A group of
64 requests pays one thread hop and one commit rather than 64 of each.

Throughput therefore *improves* with concurrency instead of collapsing, and the
event loop stays free the whole time.

Semantics are unchanged
-----------------------
:meth:`RateLimiter.try_consume_many` decides a batch in arrival order, each
request against the running total including everything admitted earlier in the
same batch. That is exactly what sequential ``try_consume`` calls produce, so
batching cannot admit a request the serial path would have rejected. The test
suite asserts this against a randomised serial oracle.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from rate_limiter import Decision, RateLimiter

logger = logging.getLogger("fde.admission")

# Upper bound on how many requests are folded into one transaction. Large enough
# to amortise the hop and the commit, small enough that one group cannot hold
# the write lock for an unbounded time.
DEFAULT_MAX_BATCH = 128


@dataclass
class _Pending:
    """One queued unit of work and the future waiting on its result."""

    payload: Any
    future: asyncio.Future


class _GroupCommitQueue:
    """Drains a queue and applies each drained group in a single batch call."""

    def __init__(self, apply_batch, name: str, max_batch: int = DEFAULT_MAX_BATCH) -> None:
        """Store the batch function; the worker starts lazily on first use."""
        self._apply_batch = apply_batch
        self._name = name
        self._max_batch = max_batch
        self._queue: asyncio.Queue[_Pending] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._closed = False

    def _ensure_worker(self) -> None:
        """Start the drain loop on the running event loop, once."""
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._run())

    async def submit(self, payload: Any) -> Any:
        """Queue ``payload`` and await its result."""
        if self._closed:
            raise RuntimeError(f"{self._name} queue is closed")
        self._ensure_worker()
        future = asyncio.get_running_loop().create_future()
        await self._queue.put(_Pending(payload, future))
        return await future

    async def _run(self) -> None:
        """Drain the queue forever, applying each group as one batch."""
        while not self._closed:
            try:
                first = await self._queue.get()
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                return
            group = [first]
            # Take everything already queued. Nothing is *waited* for: a lone
            # request dispatches immediately, so latency is never traded for
            # batching that might not arrive.
            while len(group) < self._max_batch and not self._queue.empty():
                group.append(self._queue.get_nowait())

            payloads = [item.payload for item in group]
            try:
                results = await asyncio.to_thread(self._apply_batch, payloads)
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                for item in group:
                    if not item.future.done():
                        item.future.set_exception(RuntimeError("admission queue closed"))
                raise
            except Exception as exc:  # noqa: BLE001 - propagated to every waiter
                logger.exception("%s batch failed for %d items", self._name, len(group))
                for item in group:
                    if not item.future.done():
                        item.future.set_exception(exc)
                continue

            for item, result in zip(group, results):
                if not item.future.done():
                    item.future.set_result(result)

    async def aclose(self) -> None:
        """Stop the worker, failing anything still queued rather than hanging it."""
        self._closed = True
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except BaseException:  # pragma: no cover - shutdown
                pass
            self._worker = None
        while not self._queue.empty():  # pragma: no cover - shutdown race
            pending = self._queue.get_nowait()
            if not pending.future.done():
                pending.future.set_exception(RuntimeError("admission queue closed"))


class AdmissionController:
    """Async facade over a :class:`RateLimiter`, with group commit.

    Drop-in for the limiter's async methods; the app talks only to this.
    """

    def __init__(self, limiter: RateLimiter, max_batch: int = DEFAULT_MAX_BATCH) -> None:
        """Wrap ``limiter``. Worker tasks start lazily, so this is import-safe."""
        self.limiter = limiter
        self._admissions = _GroupCommitQueue(limiter.try_consume_many, "admission", max_batch)
        self._reconciles = _GroupCommitQueue(limiter.reconcile_many, "reconcile", max_batch)

    async def try_consume(self, tenant: str, tokens: int) -> Decision:
        """Admit or reject one request, batched with whatever else is pending."""
        return await self._admissions.submit((tenant, tokens))

    async def reconcile(self, reservation_id: int, actual_tokens: int,
                        tenant: str | None = None) -> int | None:
        """Correct one reservation, batched with whatever else is pending."""
        return await self._reconciles.submit((reservation_id, actual_tokens, tenant))

    async def release(self, reservation_id: int) -> None:
        """Hand a whole reservation back."""
        await self.reconcile(reservation_id, 0)

    async def usage(self, tenant: str) -> int:
        """Current in-window usage. Off-loop, but not batched -- it is rare."""
        return await asyncio.to_thread(self.limiter.usage, tenant)

    async def aclose(self) -> None:
        """Shut both worker tasks down."""
        await self._admissions.aclose()
        await self._reconciles.aclose()

    @property
    def limit_tokens(self) -> int:
        """The per-tenant token budget."""
        return self.limiter.limit_tokens

    @property
    def window_seconds(self) -> float:
        """The sliding window length in seconds."""
        return self.limiter.window_seconds
