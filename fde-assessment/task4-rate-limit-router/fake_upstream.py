"""Fake model endpoints with configurable latency and failure injection.

Two endpoints, ``/primary/v1/complete`` and ``/secondary/v1/complete``, each
independently controllable:

    POST /_control/primary   {"mode": "ok"|"429"|"500"|"400"|"garbage",
                              "latency_seconds": 0.0}

``mode: "hang"`` sleeps far past any sane deadline, which is how the timeout
path is exercised against a real socket.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

STATE = {
    "primary": {"mode": "ok", "latency_seconds": 0.0, "calls": 0},
    "secondary": {"mode": "ok", "latency_seconds": 0.0, "calls": 0},
}

# A body that would be catastrophic to forward verbatim: it is what the
# "no leaked internals" tests plant and then assert never appears.
LEAKY_ERROR_BODY = (
    'Traceback (most recent call last):\n'
    '  File "/opt/inference/server.py", line 402, in generate\n'
    "RuntimeError: upstream pool exhausted; "
    "API_KEY=sk-live-9f2c31aa host=inference-primary.internal:8443"
)


def reset() -> None:
    """Return both endpoints to healthy and zero their call counters."""
    for entry in STATE.values():
        entry.update({"mode": "ok", "latency_seconds": 0.0, "calls": 0})


def create_app() -> FastAPI:
    """Build the fake provider app: two endpoints plus test-control routes."""
    app = FastAPI(title="Fake model providers")

    @app.post("/_control/{which}")
    async def control(which: str, request: Request):
        """Test hook: set one endpoint's failure mode and injected latency."""
        body = await request.json()
        STATE[which]["mode"] = body.get("mode", "ok")
        STATE[which]["latency_seconds"] = float(body.get("latency_seconds", 0.0))
        return {"ok": True, **STATE[which]}

    @app.post("/_control/reset/all")
    async def control_reset():
        """Test hook: reset both endpoints to healthy."""
        reset()
        return {"ok": True}

    @app.get("/_stats")
    async def stats():
        """Test hook: report each endpoint's mode and call count."""
        return STATE

    @app.post("/{which}/v1/complete")
    async def complete(which: str, request: Request):
        """Serve a completion, or the currently configured failure.

        Error bodies deliberately contain a stack trace, a file path, an
        internal hostname and a credential fragment, so the tests can assert
        none of it survives the gateway.
        """
        entry = STATE[which]
        entry["calls"] += 1
        body = await request.json()

        if entry["latency_seconds"]:
            await asyncio.sleep(entry["latency_seconds"])
        mode = entry["mode"]
        if mode == "hang":
            # Far past any deadline under test, but bounded so a stray request
            # cannot pin a worker indefinitely.
            await asyncio.sleep(60)
        if mode == "429":
            return JSONResponse(
                {"error": "rate_limited", "detail": LEAKY_ERROR_BODY},
                status_code=429,
                headers={"Retry-After": "31"},
            )
        if mode == "500":
            return PlainTextResponse(LEAKY_ERROR_BODY, status_code=500)
        if mode == "400":
            return JSONResponse({"error": "bad prompt"}, status_code=400)
        if mode == "garbage":
            return PlainTextResponse("<html>not json</html>", status_code=200)

        prompt = body.get("prompt", "")
        return {
            "text": f"[{which}] {prompt[:80]}",
            "tokens_used": max(1, len(prompt) // 4) + 20,
        }

    return app


app = create_app()
