"""End-to-end: rate limiting + failover + one error shape, over HTTP."""

from __future__ import annotations

import httpx
import pytest

import fake_upstream
from app import create_app
from conftest import FakeClock, live_server
from providers import ProviderRateLimited, ProviderTimeout, StubProvider
from rate_limiter import RateLimiter
from router import ModelRouter
from tokens_estimate import estimate_tokens

API_KEYS = {"key-acme": "tenant-acme", "key-globex": "tenant-globex"}
ERROR_KEYS = {"type", "message", "request_id", "details"}


@pytest.fixture
def providers():
    """A primary and secondary stub, each with its own call counter."""
    return StubProvider("primary", text="from-primary", tokens_used=120), StubProvider(
        "secondary", text="from-secondary", tokens_used=130
    )


@pytest.fixture
def gateway(db_path, clock, providers):
    """A gateway on a temporary database with stub providers attached."""
    primary, secondary = providers
    limiter = RateLimiter(db_path=db_path, clock=clock)
    app = create_app(
        limiter=limiter,
        router=ModelRouter(primary, secondary, timeout_ms=1000),
        api_keys=dict(API_KEYS),
    )
    app.state.test_limiter = limiter
    try:
        yield app
    finally:
        limiter.close()


@pytest.fixture
async def client(gateway):
    """An httpx client wired to the gateway app."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway.test"
    ) as instance:
        yield instance


async def complete(client, prompt="hello world", max_tokens=64, key="key-acme"):
    """POST one completion request with the given key and body."""
    headers = {"X-API-Key": key} if key else {}
    return await client.post(
        "/v1/complete", json={"prompt": prompt, "max_tokens": max_tokens}, headers=headers
    )


def assert_standard_error(response, error_type: str):
    """Assert the response uses the one standard error shape, and return it."""
    body = response.json()
    assert set(body) == {"error"}, body
    assert set(body["error"]) == ERROR_KEYS, body
    assert body["error"]["type"] == error_type
    assert body["error"]["request_id"].startswith("req_")
    assert "Traceback" not in response.text
    return body["error"]


# --------------------------------------------------------------------------
# Happy path and rate limiting
# --------------------------------------------------------------------------
async def test_request_under_the_limit_succeeds(client):
    response = await complete(client)
    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "from-primary"
    assert body["provider"] == "primary"
    assert body["failed_over"] is False
    assert body["usage"]["actual_tokens"] == 120


async def test_many_requests_under_the_limit_all_succeed(client):
    for _ in range(50):
        assert (await complete(client, max_tokens=64)).status_code == 200


async def test_request_over_the_limit_gets_a_clean_error(client, gateway):
    gateway.state.test_limiter.try_consume("tenant-acme", 49_990)
    response = await complete(client, prompt="x" * 100, max_tokens=1_000)
    assert response.status_code == 429
    error = assert_standard_error(response, "rate_limit_exceeded")
    assert error["details"]["limit_tokens"] == 50_000
    assert error["details"]["retry_after_seconds"] is not None


async def test_exactly_the_limit_is_allowed_and_the_next_token_is_not(client, gateway, providers):
    primary, _ = providers
    limiter = gateway.state.test_limiter
    prompt = "x" * 400                        # 100 prompt tokens
    per_request = estimate_tokens(prompt, 1)  # 100 + 1 = 101
    # Report usage equal to the estimate so reconciliation is a no-op and the
    # boundary arithmetic is exact.
    primary.tokens_used = per_request

    limiter.try_consume("tenant-acme", 50_000 - per_request)
    assert (await complete(client, prompt=prompt, max_tokens=1)).status_code == 200
    assert limiter.usage("tenant-acme") == 50_000, "landed exactly on the limit"

    # One more token is over.
    assert (await complete(client, prompt=prompt, max_tokens=1)).status_code == 429
    assert limiter.usage("tenant-acme") == 50_000


async def test_tenants_do_not_affect_each_other(client, gateway):
    gateway.state.test_limiter.try_consume("tenant-acme", 50_000)
    assert (await complete(client, key="key-acme")).status_code == 429
    assert (await complete(client, key="key-globex")).status_code == 200


async def test_window_eviction_lets_a_blocked_tenant_back_in(client, gateway, clock):
    gateway.state.test_limiter.try_consume("tenant-acme", 50_000)
    assert (await complete(client)).status_code == 429
    clock.advance(61)
    assert (await complete(client)).status_code == 200


async def test_usage_is_reconciled_to_the_actual_token_count(client, gateway):
    prompt = "x" * 400
    estimated = estimate_tokens(prompt, 4_000)
    response = await complete(client, prompt=prompt, max_tokens=4_000)
    body = response.json()
    assert body["usage"]["estimated_tokens"] == estimated
    assert body["usage"]["actual_tokens"] == 120
    # The window reflects the real cost, not the reservation.
    assert gateway.state.test_limiter.usage("tenant-acme") == 120


async def test_budget_is_released_when_both_providers_fail(client, gateway, providers):
    primary, secondary = providers
    primary.error = ProviderTimeout("primary", "deadline")
    secondary.error = ProviderTimeout("secondary", "deadline")
    assert (await complete(client)).status_code == 502
    # The tenant is not charged for the gateway's own outage.
    assert gateway.state.test_limiter.usage("tenant-acme") == 0


async def test_rate_limit_rejection_never_reaches_a_provider(client, gateway, providers):
    primary, _ = providers
    gateway.state.test_limiter.try_consume("tenant-acme", 50_000)
    assert (await complete(client)).status_code == 429
    assert primary.call_count == 0


# --------------------------------------------------------------------------
# Failover, end to end
# --------------------------------------------------------------------------
async def test_primary_429_fails_over_and_the_client_still_succeeds(client, providers):
    primary, secondary = providers
    primary.error = ProviderRateLimited("primary", "429 upstream")
    response = await complete(client)
    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "from-secondary"
    assert body["failed_over"] is True
    assert secondary.call_count == 1


async def test_primary_timeout_fails_over(client, providers):
    primary, _ = providers
    primary.error = ProviderTimeout("primary", "read timeout")
    body = (await complete(client)).json()
    assert body["provider"] == "secondary"


async def test_primary_success_means_the_secondary_is_never_called(client, providers):
    _, secondary = providers
    for _ in range(5):
        assert (await complete(client)).status_code == 200
    assert secondary.call_count == 0


async def test_both_down_gives_one_sanitized_error(client, providers):
    primary, secondary = providers
    primary.error = ProviderRateLimited("primary", fake_upstream.LEAKY_ERROR_BODY)
    secondary.error = ProviderTimeout("secondary", fake_upstream.LEAKY_ERROR_BODY)
    response = await complete(client)
    assert response.status_code == 502
    assert_standard_error(response, "upstream_unavailable")
    for secret in ("sk-live-9f2c31aa", "inference-primary.internal", "/opt/inference", "Traceback"):
        assert secret not in response.text


# --------------------------------------------------------------------------
# Error shape consistency
# --------------------------------------------------------------------------
async def test_missing_api_key(client):
    response = await complete(client, key=None)
    assert response.status_code == 401
    assert_standard_error(response, "unauthenticated")


async def test_unknown_api_key(client):
    response = await complete(client, key="key-not-real")
    assert response.status_code == 401
    assert_standard_error(response, "unauthenticated")


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": ""},
        {"prompt": "hi", "max_tokens": 0},
        {"prompt": "hi", "max_tokens": -5},
        {"prompt": "hi", "max_tokens": "many"},
        {"prompt": 5},
        {},
    ],
)
async def test_invalid_requests_use_the_same_error_shape(client, payload):
    response = await client.post("/v1/complete", json=payload, headers={"X-API-Key": "key-acme"})
    assert response.status_code == 400
    assert_standard_error(response, "invalid_request")


async def test_every_error_path_shares_one_shape(client, gateway, providers):
    """Auth, validation, rate limit, and upstream failure — all identical in shape."""
    primary, secondary = providers
    responses = [
        await complete(client, key=None),
        await client.post("/v1/complete", json={"prompt": ""}, headers={"X-API-Key": "key-acme"}),
    ]
    primary.error = ProviderTimeout("primary", "x")
    secondary.error = ProviderTimeout("secondary", "x")
    responses.append(await complete(client))
    gateway.state.test_limiter.try_consume("tenant-acme", 50_000)
    responses.append(await complete(client))

    shapes = {tuple(sorted(r.json()["error"])) for r in responses}
    assert shapes == {tuple(sorted(ERROR_KEYS))}
    assert {r.status_code for r in responses} == {400, 401, 429, 502}


async def test_unexpected_internal_exception_is_sanitized(client, providers):
    primary, secondary = providers
    primary.error = RuntimeError("DB_PASSWORD=hunter2 host=vault.internal")
    response = await complete(client)
    assert response.status_code == 500
    assert_standard_error(response, "internal_error")
    for secret in ("hunter2", "vault.internal", "RuntimeError"):
        assert secret not in response.text


# --------------------------------------------------------------------------
# Persistence, over the HTTP surface
# --------------------------------------------------------------------------
async def test_a_restarted_gateway_still_enforces_prior_usage(db_path, clock, providers):
    """New app object, new limiter object, same database file."""
    primary, secondary = providers

    def build():
        """Construct a gateway sharing the same database file."""
        limiter = RateLimiter(db_path=db_path, clock=clock)
        app = create_app(
            limiter=limiter,
            router=ModelRouter(primary, secondary, timeout_ms=1000),
            api_keys=dict(API_KEYS),
        )
        app.state.test_limiter = limiter
        return app, limiter

    first_app, first_limiter = build()
    first_limiter.try_consume("tenant-acme", 49_900)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=first_app), base_url="http://gateway.test"
    ) as first_client:
        assert (await complete(first_client, max_tokens=1_000)).status_code == 429
    first_limiter.close()

    second_app, second_limiter = build()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second_app), base_url="http://gateway.test"
        ) as second_client:
            assert second_limiter.usage("tenant-acme") == 49_900
            assert (await complete(second_client, max_tokens=1_000)).status_code == 429
    finally:
        second_limiter.close()


# --------------------------------------------------------------------------
# Real HTTP providers, real sockets
# --------------------------------------------------------------------------
async def test_full_stack_over_real_sockets(db_path, clock):
    """Gateway + real HTTP providers, exercised through an actual server."""
    from providers import HttpModelProvider

    fake_upstream.reset()
    async with live_server(fake_upstream.create_app()) as upstream_url:
        async with httpx.AsyncClient() as http_client:
            limiter = RateLimiter(db_path=db_path, clock=clock)
            router = ModelRouter(
                HttpModelProvider("primary", f"{upstream_url}/primary/v1/complete", http_client),
                HttpModelProvider("secondary", f"{upstream_url}/secondary/v1/complete", http_client),
                timeout_ms=500,
            )
            app = create_app(limiter=limiter, router=router, api_keys=dict(API_KEYS))
            try:
                async with live_server(app) as gateway_url:
                    async with httpx.AsyncClient(base_url=gateway_url) as gateway_client:
                        ok = await complete(gateway_client)
                        assert ok.status_code == 200
                        assert ok.json()["provider"] == "primary"

                        fake_upstream.STATE["primary"]["mode"] = "429"
                        failed_over = await complete(gateway_client)
                        assert failed_over.status_code == 200
                        assert failed_over.json()["provider"] == "secondary"

                        fake_upstream.STATE["secondary"]["mode"] = "500"
                        both_down = await complete(gateway_client)
                        assert both_down.status_code == 502
                        assert_standard_error(both_down, "upstream_unavailable")
                        assert "sk-live-9f2c31aa" not in both_down.text
            finally:
                limiter.close()
