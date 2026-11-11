"""Tenant identity, key handling, and per-tenant quota enforcement.

These cover what "rate limits" has to mean for a multi-tenant gateway: each
tenant's own budget, across more than one dimension, with credentials that are
not recoverable from the store. The previous implementation had a single global
limit and a plaintext key dict, and no test asked whether that was adequate.
"""

from __future__ import annotations

import json

import httpx
import pytest

import app as app_module
from providers import StubProvider
from rate_limiter import RateLimiter
from router import ModelRouter
from tenants import DEFAULT_TIERS, Quota, Tenant, TenantRegistry, hash_key


# --------------------------------------------------------------------------
# Key handling
# --------------------------------------------------------------------------
def test_only_a_hash_of_each_key_is_stored():
    """A gateway holding reversible customer credentials is a breach waiting."""
    registry = TenantRegistry.demo()
    stored = json.dumps(registry._keys)
    assert "key-acme" not in stored
    assert hash_key("key-acme") in stored


def test_a_valid_key_resolves_to_its_tenant_and_quota():
    """A key must yield both identity and entitlement in one lookup."""
    registry = TenantRegistry.demo()
    tenant = registry.resolve("key-acme")
    assert tenant.tenant_id == "tenant-acme"
    assert tenant.tier == "pro"
    assert tenant.quota == DEFAULT_TIERS["pro"]


@pytest.mark.parametrize("presented", [None, "", "wrong", "key-acm", "key-acme ", "KEY-ACME"])
def test_an_unknown_or_near_miss_key_resolves_to_nothing(presented):
    """Resolution is exact; a near miss is not a partial credit."""
    assert TenantRegistry.demo().resolve(presented) is None


def test_keys_may_be_supplied_pre_hashed():
    """So a production config never has to contain the plaintext at all."""
    registry = TenantRegistry.from_dict(
        {"tenants": [{"tenant_id": "t", "tier": "free",
                      "api_keys": [hash_key("the-real-key")]}]}
    )
    assert registry.resolve("the-real-key").tenant_id == "t"


def test_one_key_cannot_belong_to_two_tenants():
    """Ambiguous ownership would make billing and limits meaningless."""
    with pytest.raises(ValueError):
        TenantRegistry.from_dict(
            {"tenants": [{"tenant_id": "a", "api_keys": ["same"]},
                         {"tenant_id": "b", "api_keys": ["same"]}]}
        )


def test_a_tenant_may_hold_several_keys():
    """Key rotation needs two live keys at once."""
    registry = TenantRegistry.from_dict(
        {"tenants": [{"tenant_id": "t", "api_keys": ["old-key", "new-key"]}]}
    )
    assert registry.resolve("old-key").tenant_id == "t"
    assert registry.resolve("new-key").tenant_id == "t"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def test_tiers_give_tenants_different_budgets():
    """The point of tiers: free and pro are not the same product."""
    registry = TenantRegistry.demo()
    assert (registry.resolve("key-globex").quota.tokens_per_minute
            < registry.resolve("key-acme").quota.tokens_per_minute)


def test_a_per_tenant_override_beats_its_tier():
    """A negotiated limit must not require inventing a new tier."""
    registry = TenantRegistry.from_dict(
        {"tenants": [{"tenant_id": "t", "tier": "free", "api_keys": ["k"],
                      "overrides": {"tokens_per_minute": 777}}]}
    )
    quota = registry.resolve("k").quota
    assert quota.tokens_per_minute == 777
    assert quota.requests_per_minute == DEFAULT_TIERS["free"].requests_per_minute


def test_a_custom_tier_can_be_declared():
    """Deployments need their own tiers without a code change."""
    registry = TenantRegistry.from_dict(
        {"tiers": {"titanium": {"tokens_per_minute": 9_000_000, "requests_per_minute": 9000}},
         "tenants": [{"tenant_id": "t", "tier": "titanium", "api_keys": ["k"]}]}
    )
    assert registry.resolve("k").quota.tokens_per_minute == 9_000_000


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"tenants": []},
        {"tenants": [{"tier": "free", "api_keys": ["k"]}]},
        {"tenants": [{"tenant_id": "t", "api_keys": []}]},
        {"tenants": [{"tenant_id": "t", "tier": "nope", "api_keys": ["k"]}]},
        {"tenants": [{"tenant_id": "t", "api_keys": [123]}]},
    ],
)
def test_malformed_configuration_is_refused_at_load(payload):
    """A broken quota file must fail at startup, not mid-request."""
    with pytest.raises(ValueError):
        TenantRegistry.from_dict(payload)


