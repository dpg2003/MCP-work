"""LLM Gateway: per-tenant token rate limiting with primary/secondary failover.

    POST /v1/complete
    X-API-Key: <tenant key>
    {"prompt": "...", "max_tokens": 256}

Request flow:

1. Authenticate the API key to a tenant.
2. Estimate the token cost and ask the limiter to admit it. A rejection here
   costs nothing upstream.
3. Route to the primary, failing over to the secondary on 429/timeout/5xx.
4. Reconcile the reservation with the provider's reported usage.

Every error path — validation, auth, rate limit, both-providers-down, and an
unexpected internal exception — returns the same payload shape from
``errors.py``. There is no path that returns a provider's error body.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from errors import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    RATE_LIMIT_EXCEEDED,
    UNAUTHENTICATED,
    GatewayError,
    new_request_id,
)
from admission import AdmissionController
from providers import HttpModelProvider
from rate_limiter import DEFAULT_LIMIT_TOKENS, DEFAULT_WINDOW_SECONDS, RateLimiter
from router import DEFAULT_TIMEOUT_MS, ModelRouter
from tokens_estimate import estimate_tokens

logger = logging.getLogger("fde.gateway")

# Demo key -> tenant mapping. A real deployment swaps this for a lookup; the
# gateway only ever needs a tenant id out of it.
DEFAULT_API_KEYS = {
    "key-acme": "tenant-acme",
    "key-globex": "tenant-globex",
}


class CompleteRequest(BaseModel):
    """Request body for ``POST /v1/complete``.

    Bounds are rejections, not truncations: an oversized prompt or an
    out-of-range ``max_tokens`` produces a standardized ``invalid_request``.
    """

    prompt: str = Field(min_length=1, max_length=200_000)
    max_tokens: int = Field(default=256, ge=1, le=32_000)


def create_app(
    limiter: RateLimiter | None = None,
    router: ModelRouter | None = None,
    api_keys: dict[str, str] | None = None,
) -> FastAPI:
    """Build the gateway application.

    Args:
        limiter: Rate limiter. Injectable so tests can supply a temporary
            database and a controllable clock; defaults to one configured
            from the environment.
        router: Model router. Injectable so tests can use stub providers.
        api_keys: Key-to-tenant mapping; defaults to the demo mapping.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Drain the admission workers and close a client we own, on shutdown."""
        try:
            yield
        finally:
            await app.state.admission.aclose()
            if app.state.owns_client:
                await app.state.client.aclose()

    app = FastAPI(title="LLM gateway: rate limiting and model fallback", lifespan=lifespan)

    app.state.api_keys = api_keys if api_keys is not None else dict(DEFAULT_API_KEYS)
    app.state.owns_client = False

    if limiter is None:
        limiter = RateLimiter(
            db_path=os.environ.get("RATE_LIMIT_DB", "rate_limit.sqlite3"),
            limit_tokens=int(os.environ.get("RATE_LIMIT_TOKENS", DEFAULT_LIMIT_TOKENS)),
            window_seconds=float(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", DEFAULT_WINDOW_SECONDS)),
        )
    # Everything downstream talks to the async, group-committing facade rather
    # than to the limiter directly, so no request ever does SQLite I/O on the
    # event loop. `limiter` stays reachable for tests and for shutdown.
    app.state.limiter = limiter
    app.state.admission = AdmissionController(limiter)

    if router is None:
        client = httpx.AsyncClient()
        app.state.owns_client = True
        app.state.client = client
        timeout_ms = int(os.environ.get("PROVIDER_TIMEOUT_MS", DEFAULT_TIMEOUT_MS))
        router = ModelRouter(
            primary=HttpModelProvider(
                name="primary",
                url=os.environ.get("PRIMARY_URL", "http://127.0.0.1:9100/primary/v1/complete"),
                client=client,
            ),
            secondary=HttpModelProvider(
                name="secondary",
                url=os.environ.get("SECONDARY_URL", "http://127.0.0.1:9100/secondary/v1/complete"),
                client=client,
            ),
            timeout_ms=timeout_ms,
        )
    app.state.router = router

    # -- error handlers: every failure exits through the same shape ---------
    @app.exception_handler(GatewayError)
    async def _gateway_error(request: Request, exc: GatewayError):
        """Render a deliberate gateway error in the standard shape."""
        return JSONResponse(exc.to_payload(), status_code=exc.http_status)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        """Convert FastAPI's validation error into the gateway's own shape.

        Only the offending field *names* are echoed, never the submitted
        values, so an error response cannot reflect a caller's payload back out.
        """
        error = GatewayError(
            INVALID_REQUEST,
            details={"fields": [".".join(str(p) for p in e["loc"][1:]) for e in exc.errors()]},
        )
        return JSONResponse(error.to_payload(), status_code=error.http_status)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        """Backstop for anything unhandled: log the traceback, return a bare 500."""
        error = GatewayError(INTERNAL_ERROR)
        # The traceback goes to the log, correlated by request_id. Never to the client.
        logger.exception("request_id=%s unhandled error", error.request_id)
        return JSONResponse(error.to_payload(), status_code=error.http_status)

    @app.get("/healthz")
    async def healthz():
        """Liveness probe. Touches neither the limiter nor a provider."""
        return {"status": "ok"}

    @app.post("/v1/complete")
    async def complete(body: CompleteRequest, x_api_key: str | None = Header(default=None)):
        """Authenticate, meter, route, and reconcile one completion request.

        The reservation is released rather than charged whenever the request
        produced no tokens, so a tenant is never billed for a gateway outage.
        """
        request_id = new_request_id()

        tenant = app.state.api_keys.get(x_api_key or "")
        if tenant is None:
            raise GatewayError(UNAUTHENTICATED, request_id=request_id)

        admission: AdmissionController = app.state.admission
        estimated = estimate_tokens(body.prompt, body.max_tokens)
        decision = await admission.try_consume(tenant, estimated)
        if not decision.allowed:
            raise GatewayError(
                RATE_LIMIT_EXCEEDED,
                details={
                    "limit_tokens": decision.limit_tokens,
                    "window_seconds": admission.window_seconds,
                    "used_tokens": decision.used_tokens,
                    "requested_tokens": decision.requested_tokens,
                    "retry_after_seconds": decision.retry_after_seconds,
                },
                request_id=request_id,
            )

        try:
            routed = await app.state.router.complete(body.prompt, body.max_tokens, request_id)
        except GatewayError:
            # The request never produced tokens; give the budget back rather
            # than charging a tenant for an outage that was not their fault.
            await admission.release(decision.reservation_id)
            raise
        except Exception as exc:
            # A bug, not a provider failure. Caught here rather than left to the
            # framework's handler so the reservation is still released, and so
            # the client gets the gateway's own error shape either way.
            await admission.release(decision.reservation_id)
            logger.exception("request_id=%s unexpected routing failure", request_id)
            raise GatewayError(
                INTERNAL_ERROR, request_id=request_id, internal_detail=f"{type(exc).__name__}"
            ) from exc

        window_tokens = await admission.reconcile(
            decision.reservation_id, routed.completion.tokens_used, tenant
        )

        payload: dict[str, Any] = {
            "request_id": request_id,
            "text": routed.completion.text,
            "provider": routed.provider_used,
            "failed_over": routed.failed_over,
            "usage": {
                "estimated_tokens": estimated,
                "actual_tokens": routed.completion.tokens_used,
                "tenant_window_tokens": window_tokens,
                "limit_tokens": admission.limit_tokens,
            },
        }
        return JSONResponse(payload, headers={"X-Request-Id": request_id})

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8080")))
