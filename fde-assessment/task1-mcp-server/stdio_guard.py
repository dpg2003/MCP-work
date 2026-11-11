"""Turn unparseable stdio frames into proper JSON-RPC error responses.

``mcp.server.stdio.stdio_server()`` pushes a parse/validation *exception* onto
the read stream for any line it cannot decode, and the default dispatcher
simply logs and drops it. A client that sent one line of garbage would then
hang forever waiting for a reply.

``MalformedFrameReporter`` wraps the read stream: protocol messages pass
through untouched, while exception items are converted to a JSON-RPC error
response written straight to the write stream.

  * unparseable JSON                     -> -32700 Parse error
  * valid JSON, wrong JSON-RPC shape     -> -32600 Invalid Request
    (missing ``jsonrpc``, bad ``method`` type, ``id`` of the wrong type, ...)

The request ``id`` is echoed when it can be recovered from the offending
payload and is ``null`` otherwise, per JSON-RPC 2.0.
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any

import mcp_types as types
from mcp.shared._stream_protocols import ReadStream, WriteStream
from mcp.shared.message import SessionMessage
from pydantic import ValidationError
from typing_extensions import Self

logger = logging.getLogger("fde.mcp.stdio_guard")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600


def _recoverable_id(exc: ValidationError) -> Any:
    """Best-effort extraction of ``id`` from the payload that failed validation.

    Pydantic keeps the offending input on each error entry. When the frame was
    valid JSON but the wrong shape, that input is the decoded object and its
    ``id`` is safe to echo (only if it is a JSON-RPC-legal id type).
    """
    for err in exc.errors(include_url=False):
        candidate = err.get("input")
        if isinstance(candidate, dict) and "id" in candidate:
            value = candidate["id"]
            if isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool)):
                return value
    return None


def classify(exc: Exception) -> tuple[int, str, Any]:
    """Map a transport-level decode failure to ``(code, message, id)``."""
    if isinstance(exc, ValidationError):
        entries = exc.errors(include_url=False)
        if any(err.get("type") == "json_invalid" for err in entries):
            return PARSE_ERROR, "Parse error", None
        return INVALID_REQUEST, "Invalid Request", _recoverable_id(exc)
    return PARSE_ERROR, "Parse error", None


class MalformedFrameReporter:
    """Read-stream wrapper that answers undecodable frames instead of dropping them."""

    def __init__(
        self,
        inner: ReadStream[SessionMessage | Exception],
        write_stream: WriteStream[SessionMessage],
    ) -> None:
        self._inner = inner
        self._write_stream = write_stream

    # -- ReadStream protocol ------------------------------------------------
    async def receive(self) -> SessionMessage | Exception:
        """Return the next protocol message, answering any bad frames first."""
        while True:
            item = await self._inner.receive()
            if not isinstance(item, Exception):
                return item
            await self._report(item)

    def __aiter__(self) -> "MalformedFrameReporter":
        return self

    async def __anext__(self) -> SessionMessage | Exception:
        while True:
            item = await self._inner.__anext__()
            if not isinstance(item, Exception):
                return item
            await self._report(item)

    async def aclose(self) -> None:
        """Close the wrapped stream. The write stream is not ours to close."""
        await self._inner.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        await self.aclose()
        return None

    @property
    def last_context(self) -> Any:  # pragma: no cover - passthrough for the dispatcher
        """Sender context of the last item, forwarded from the wrapped stream.

        The dispatcher reads this to restore ``contextvars`` across the task
        boundary, so the wrapper must not hide it.
        """
        return getattr(self._inner, "last_context", None)

    # -- internals ----------------------------------------------------------
    async def _report(self, exc: Exception) -> None:
        """Answer one undecodable frame with a JSON-RPC error response."""
        code, message, request_id = classify(exc)
        logger.warning("rejecting malformed frame: code=%s (%s)", code, type(exc).__name__)
        error = types.JSONRPCError(
            jsonrpc="2.0",
            id=request_id,
            error=types.ErrorData(code=code, message=message),
        )
        await self._write_stream.send(SessionMessage(error))
