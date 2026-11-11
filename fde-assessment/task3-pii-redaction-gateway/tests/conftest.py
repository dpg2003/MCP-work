"""Shared fixtures, PII samples, and chunking helpers for the Task 3 suite."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from redactor import StreamRedactor  # noqa: E402

# Luhn-valid test PANs published by the card networks. They are not real
# accounts, but they are structurally genuine, which is what the Luhn filter
# requires.
VISA = "4111111111111111"
VISA_SPACED = "4111 1111 1111 1111"
MASTERCARD = "5555555555554444"
AMEX = "378282246310005"

EMAIL = "john.doe@example.com"
SSN = "123-45-6789"


def stream_through(chunks, max_hold: int | None = None) -> str:
    """Feed ``chunks`` through a redactor and return the concatenated output."""
    redactor = StreamRedactor(**({"max_hold": max_hold} if max_hold else {}))
    parts = [redactor.feed(chunk) for chunk in chunks]
    parts.append(redactor.close())
    return "".join(parts)


def split_every(text: str, size: int) -> list[str]:
    """Split ``text`` into fixed-size chunks of ``size`` characters."""
    return [text[index : index + size] for index in range(0, len(text), size)]


import asyncio  # noqa: E402
import contextlib  # noqa: E402


@contextlib.asynccontextmanager
async def live_server(app):
    """Run ``app`` on a real socket.

    httpx's ``ASGITransport`` buffers the entire response body before yielding
    it (see ``httpx/_transports/asgi.py``), so it cannot be used to assert
    anything about streaming or time-to-first-token. Those tests need a real
    HTTP server, which is what this provides on an ephemeral port.
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
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=10)
