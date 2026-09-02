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

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Callable

DEFAULT_LIMIT_TOKENS = 50_000
DEFAULT_WINDOW_SECONDS = 60.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant    TEXT    NOT NULL,
    ts        REAL    NOT NULL,
    tokens    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_token_usage_tenant_ts ON token_usage (tenant, ts);
"""


@dataclass(frozen=True)
class Decision:
    """Outcome of an admission check."""

    allowed: bool
    tenant: str
    requested_tokens: int
    used_tokens: int          # usage in the window *including* this request if allowed
    limit_tokens: int
    retry_after_seconds: float | None = None
    reservation_id: int | None = None

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.limit_tokens - self.used_tokens)


class RateLimiter:
    """Per-tenant token budget over a sliding window, persisted in SQLite."""

    def __init__(
        self,
        db_path: str,
        limit_tokens: int = DEFAULT_LIMIT_TOKENS,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.db_path = db_path
        self.limit_tokens = limit_tokens
        self.window_seconds = window_seconds
        # Injectable so window-eviction can be tested without sleeping 60s.
        self.clock = clock or time.time
        self._lock = threading.Lock()

        directory = os.path.dirname(os.path.abspath(db_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        # isolation_level=None: no implicit transactions, so BEGIN IMMEDIATE
        # below is the only transaction boundary and it means what it says.
        self._conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False,
                                     timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # -- core ---------------------------------------------------------------
    def try_consume(self, tenant: str, tokens: int) -> Decision:
        """Atomically evict, sum, decide, and record.

        Allows when ``used + tokens <= limit`` — so exactly the limit is
        admitted and one token over is not.
        """
        if tokens < 0:
            raise ValueError("tokens must be non-negative")

        with self._lock:
            now = self.clock()
            cutoff = now - self.window_seconds
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM token_usage WHERE ts <= ?", (cutoff,))
                used = conn.execute(
                    "SELECT COALESCE(SUM(tokens), 0) FROM token_usage WHERE tenant = ? AND ts > ?",
                    (tenant, cutoff),
                ).fetchone()[0]

                if used + tokens > self.limit_tokens:
                    retry_after = self._retry_after(conn, tenant, cutoff, now, tokens, used)
                    conn.execute("COMMIT")
                    return Decision(
                        allowed=False,
                        tenant=tenant,
                        requested_tokens=tokens,
                        used_tokens=used,
                        limit_tokens=self.limit_tokens,
                        retry_after_seconds=retry_after,
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
                limit_tokens=self.limit_tokens,
                reservation_id=reservation_id,
            )

    def reconcile(self, reservation_id: int, actual_tokens: int) -> None:
        """Correct a reservation once the real usage is known.

        Admission has to charge an *estimate* — the true cost is not known
        until the provider answers, and by then the request has already been
        made. Reconciling afterwards keeps the window honest without ever
        letting an unmetered request through.
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
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def release(self, reservation_id: int) -> None:
        """Drop a reservation entirely (the request never reached a provider)."""
        self.reconcile(reservation_id, 0)

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
                     tokens: int, used: int) -> float:
        """Seconds until enough usage ages out for this request to fit.

        Walks the tenant's rows oldest-first, accumulating what expires, and
        returns when the freed amount is sufficient.
        """
        need = used + tokens - self.limit_tokens
        rows = conn.execute(
            "SELECT ts, tokens FROM token_usage WHERE tenant = ? AND ts > ? ORDER BY ts ASC",
            (tenant, cutoff),
        ).fetchall()
        freed = 0
        for ts, row_tokens in rows:
            freed += row_tokens
            if freed >= need:
                return max(0.0, round(ts + self.window_seconds - now, 3))
        # Even emptying the window would not fit this request.
        return round(self.window_seconds, 3)
