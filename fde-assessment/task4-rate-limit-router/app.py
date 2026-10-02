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

import asyncio
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
from tenants import Tenant, TenantRegistry
from rate_limiter import DEFAULT_LIMIT_TOKENS, DEFAULT_WINDOW_SECONDS, RateLimiter
from router import DEFAULT_TIMEOUT_MS, ModelRouter
from tokens_estimate import estimate_tokens

logger = logging.getLogger("fde.gateway")

# Tenant identity and quotas come from TenantRegistry (see tenants.py), which
# stores only hashed keys and resolves them in constant time. Configure it with
# TENANTS_FILE; with none set, a clearly-labelled demo registry is used and the
# service logs a warning.


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
    registry: TenantRegistry | None = None,
) -> FastAPI:
    """Build the gateway application.

    Args:
        limiter: Rate limiter. Injectable so tests can supply a temporary
            database and a controllable clock; defaults to one configured
            from the environment.
        router: Model router. Injectable so tests can use stub providers.
        api_keys: Legacy plaintext key-to-tenant mapping. Accepted so existing
            callers and tests keep working; it is converted into a registry
            whose tenants all get the "pro" quota.
        registry: Tenant registry. Takes precedence over ``api_keys``;
            defaults to one built from the environment.
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

    if registry is not None:
        app.state.registry = registry
    elif api_keys is not None:
        # Adapt the legacy {key: tenant} shape onto the registry.
        app.state.registry = TenantRegistry.from_dict(
            {"tenants": [{"tenant_id": tenant, "tier": "pro", "api_keys": [key]}
                         for key, tenant in api_keys.items()]}
        )
    else:
        app.state.registry = TenantRegistry.from_env()
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
        # Bound the pool so a burst cannot exhaust file descriptors, and so
        # backpressure shows up as queuing rather than as collapse.
        client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=int(os.environ.get("PROVIDER_MAX_CONNECTIONS", 100)),
                max_keepalive_connections=int(os.environ.get("PROVIDER_MAX_KEEPALIVE", 20)),
            )
        )
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

    @app.get("/readyz")
    async def readyz():
        """Readiness probe: can this instance actually serve a request?

        Distinct from ``/healthz`` on purpose. Liveness answers "is the process
        alive" and must never fail for a dependency, or an orchestrator will
        restart a healthy pod during a downstream blip. Readiness answers "can I
        serve traffic right now", so it touches the database that admission
        depends on -- and takes this instance out of rotation instead of
        rejecting requests if that is broken.
        """
        try:
            await asyncio.to_thread(app.state.limiter.row_count)
        except Exception:
            logger.exception("readiness probe failed")
            return JSONResponse(
                {"status": "not_ready", "checks": {"rate_limit_db": "error"}},
                status_code=503,
            )
        router_health = app.state.router.health()
        if not router_health["available"]:
            # Every provider's breaker is open: the gateway is up but cannot
            # serve, so it should leave rotation rather than return errors.
            return JSONResponse(
                {"status": "not_ready",
                 "checks": {"rate_limit_db": "ok", "providers": "all_circuits_open"},
                 "providers": router_health["providers"]},
                status_code=503,
            )
        return {
            "status": "ready",
            "checks": {"rate_limit_db": "ok", "providers": "ok"},
            "providers": router_health["providers"],
        }

    @app.get("/v1/providers")
    async def providers_health():
        """Per-provider circuit-breaker state, for dashboards and on-call.

        Exposes which providers are currently being skipped and how long until
        the next probe, so an outage is visible without reading logs.
        """
        return app.state.router.health()

    @app.get("/v1/quota")
    async def quota(x_api_key: str | None = Header(default=None)):
        """The calling tenant's own quota and current window usage."""
        resolved = app.state.registry.resolve(x_api_key)
        if resolved is None:
            error = GatewayError(UNAUTHENTICATED)
            return JSONResponse(error.to_payload(), status_code=error.http_status)
        used = await app.state.admission.usage(resolved.tenant_id)
        return {
            "tenant": resolved.tenant_id,
            "tier": resolved.tier,
            "window_seconds": app.state.admission.window_seconds,
            "limits": {
                "tokens_per_minute": resolved.quota.tokens_per_minute,
                "requests_per_minute": resolved.quota.requests_per_minute,
                "max_concurrent": resolved.quota.max_concurrent,
            },
            "used_tokens": used,
        }

    @app.post("/v1/complete")
    async def complete(body: CompleteRequest, x_api_key: str | None = Header(default=None)):
        """Authenticate, meter, route, and reconcile one completion request.

        The reservation is released rather than charged whenever the request
        produced no tokens, so a tenant is never billed for a gateway outage.
        """
        request_id = new_request_id()

        resolved: Tenant | None = app.state.registry.resolve(x_api_key)
        if resolved is None:
            # The key itself is never logged -- only that resolution failed.
            logger.warning("request_id=%s unresolved api key", request_id)
            raise GatewayError(UNAUTHENTICATED, request_id=request_id)
        tenant = resolved.tenant_id

        admission: AdmissionController = app.state.admission
        estimated = estimate_tokens(body.prompt, body.max_tokens)
        decision = await admission.try_consume(tenant, estimated, resolved.quota)
        if not decision.allowed:
            raise GatewayError(
                RATE_LIMIT_EXCEEDED,
                details={
                    "limit": decision.reason,
                    "tier": resolved.tier,
                    "limit_tokens": decision.limit_tokens,
                    "limit_requests": decision.limit_requests,
                    "window_seconds": admission.window_seconds,
                    "used_tokens": decision.used_tokens,
                    "used_requests": decision.used_requests,
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
            "tenant": {"id": tenant, "tier": resolved.tier},
            "usage": {
                "estimated_tokens": estimated,
                "actual_tokens": routed.completion.tokens_used,
                "tenant_window_tokens": window_tokens,
                "limit_tokens": decision.limit_tokens,
                "limit_requests": decision.limit_requests,
            },
        }
        return JSONResponse(payload, headers={"X-Request-Id": request_id})

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8080")))
