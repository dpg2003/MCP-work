"""Operational surface: liveness, readiness, pooling, and graceful shutdown."""

from __future__ import annotations

import asyncio

import httpx
import pytest

import app as app_module
from admission import AdmissionController
from conftest import FakeClock
from providers import StubProvider
from rate_limiter import RateLimiter
from router import ModelRouter

API_KEYS = {"key-acme": "tenant-acme"}


@pytest.fixture
def gateway_app(db_path, clock):
    """A gateway on a temporary database with stub providers."""
    limiter = RateLimiter(db_path=db_path, clock=clock)
    application = app_module.create_app(
        limiter=limiter,
        router=ModelRouter(StubProvider("primary"), StubProvider("secondary"), timeout_ms=1000),
        api_keys=dict(API_KEYS),
    )
    application.state.test_limiter = limiter
    try:
        yield application
    finally:
        limiter.close()


@pytest.fixture
async def ops_client(gateway_app):
    """An httpx client wired to the gateway."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app), base_url="http://gateway.test"
    ) as client:
        yield client


# --------------------------------------------------------------------------
# Liveness vs readiness
# --------------------------------------------------------------------------
async def test_liveness_is_cheap_and_dependency_free(ops_client):
    """Liveness must not fail for a dependency, or a blip triggers restarts."""
    assert (await ops_client.get("/healthz")).json() == {"status": "ok"}


async def test_readiness_reports_the_database_check(ops_client):
    response = await ops_client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"rate_limit_db": "ok"}}


async def test_readiness_fails_when_the_database_is_unusable(ops_client, gateway_app):
    """A broken dependency must take the instance out of rotation, not 500."""
    gateway_app.state.test_limiter.close()          # simulate a lost database
    response = await ops_client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"]["rate_limit_db"] == "error"
    assert "Traceback" not in response.text
    assert "sqlite" not in response.text.lower(), "readiness must not leak internals"


async def test_liveness_still_passes_when_readiness_fails(ops_client, gateway_app):
    """The two probes are deliberately independent."""
    gateway_app.state.test_limiter.close()
    assert (await ops_client.get("/healthz")).status_code == 200
    assert (await ops_client.get("/readyz")).status_code == 503


# --------------------------------------------------------------------------
# Connection pooling
# --------------------------------------------------------------------------
async def test_provider_pool_is_bounded():
    """An unbounded pool exhausts file descriptors under a burst."""
    application = app_module.create_app(api_keys=dict(API_KEYS))
    try:
        async with application.router.lifespan_context(application):
            pool = application.state.client._transport._pool
            assert pool._max_connections == 100
            assert pool._max_keepalive_connections == 20
    finally:
        application.state.limiter.close()


# --------------------------------------------------------------------------
# Graceful shutdown
# --------------------------------------------------------------------------
async def test_shutdown_drains_the_admission_workers(db_path, clock):
    """Lifespan exit must stop the group-commit workers, not orphan them."""
    limiter = RateLimiter(db_path=db_path, clock=clock)
    application = app_module.create_app(
        limiter=limiter,
        router=ModelRouter(StubProvider("primary"), StubProvider("secondary")),
        api_keys=dict(API_KEYS),
    )
    try:
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://g.test"
            ) as client:
                response = await client.post(
                    "/v1/complete", json={"prompt": "hi", "max_tokens": 8},
                    headers={"X-API-Key": "key-acme"},
                )
                assert response.status_code == 200
        # After lifespan exit the controller refuses new work rather than hanging.
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(
                application.state.admission.try_consume("tenant-acme", 1), timeout=5
            )
    finally:
        limiter.close()


async def test_shutdown_is_safe_with_no_traffic(db_path, clock):
    limiter = RateLimiter(db_path=db_path, clock=clock)
    application = app_module.create_app(limiter=limiter, api_keys=dict(API_KEYS))
    try:
        async with application.router.lifespan_context(application):
            pass
    finally:
        limiter.close()


async def test_shutdown_does_not_lose_an_in_flight_admission(db_path, clock):
    """Work already accepted must complete before the worker stops."""
    limiter = RateLimiter(db_path=db_path, clock=clock)
    controller = AdmissionController(limiter)
    try:
        decisions = await asyncio.gather(*[controller.try_consume("t", 100) for _ in range(20)])
        assert all(d.allowed for d in decisions)
        await controller.aclose()
        assert limiter.usage("t") == 2_000
    finally:
        limiter.close()
