"""Minimal mock MCP server sitting behind the gateway.

It is *not* security-aware on purpose: it will happily run `admin_reset_key`
for anyone who reaches it. That is the point — the tests assert the gateway
blocks unauthorized calls by checking this server's call counter never moves.

Failure injection knobs let the gateway's upstream-error handling be tested:

* ``POST /_control/failure`` with ``{"mode": "hang"|"error"|"garbage"|"none",
  "delay_seconds": 0.0}``
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

TOOLS = [
    {
        "name": "get_weather",
        "description": "Current weather for a city. Available to any authenticated role.",
        "inputSchema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
    {
        "name": "list_invoices",
        "description": "List invoices for a customer.",
        "inputSchema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
        },
    },
    {
        "name": "admin_reset_key",
        "description": "Rotate a tenant API key. Admin only.",
        "inputSchema": {
            "type": "object",
            "properties": {"tenant": {"type": "string"}},
            "required": ["tenant"],
        },
    },
    {
        "name": "admin_delete_tenant",
        "description": "Permanently delete a tenant. Admin only.",
        "inputSchema": {
            "type": "object",
            "properties": {"tenant": {"type": "string"}},
            "required": ["tenant"],
        },
    },
]


class CallLog:
    """Records everything that actually reached the downstream server."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.tool_calls: list[str] = []

    @property
    def count(self) -> int:
        return len(self.requests)

    def reset(self) -> None:
        self.requests.clear()
        self.tool_calls.clear()


CALL_LOG = CallLog()

# Failure injection state.
FAILURE = {"mode": "none", "delay_seconds": 0.0}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _handle_one(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        CALL_LOG.tool_calls.append(name)
        known = {tool["name"] for tool in TOOLS}
        if name not in known:
            return _error(request_id, -32601, f"Unknown tool: {name}")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": f"{name} executed"}],
                "structuredContent": {"tool": name, "arguments": params.get("arguments") or {}},
                "isError": False,
            },
        }

    if request_id is None:
        return None  # notification
    return _error(request_id, -32601, f"Method not found: {method}")


def create_app() -> FastAPI:
    app = FastAPI(title="Mock downstream MCP server")

    @app.post("/_control/failure")
    async def set_failure(request: Request):
        body = await request.json()
        FAILURE["mode"] = body.get("mode", "none")
        FAILURE["delay_seconds"] = float(body.get("delay_seconds", 0.0))
        return {"ok": True, **FAILURE}

    @app.post("/_control/reset")
    async def reset():
        CALL_LOG.reset()
        FAILURE.update({"mode": "none", "delay_seconds": 0.0})
        return {"ok": True}

    @app.get("/_control/stats")
    async def stats():
        return {"count": CALL_LOG.count, "tool_calls": CALL_LOG.tool_calls}

    @app.post("/mcp")
    async def mcp(request: Request):
        payload = await request.json()
        CALL_LOG.requests.append(payload)

        if FAILURE["delay_seconds"]:
            await asyncio.sleep(FAILURE["delay_seconds"])
        if FAILURE["mode"] == "error":
            return JSONResponse({"detail": "downstream exploded"}, status_code=500)
        if FAILURE["mode"] == "garbage":
            return PlainTextResponse("<html>502 Bad Gateway</html>", status_code=200)

        if isinstance(payload, list):
            responses = [r for r in (_handle_one(item) for item in payload) if r is not None]
            return JSONResponse(responses)
        return JSONResponse(_handle_one(payload))

    return app


app = create_app()
