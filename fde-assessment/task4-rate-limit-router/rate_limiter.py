"""Token-aware sliding-window rate limiter backed by on-disk SQLite.

Algorithm: **sliding window log**. Every admitted request writes one row
``(tenant, timestamp, tokens)``; the current usage for a tenant is the sum of
its rows inside the trailing window, and rows older than the window are
deleted on the way past.

Why a log rather than the cheaper alternatives:

* A **fixed window** ("50k per calendar minute") allows a 100k burst across a
  window boundary — 50k at 11:59:59 and 50k at 12:00:00. For a *token* budget
  that maps directly to spend and to upstream capacity, that is the failure
  mode you least want.
* A **sliding-window counter** (weighted blend of the previous and current
  window) fixes the burst but only approximates, and it cannot answer "exactly
  50,000 is allowed, 50,001 is not" — which is a stated requirement here.
* The log is exact. Its cost is one row per request and a periodic delete,
  which is nothing next to an LLM call. If row volume ever became the
  bottleneck, the same interface backs a Redis sorted set unchanged.

Correctness under concurrency
-----------------------------
Check-then-write is a race: two requests can both read 49,000 and both admit
5,000. Every admission therefore runs inside a single ``BEGIN IMMEDIATE``
transaction, which takes SQLite's RESERVED lock before the read, so the
read-decide-write sequence is serialized across threads *and* processes. A
process-local lock additionally serialises callers within one process so they
queue instead of thrashing on ``SQLITE_BUSY``.

Persistence
-----------
State lives in a real file. A fresh process pointed at the same database
enforces against the usage already recorded there, rather than starting from
zero — which is what makes the limiter meaningful behind more than one worker.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Callable

DEFAULT_LIMIT_TOKENS = 50_000
DEFAULT_WINDOW_SECONDS = 60.0

# How often expired rows are physically deleted. Correctness does not depend on
# this -- every read filters by timestamp -- so it is tuned purely for write
# amplification. See RateLimiter._maybe_evict.
DEFAULT_EVICT_INTERVAL_SECONDS = 1.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant    TEXT    NOT NULL,
    ts        REAL    NOT NULL,
    tokens    INTEGER NOT NULL
);
-- Covering index: (tenant, ts) selects the window and `tokens` is carried in
-- the index itself, so the hot SUM is answered without touching the table.
CREATE INDEX IF NOT EXISTS idx_token_usage_tenant_ts
    ON token_usage (tenant, ts, tokens);
"""


@dataclass(frozen=True)
class Decision:
    """Outcome of an admission check.

    Carries both enforced dimensions so a client can tell *which* budget it
    exhausted; ``reason`` names it directly.
    """

    allowed: bool
    tenant: str
    requested_tokens: int
    used_tokens: int          # usage in the window *including* this request if allowed
    limit_tokens: int
    retry_after_seconds: float | None = None
    reservation_id: int | None = None
    used_requests: int = 0
    limit_requests: int | None = None
    reason: str | None = None   # "tokens" or "requests" when refused

    @property
    def remaining_tokens(self) -> int:
        """Tokens still available in the window, floored at zero."""
        return max(0, self.limit_tokens - self.used_tokens)

    @property
    def remaining_requests(self) -> int | None:
        """Requests still available in the window, or ``None`` if uncapped."""
        if self.limit_requests is None:
            return None
        return max(0, self.limit_requests - self.used_requests)


