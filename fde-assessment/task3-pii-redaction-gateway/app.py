"""LLM Gateway with a streaming PII-redaction guardrail.

``POST /v1/generate`` proxies a prompt to the configured provider and streams
the response back with emails, SSNs, and card numbers replaced by
``[REDACTED]`` -- correctly even when a pattern straddles chunk boundaries.

Response framing is ``text/plain`` chunked transfer. The redaction layer is
independent of framing; SSE would only change how the same emitted strings are
wrapped.

Mid-stream failures
-------------------
Once the first byte is sent the status code is committed, so an upstream
failure cannot become a 502. The gateway instead flushes whatever is safely
redacted, appends one sanitized sentinel line, and closes the connection:

    \\n[gateway-error] upstream_stream_failed\\n

That is a clean close, not a hang, and it carries no provider detail.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from providers import UpstreamStreamError, provider_from_env
from redactor import DEFAULT_MAX_HOLD, StreamRedactor

logger = logging.getLogger("fde.pii_gateway")

ERROR_SENTINEL = "\n[gateway-error] upstream_stream_failed\n"

# Header a caller may set to join this request to their own trace; generated
# when absent. Echoed on the response and stamped on every log line, so a user
# quoting an id from a truncated stream leads an operator straight to the cause.
REQUEST_ID_HEADER = "X-Request-Id"


def new_request_id() -> str:
    """Mint a correlation id for one request."""
    return f"req_{uuid.uuid4().hex[:16]}"


class GenerateRequest(BaseModel):
    """Request body for ``POST /v1/generate``.

    The upper bound on ``prompt`` is a rejection, not a truncation: an
    oversized prompt gets a 422 rather than being silently clipped.
    """

    prompt: str = Field(min_length=1, max_length=100_000)


async def redacted_stream(
    source: AsyncIterator[str],
    max_hold: int = DEFAULT_MAX_HOLD,
    request_id: str | None = None,
) -> AsyncIterator[bytes]:
    """Redact ``source`` and encode for the wire.

    Encoding happens here, after redaction, on complete ``str`` values. That
    ordering is what makes multi-byte text safe: the redactor never sees half a
    UTF-8 sequence, and a chunk boundary can never fall inside a character.
    """
    redactor = StreamRedactor(max_hold=max_hold)
    failed = False
    try:
        async for chunk in source:
            emitted = redactor.feed(chunk)
            if emitted:
                yield emitted.encode("utf-8")
    except UpstreamStreamError as exc:
        failed = True
        logger.error("request_id=%s upstream stream failed: %s", request_id, exc)
    except Exception as exc:  # noqa: BLE001 - anything at all, sanitized below
        failed = True
        logger.exception(
            "request_id=%s unexpected upstream failure: %s", request_id, type(exc).__name__
        )
    finally:
        # The held-back tail is redacted and flushed even on failure. Dropping
        # it would silently truncate every stream that errors.
        tail = redactor.close()
        if tail:
            yield tail.encode("utf-8")
        if failed:
            yield ERROR_SENTINEL.encode("utf-8")


def create_app(provider=None, max_hold: int = DEFAULT_MAX_HOLD) -> FastAPI:
    """Build the gateway application.

    Args:
        provider: Upstream provider. Injectable so tests can script exact
            chunk boundaries; defaults to the one named by ``LLM_PROVIDER``.
        max_hold: Passed through to the redactor as the split-width guarantee.
    """
    app = FastAPI(title="LLM gateway with PII redaction")
    app.state.provider = provider or provider_from_env()
    app.state.max_hold = max_hold

    @app.get("/healthz")
    async def healthz():
        """Liveness probe, naming the active provider."""
        return {"status": "ok", "provider": type(app.state.provider).__name__}

    @app.post("/v1/generate")
    async def generate(request: GenerateRequest, http_request: Request):
        """Proxy a prompt upstream and stream the redacted response back.

        A provider that fails *before* any bytes are sent still gets a real
        502. Once streaming has begun the status is committed, so mid-stream
        failures are handled inside :func:`redacted_stream` instead.
        """
        request_id = http_request.headers.get(REQUEST_ID_HEADER) or new_request_id()
        try:
            source = app.state.provider.stream(request.prompt)
        except UpstreamStreamError as exc:
            # Failed before any bytes were sent, so a real status code is still
            # available.
            logger.error("request_id=%s provider unavailable: %s", request_id, exc)
            return JSONResponse(
                {
                    "error": {
                        "type": "upstream_unavailable",
                        "message": "Upstream provider error",
                        "request_id": request_id,
                    }
                },
                status_code=502,
                headers={REQUEST_ID_HEADER: request_id},
            )
        return StreamingResponse(
            redacted_stream(source, max_hold=app.state.max_hold, request_id=request_id),
            media_type="text/plain; charset=utf-8",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                REQUEST_ID_HEADER: request_id,
            },
        )

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
