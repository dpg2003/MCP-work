"""Shared fixtures for the Task 4 suite: a fake clock, a temp DB, a limiter."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from rate_limiter import RateLimiter  # noqa: E402


class FakeClock:
    """Injectable clock, so window eviction is tested without sleeping 60s."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds``."""
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    """A controllable clock shared by a test and its limiter."""
    return FakeClock()


@pytest.fixture
def db_path(tmp_path) -> str:
    """Path to a throwaway SQLite database inside the test's tmp dir."""
    return str(tmp_path / "rate_limit.sqlite3")


@pytest.fixture
def limiter(db_path, clock):
    """A limiter on a temporary database, driven by the fake clock."""
    instance = RateLimiter(db_path=db_path, clock=clock)
    try:
        yield instance
    finally:
        instance.close()


import asyncio  # noqa: E402
import contextlib  # noqa: E402


@contextlib.asynccontextmanager
async def live_server(app):
    """Run ``app`` on a real socket on an ephemeral port.

    Needed because httpx's ``ASGITransport`` awaits the application directly:
    there is no socket, so a client-side read timeout has nothing to fire on. A
    genuine timeout test needs a genuine server.
    """
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", access_log=False)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=10)