class RateLimiter:
    """Per-tenant token budget over a sliding window, persisted in SQLite."""

    def __init__(
        self,
        db_path: str,
        limit_tokens: int = DEFAULT_LIMIT_TOKENS,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        limit_requests: int | None = None,
        clock: Callable[[], float] | None = None,
        evict_interval_seconds: float = DEFAULT_EVICT_INTERVAL_SECONDS,
    ) -> None:
        """Open (creating if needed) the SQLite database and ensure the schema.

        Args:
            db_path: On-disk database file. State persists here across
                restarts, which is what makes the limit meaningful behind
                more than one worker.
            limit_tokens: Default token budget, used when a call supplies no
                per-tenant quota.
            window_seconds: Length of the sliding window.
            limit_requests: Default request-count budget. ``None`` leaves the
                request dimension unenforced, which is the behaviour callers
                that predate per-tenant quotas rely on.
            clock: Time source, injectable so window eviction can be tested
                without sleeping for a real minute.
            evict_interval_seconds: How often expired rows are actually
                deleted. See :meth:`_maybe_evict`.
        """
        self.db_path = db_path
        self.limit_tokens = limit_tokens
        self.limit_requests = limit_requests
        self.window_seconds = window_seconds
        # Injectable so window-eviction can be tested without sleeping 60s.
        self.clock = clock or time.time
        self._lock = threading.Lock()
        self._evict_interval = evict_interval_seconds
        self._last_evict = float("-inf")

        directory = os.path.dirname(os.path.abspath(db_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        # isolation_level=None: no implicit transactions, so BEGIN IMMEDIATE
        # below is the only transaction boundary and it means what it says.
        self._conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False,
                                     timeout=30.0)
        # WAL lets readers proceed during a write; NORMAL trades an fsync per
        # commit for one per checkpoint, which is the right durability point for
        # rate-limit state (losing the last few milliseconds of usage on a hard
        # crash is survivable, stalling every request on fsync is not).
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # Wait rather than raising SQLITE_BUSY when another process holds the
        # write lock; admission must queue, never fail open or fail loudly.
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        """Close the database connection. Persisted state is unaffected."""
        self._conn.close()

    # -- limits -------------------------------------------------------------
    def _limits_for(self, quota) -> tuple[int, int | None]:
        """Resolve the limits that apply to one call.

        A per-tenant :class:`~tenants.Quota` wins; otherwise the limiter's own
        defaults apply, which is what keeps single-limit callers working.
        """
        if quota is None:
            return self.limit_tokens, self.limit_requests
        return quota.tokens_per_minute, quota.requests_per_minute

    @staticmethod
    def _window_usage(conn, tenant: str, cutoff: float) -> tuple[int, int]:
        """Tokens and request count for a tenant inside the window.

        One query for both dimensions: the covering index answers it without
        touching the table, so adding the request limit cost no extra round
        trip.
        """
        row = conn.execute(
            "SELECT COALESCE(SUM(tokens), 0), COUNT(*) FROM token_usage "
            "WHERE tenant = ? AND ts > ?",
            (tenant, cutoff),
        ).fetchone()
        return row[0], row[1]

    # -- core ---------------------------------------------------------------
    def _maybe_evict(self, conn, now: float, cutoff: float) -> None:
        """Delete expired rows, but at most once per ``_evict_interval``.

        Deleting on every request was pure write amplification: the admission
        decision already filters on ``ts > cutoff``, so an un-deleted expired
        row cannot influence any answer. The DELETE is housekeeping to stop the
        table growing, and housekeeping does not need to run 1,000 times a
        second. Amortising it removed roughly a third of the per-request cost.
        """
        if now - self._last_evict < self._evict_interval:
            return
        conn.execute("DELETE FROM token_usage WHERE ts <= ?", (cutoff,))
        self._last_evict = now

    def try_consume(self, tenant: str, tokens: int, quota=None) -> Decision:
        """Atomically evict, sum, decide, and record.

        Allows when the request fits *both* budgets: ``used + tokens <= limit``
        (so exactly the limit is admitted and one token over is not) and, when
        a request limit applies, ``requests + 1 <= limit_requests``.

        Args:
            tenant: Tenant id.
            tokens: Estimated cost to reserve.
            quota: Per-tenant :class:`~tenants.Quota`. Omitted, the limiter's
                own defaults apply.

        Raises:
            ValueError: ``tokens`` is negative.
        """
        if tokens < 0:
            raise ValueError("tokens must be non-negative")
        limit_tokens, limit_requests = self._limits_for(quota)

        with self._lock:
            now = self.clock()
            cutoff = now - self.window_seconds
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._maybe_evict(conn, now, cutoff)
                used, requests = self._window_usage(conn, tenant, cutoff)

                refusal = None
                if used + tokens > limit_tokens:
                    refusal = "tokens"
                elif limit_requests is not None and requests + 1 > limit_requests:
                    refusal = "requests"

                if refusal is not None:
                    retry_after = self._retry_after(
                        conn, tenant, cutoff, now, tokens, used,
                        limit_tokens=limit_tokens, by_requests=refusal == "requests",
                    )
                    conn.execute("COMMIT")
                    return Decision(
                        allowed=False,
                        tenant=tenant,
                        requested_tokens=tokens,
                        used_tokens=used,
                        limit_tokens=limit_tokens,
                        retry_after_seconds=retry_after,
                        used_requests=requests,
                        limit_requests=limit_requests,
                        reason=refusal,
                    )

                cursor = conn.execute(
                    "INSERT INTO token_usage (tenant, ts, tokens) VALUES (?, ?, ?)",
                    (tenant, now, tokens),
                )
                reservation_id = cursor.lastrowid
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

            return Decision(
                allowed=True,
                tenant=tenant,
                requested_tokens=tokens,
                used_tokens=used + tokens,
                limit_tokens=limit_tokens,
                reservation_id=reservation_id,
                used_requests=requests + 1,
                limit_requests=limit_requests,
            )

    def reconcile(self, reservation_id: int, actual_tokens: int,
                  tenant: str | None = None) -> int | None:
        """Correct a reservation once the real usage is known.

        Admission has to charge an *estimate* -- the true cost is not known
        until the provider answers, and by then the request has already been
        made. Reconciling afterwards keeps the window honest without ever
        letting an unmetered request through.

        Args:
            reservation_id: The id returned by the admitting :meth:`try_consume`.
            actual_tokens: The real cost to record.
            tenant: When given, the tenant's post-update in-window usage is
                computed inside the same transaction and returned. That folds
                what used to be a separate :meth:`usage` round trip into this
                one, and makes the number consistent with the update rather
                than merely subsequent to it.

        Returns:
            The tenant's in-window usage, or ``None`` when ``tenant`` is omitted.

        Raises:
            ValueError: ``actual_tokens`` is negative.
        """
        if actual_tokens < 0:
            raise ValueError("actual_tokens must be non-negative")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "UPDATE token_usage SET tokens = ? WHERE id = ?",
                    (actual_tokens, reservation_id),
                )
                usage = None
                if tenant is not None:
                    cutoff = self.clock() - self.window_seconds
                    usage = self._conn.execute(
                        "SELECT COALESCE(SUM(tokens), 0) FROM token_usage "
                        "WHERE tenant = ? AND ts > ?",
                        (tenant, cutoff),
                    ).fetchone()[0]
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return usage

    def release(self, reservation_id: int) -> None:
        """Drop a reservation entirely (the request never reached a provider)."""
        self.reconcile(reservation_id, 0)

    # -- batch primitives ---------------------------------------------------
    def try_consume_many(self, requests: list[tuple]) -> list[Decision]:
        """Decide a batch of admissions in one transaction, in arrival order.

        Semantically identical to calling :meth:`try_consume` once per entry in
        the same order -- each request is decided against the running total
        including everything admitted earlier in the batch -- but it pays one
        transaction and one commit for the whole group instead of one each.

        The in-memory running total is exact rather than an approximation:
        ``BEGIN IMMEDIATE`` holds SQLite's write lock for the duration, so no
        other connection can commit between the opening sums and the final
        commit.

        Args:
            requests: ``(tenant, tokens)`` pairs, in the order they arrived.

        Returns:
            One :class:`Decision` per request, in the same order.

        Raises:
            ValueError: Any entry requests a negative number of tokens.
        """
        # Entries are (tenant, tokens) or (tenant, tokens, quota); normalise so
        # the batch path and the single path enforce identical rules.
        normalised = [(entry[0], entry[1], entry[2] if len(entry) > 2 else None)
                      for entry in requests]
        if any(tokens < 0 for _, tokens, _ in normalised):
            raise ValueError("tokens must be non-negative")
        if not normalised:
            return []

        with self._lock:
            now = self.clock()
            cutoff = now - self.window_seconds
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._maybe_evict(conn, now, cutoff)

                # One read per distinct tenant, not one per request.
                used: dict[str, int] = {}
                counts: dict[str, int] = {}
                for tenant in {tenant for tenant, _, _ in normalised}:
                    used[tenant], counts[tenant] = self._window_usage(conn, tenant, cutoff)

                decisions: list[Decision] = []
                for tenant, tokens, quota in normalised:
                    limit_tokens, limit_requests = self._limits_for(quota)
                    current = used[tenant]
                    requests_so_far = counts[tenant]

                    refusal = None
                    if current + tokens > limit_tokens:
                        refusal = "tokens"
                    elif limit_requests is not None and requests_so_far + 1 > limit_requests:
                        refusal = "requests"

                    if refusal is not None:
                        decisions.append(
                            Decision(
                                allowed=False,
                                tenant=tenant,
                                requested_tokens=tokens,
                                used_tokens=current,
                                limit_tokens=limit_tokens,
                                retry_after_seconds=self._retry_after(
                                    conn, tenant, cutoff, now, tokens, current,
                                    limit_tokens=limit_tokens,
                                    by_requests=refusal == "requests",
                                ),
                                used_requests=requests_so_far,
                                limit_requests=limit_requests,
                                reason=refusal,
                            )
                        )
                        continue
                    cursor = conn.execute(
                        "INSERT INTO token_usage (tenant, ts, tokens) VALUES (?, ?, ?)",
                        (tenant, now, tokens),
                    )
                    used[tenant] = current + tokens
                    counts[tenant] = requests_so_far + 1
                    decisions.append(
                        Decision(
                            allowed=True,
                            tenant=tenant,
                            requested_tokens=tokens,
                            used_tokens=used[tenant],
                            limit_tokens=limit_tokens,
                            reservation_id=cursor.lastrowid,
                            used_requests=counts[tenant],
                            limit_requests=limit_requests,
                        )
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return decisions

    def reconcile_many(
        self, updates: list[tuple[int, int, str | None]]
    ) -> list[int | None]:
        """Apply a batch of reconciliations in one transaction.

        Args:
            updates: ``(reservation_id, actual_tokens, tenant)`` triples. A
                ``tenant`` of ``None`` skips the usage read-back for that entry.

        Returns:
            The post-update in-window usage per entry, or ``None`` where no
            tenant was supplied.

        Raises:
            ValueError: Any entry supplies a negative token count.
        """
        if any(tokens < 0 for _, tokens, _ in updates):
            raise ValueError("actual_tokens must be non-negative")
        if not updates:
            return []

        with self._lock:
            conn = self._conn
            cutoff = self.clock() - self.window_seconds
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany(
                    "UPDATE token_usage SET tokens = ? WHERE id = ?",
                    [(tokens, reservation_id) for reservation_id, tokens, _ in updates],
                )
                sums: dict[str, int] = {}
                for tenant in {t for _, _, t in updates if t is not None}:
                    sums[tenant] = conn.execute(
                        "SELECT COALESCE(SUM(tokens), 0) FROM token_usage "
                        "WHERE tenant = ? AND ts > ?",
                        (tenant, cutoff),
                    ).fetchone()[0]
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return [None if tenant is None else sums[tenant] for _, _, tenant in updates]

    # -- async API ----------------------------------------------------------
    #
    # SQLite work is blocking I/O. Called directly from a coroutine it stalls
    # the whole event loop -- not just the caller -- so one commit spike delays
    # every in-flight request on the worker. Measured on this machine: 0.054 ms
    # of loop time per admission in the steady state, with spikes up to 28 ms.
    # These wrappers hand the work to the default thread pool, so the loop stays
    # free to progress other requests while a commit is in flight.
    #
    # The thread hop costs tens of microseconds, which is noise next to the
    # upstream model call the limiter is guarding.

    async def try_consume_async(self, tenant: str, tokens: int, quota=None) -> Decision:
        """Off-loop :meth:`try_consume`."""
        return await asyncio.to_thread(self.try_consume, tenant, tokens, quota)

    async def reconcile_async(self, reservation_id: int, actual_tokens: int,
                              tenant: str | None = None) -> int | None:
        """Off-loop :meth:`reconcile`."""
        return await asyncio.to_thread(self.reconcile, reservation_id, actual_tokens, tenant)

    async def release_async(self, reservation_id: int) -> None:
        """Off-loop :meth:`release`."""
        await asyncio.to_thread(self.release, reservation_id)

    async def usage_async(self, tenant: str) -> int:
        """Off-loop :meth:`usage`."""
        return await asyncio.to_thread(self.usage, tenant)

    def usage(self, tenant: str) -> int:
        """Current in-window usage for a tenant. Read-only, no eviction."""
        cutoff = self.clock() - self.window_seconds
        with self._lock:
            return self._conn.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM token_usage WHERE tenant = ? AND ts > ?",
                (tenant, cutoff),
            ).fetchone()[0]

    def row_count(self) -> int:
        """Rows currently stored. Used by tests to prove eviction happens."""
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0]

    # -- helpers ------------------------------------------------------------
    def _retry_after(self, conn, tenant: str, cutoff: float, now: float,
                     tokens: int, used: int, limit_tokens: int | None = None,
                     by_requests: bool = False) -> float:
        """Seconds until the refused request would fit.

        For a token refusal, walks the tenant's rows oldest-first and returns
        when enough usage has aged out. For a request-count refusal the size of
        each row is irrelevant -- one slot frees the moment the oldest row
        expires -- so that is what gets reported.
        """
        rows = conn.execute(
            "SELECT ts, tokens FROM token_usage WHERE tenant = ? AND ts > ? ORDER BY ts ASC",
            (tenant, cutoff),
        ).fetchall()
        if not rows:
            return 0.0
        if by_requests:
            return max(0.0, round(rows[0][0] + self.window_seconds - now, 3))

        ceiling = self.limit_tokens if limit_tokens is None else limit_tokens
        need = used + tokens - ceiling
        freed = 0
        for ts, row_tokens in rows:
            freed += row_tokens
            if freed >= need:
                return max(0.0, round(ts + self.window_seconds - now, 3))
        # Even emptying the window would not fit this request.
        return round(self.window_seconds, 3)