@pytest.mark.parametrize("field", ["tokens_per_minute", "requests_per_minute", "max_concurrent"])
def test_a_non_positive_quota_is_refused(field):
    """A zero budget would admit nothing and look like an outage."""
    with pytest.raises(ValueError):
        Quota(**{**{"tokens_per_minute": 1, "requests_per_minute": 1, "max_concurrent": 1},
                 field: 0})


def test_a_registry_loads_from_a_file(tmp_path):
    """The production path is a JSON file, so it has to actually load."""
    path = tmp_path / "tenants.json"
    path.write_text(json.dumps(
        {"tenants": [{"tenant_id": "filed", "tier": "enterprise", "api_keys": ["fk"]}]}))
    registry = TenantRegistry.from_file(path)
    assert registry.resolve("fk").tier == "enterprise"


def test_a_missing_file_is_a_clear_error(tmp_path):
    """A typo in TENANTS_FILE must say so, not fall back to demo keys silently."""
    with pytest.raises(ValueError):
        TenantRegistry.from_file(tmp_path / "absent.json")


def test_the_environment_falls_back_to_the_demo_registry(monkeypatch):
    """The service must still start with no quota file configured."""
    monkeypatch.delenv("TENANTS_FILE", raising=False)
    assert TenantRegistry.from_env().tenant_ids == ["tenant-acme", "tenant-globex"]


# --------------------------------------------------------------------------
# Enforcement: the limiter honours each tenant's own quota
# --------------------------------------------------------------------------
def test_the_limiter_applies_each_tenants_own_token_budget(db_path, clock):
    """The defect this fixes: every tenant previously shared one global limit."""
    limiter = RateLimiter(db_path=db_path, clock=clock)
    registry = TenantRegistry.demo()
    pro = registry.resolve("key-acme").quota        # 50,000
    free = registry.resolve("key-globex").quota     # 10,000
    try:
        assert limiter.try_consume("acme", 40_000, pro).allowed
        refused = limiter.try_consume("globex", 40_000, free)
        assert not refused.allowed
        assert refused.reason == "tokens"
        assert refused.limit_tokens == 10_000
    finally:
        limiter.close()


def test_the_request_dimension_is_enforced_independently(db_path, clock):
    """Many tiny prompts exhaust connections long before they exhaust tokens."""
    limiter = RateLimiter(db_path=db_path, clock=clock,
                          limit_tokens=10**9, limit_requests=3)
    try:
        assert all(limiter.try_consume("t", 1).allowed for _ in range(3))
        refused = limiter.try_consume("t", 1)
        assert not refused.allowed
        assert refused.reason == "requests"
        assert refused.remaining_requests == 0
    finally:
        limiter.close()


def test_the_request_dimension_also_slides(db_path, clock):
    """The request budget uses the same sliding window as tokens, not a fixed one."""
    limiter = RateLimiter(db_path=db_path, clock=clock,
                          limit_tokens=10**9, limit_requests=2)
    try:
        limiter.try_consume("t", 1)
        limiter.try_consume("t", 1)
        assert not limiter.try_consume("t", 1).allowed
        clock.advance(61)
        assert limiter.try_consume("t", 1).allowed
    finally:
        limiter.close()


def test_a_request_refusal_reports_when_a_slot_frees(db_path, clock):
    """Retry-after for a request refusal is when the oldest row expires."""
    limiter = RateLimiter(db_path=db_path, clock=clock,
                          limit_tokens=10**9, limit_requests=1)
    try:
        limiter.try_consume("t", 1)
        clock.advance(10)
        refused = limiter.try_consume("t", 1)
        assert refused.reason == "requests"
        assert refused.retry_after_seconds == pytest.approx(50.0, abs=0.01)
    finally:
        limiter.close()


def test_batched_admissions_each_use_their_own_tenant_quota(db_path, clock):
    """Group commit must not blur two tenants' limits together."""
    limiter = RateLimiter(db_path=db_path, clock=clock)
    registry = TenantRegistry.demo()
    pro = registry.resolve("key-acme").quota
    free = registry.resolve("key-globex").quota
    try:
        decisions = limiter.try_consume_many(
            [("globex", 9_000, free), ("globex", 9_000, free), ("acme", 40_000, pro)]
        )
        assert [d.allowed for d in decisions] == [True, False, True]
        assert decisions[1].limit_tokens == 10_000
    finally:
        limiter.close()


def test_omitting_a_quota_keeps_the_limiter_defaults(db_path, clock):
    """Callers that predate per-tenant quotas must behave exactly as before."""
    limiter = RateLimiter(db_path=db_path, clock=clock)
    try:
        assert limiter.try_consume("t", 50_000).allowed
        assert not limiter.try_consume("t", 1).allowed
    finally:
        limiter.close()


# --------------------------------------------------------------------------
# Enforcement end to end
# --------------------------------------------------------------------------
@pytest.fixture
def quota_app(db_path, clock):
    """A gateway using the demo registry, so the two tiers are both live."""
    limiter = RateLimiter(db_path=db_path, clock=clock)
    application = app_module.create_app(
        limiter=limiter,
        router=ModelRouter(StubProvider("primary", tokens_used=50),
                           StubProvider("secondary"), timeout_ms=1000),
        registry=TenantRegistry.demo(),
    )
    application.state.test_limiter = limiter
    try:
        yield application
    finally:
        limiter.close()


@pytest.fixture
async def quota_client(quota_app):
    """An httpx client wired to the quota-aware gateway."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=quota_app), base_url="http://g.test"
    ) as client:
        yield client


async def test_the_response_names_the_tenant_and_its_tier(quota_client):
    """A caller should be able to see which plan their request was judged against."""
    response = await quota_client.post(
        "/v1/complete", json={"prompt": "hello", "max_tokens": 16},
        headers={"X-API-Key": "key-acme"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["tenant"] == {"id": "tenant-acme", "tier": "pro"}
    assert body["usage"]["limit_tokens"] == 50_000


async def test_a_free_tenant_is_limited_sooner_than_a_pro_tenant(quota_client, quota_app):
    """The end-to-end proof that quotas are per tenant."""
    quota_app.state.test_limiter.try_consume(
        "tenant-globex", 9_950, TenantRegistry.demo().resolve("key-globex").quota)
    free = await quota_client.post(
        "/v1/complete", json={"prompt": "x" * 400, "max_tokens": 500},
        headers={"X-API-Key": "key-globex"})
    pro = await quota_client.post(
        "/v1/complete", json={"prompt": "x" * 400, "max_tokens": 500},
        headers={"X-API-Key": "key-acme"})
    assert free.status_code == 429
    assert pro.status_code == 200
    assert free.json()["error"]["details"]["tier"] == "free"
    assert free.json()["error"]["details"]["limit"] == "tokens"


async def test_the_quota_endpoint_reports_a_tenants_own_limits(quota_client):
    """Tenants need to read their own budget without asking an operator."""
    response = await quota_client.get("/v1/quota", headers={"X-API-Key": "key-globex"})
    assert response.status_code == 200
    body = response.json()
    assert body["tier"] == "free"
    assert body["limits"]["tokens_per_minute"] == 10_000
    assert "used_tokens" in body


async def test_the_quota_endpoint_requires_a_key(quota_client):
    """Quota is tenant-scoped data, so it is not readable anonymously."""
    response = await quota_client.get("/v1/quota")
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "unauthenticated"


async def test_an_api_key_is_never_echoed_or_logged(quota_client, caplog):
    """A leaked credential in a log line is a credential leak."""
    import logging

    with caplog.at_level(logging.WARNING):
        response = await quota_client.post(
            "/v1/complete", json={"prompt": "hi", "max_tokens": 8},
            headers={"X-API-Key": "super-secret-key-value"})
    assert response.status_code == 401
    assert "super-secret-key-value" not in response.text
    assert "super-secret-key-value" not in caplog.text
