"""Tenant identity and per-tenant quotas.

What this replaces
------------------
The gateway previously held a two-entry dict of plaintext API keys in source,
and a single global token limit read from one environment variable -- so every
tenant shared the same 50,000 tokens/minute and keys were compared with ``==``.
For a multi-tenant gateway, per-tenant quotas *are* the product, so they are
configuration now, not a constant.

Key handling
------------
Only a SHA-256 hash of each key is stored, and lookup is constant-time.
A gateway holding reversible customer credentials is a breach waiting to
happen, and an early-exit comparison leaks the key a byte at a time. Keys are
never logged -- only the tenant id they resolve to.

Hashing is unsalted and unstretched on purpose: these are high-entropy
machine-generated secrets, not user-chosen passwords, so there is no dictionary
to defend against and per-request Argon2 would price every call. If keys ever
become human-chosen, this is the line to revisit.

Configuration
-------------
``TENANTS_FILE`` points at a JSON document::

    {
      "tiers": {"pro": {"tokens_per_minute": 200000, "requests_per_minute": 600}},
      "tenants": [
        {"tenant_id": "acme", "tier": "pro", "api_keys": ["key-acme-live-..."]},
        {"tenant_id": "globex", "tier": "free",
         "api_keys": ["key-globex-..."],
         "overrides": {"tokens_per_minute": 10000}}
      ]
    }

Keys may be given as plaintext (hashed on load) or pre-hashed as
``"sha256:<hex>"``, so a production deployment never has to put the plaintext
in the file at all. With no file configured, a clearly-labelled demo registry
is used so the service still starts and the tests have fixtures.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path

logger = logging.getLogger("fde.tenants")

HASH_PREFIX = "sha256:"


@dataclass(frozen=True)
class Quota:
    """One tenant's limits, across every dimension the gateway enforces.

    Attributes:
        tokens_per_minute: Token budget in the sliding window.
        requests_per_minute: Request-count budget in the same window. A tenant
            sending many tiny prompts can exhaust a provider's connections
            long before it exhausts a token budget, so tokens alone are not
            enough.
        max_concurrent: Simultaneous in-flight requests. Caps a single tenant's
            share of the worker's capacity.
    """

    tokens_per_minute: int
    requests_per_minute: int
    max_concurrent: int

    def __post_init__(self) -> None:
        """Reject a quota that would admit nothing or be unbounded by accident."""
        for field_name in ("tokens_per_minute", "requests_per_minute", "max_concurrent"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{field_name} must be a positive integer, got {value!r}")


# Shipped tiers. 50,000 tokens/minute is kept as the "pro" figure because that
# is the number the assessment specifies; the others bracket it.
DEFAULT_TIERS: dict[str, Quota] = {
    "free": Quota(tokens_per_minute=10_000, requests_per_minute=60, max_concurrent=4),
    "pro": Quota(tokens_per_minute=50_000, requests_per_minute=300, max_concurrent=16),
    "enterprise": Quota(tokens_per_minute=500_000, requests_per_minute=3_000, max_concurrent=64),
}


@dataclass(frozen=True)
class Tenant:
    """A resolved caller: who they are and what they are allowed."""

    tenant_id: str
    tier: str
    quota: Quota


def hash_key(api_key: str) -> str:
    """Hash an API key for storage and comparison."""
    return HASH_PREFIX + hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _normalise_key(raw: str) -> str:
    """Accept either a plaintext key or an already-hashed ``sha256:`` entry."""
    return raw if raw.startswith(HASH_PREFIX) else hash_key(raw)


class TenantRegistry:
    """Maps an API key to a :class:`Tenant`, in constant time."""

    def __init__(self, tenants: list[Tenant], keys: dict[str, str]) -> None:
        """Build a registry.

        Args:
            tenants: Every known tenant.
            keys: Hashed key -> tenant id.

        Raises:
            ValueError: A key maps to a tenant that was not supplied.
        """
        self._tenants = {tenant.tenant_id: tenant for tenant in tenants}
        unknown = set(keys.values()) - set(self._tenants)
        if unknown:
            raise ValueError(f"api keys reference unknown tenants: {sorted(unknown)}")
        self._keys = dict(keys)

    def __len__(self) -> int:
        """Number of tenants in the registry."""
        return len(self._tenants)

    @property
    def tenant_ids(self) -> list[str]:
        """Known tenant ids, sorted."""
        return sorted(self._tenants)

    def resolve(self, api_key: str | None) -> Tenant | None:
        """Resolve a presented key to a tenant, or ``None``.

        The comparison is constant-time across every stored key, so the time
        taken does not reveal how much of a guessed key was correct. An absent
        or empty key resolves to ``None`` without touching the table.
        """
        if not api_key:
            return None
        presented = hash_key(api_key)
        matched: str | None = None
        for stored_hash, tenant_id in self._keys.items():
            # compare_digest on every entry, and no early return, so the loop
            # runs the same way whether or not a match was found.
            if hmac.compare_digest(stored_hash, presented):
                matched = tenant_id
        return self._tenants.get(matched) if matched else None

    def get(self, tenant_id: str) -> Tenant | None:
        """Look a tenant up by id, for admin surfaces."""
        return self._tenants.get(tenant_id)

    # -- construction -----------------------------------------------------
    @classmethod
    def from_dict(cls, payload: dict) -> "TenantRegistry":
        """Build a registry from the documented JSON shape.

        Raises:
            ValueError: The payload is malformed, names an unknown tier, or
                reuses one API key across two tenants.
        """
        if not isinstance(payload, dict):
            raise ValueError("tenant configuration must be a JSON object")

        tiers = dict(DEFAULT_TIERS)
        for name, spec in (payload.get("tiers") or {}).items():
            if not isinstance(spec, dict):
                raise ValueError(f"tier {name!r} must be an object")
            base = tiers.get(name, DEFAULT_TIERS["free"])
            tiers[name] = replace(
                base,
                **{k: v for k, v in spec.items()
                   if k in ("tokens_per_minute", "requests_per_minute", "max_concurrent")},
            )

        tenants: list[Tenant] = []
        keys: dict[str, str] = {}
        entries = payload.get("tenants")
        if not isinstance(entries, list) or not entries:
            raise ValueError("tenant configuration needs a non-empty 'tenants' list")

        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("each tenant must be an object")
            tenant_id = entry.get("tenant_id")
            if not isinstance(tenant_id, str) or not tenant_id:
                raise ValueError("each tenant needs a non-empty string tenant_id")
            tier = entry.get("tier", "free")
            if tier not in tiers:
                raise ValueError(f"tenant {tenant_id!r} names unknown tier {tier!r}")
            quota = tiers[tier]
            overrides = entry.get("overrides") or {}
            if overrides:
                quota = replace(
                    quota,
                    **{k: v for k, v in overrides.items()
                       if k in ("tokens_per_minute", "requests_per_minute", "max_concurrent")},
                )
            tenants.append(Tenant(tenant_id=tenant_id, tier=tier, quota=quota))

            raw_keys = entry.get("api_keys") or []
            if not isinstance(raw_keys, list) or not raw_keys:
                raise ValueError(f"tenant {tenant_id!r} needs at least one api key")
            for raw in raw_keys:
                if not isinstance(raw, str) or not raw:
                    raise ValueError(f"tenant {tenant_id!r} has a non-string api key")
                hashed = _normalise_key(raw)
                if hashed in keys and keys[hashed] != tenant_id:
                    raise ValueError("the same api key is assigned to two tenants")
                keys[hashed] = tenant_id

        return cls(tenants, keys)

    @classmethod
    def from_file(cls, path: str | Path) -> "TenantRegistry":
        """Load a registry from a JSON file.

        Raises:
            ValueError: The file is missing or not valid JSON.
        """
        file_path = Path(path)
        if not file_path.is_file():
            raise ValueError(f"tenant configuration not found: {file_path}")
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"tenant configuration is not valid JSON: {exc}") from exc
        return cls.from_dict(payload)

    @classmethod
    def demo(cls) -> "TenantRegistry":
        """A clearly-labelled development registry, used when none is configured.

        Two tenants on different tiers, so the per-tenant behaviour is visible
        in the demo rather than only in the tests. The keys are obvious
        placeholders and the gateway logs a warning when this is in use.
        """
        return cls.from_dict(
            {
                "tenants": [
                    {"tenant_id": "tenant-acme", "tier": "pro", "api_keys": ["key-acme"]},
                    {"tenant_id": "tenant-globex", "tier": "free", "api_keys": ["key-globex"]},
                ]
            }
        )

    @classmethod
    def from_env(cls) -> "TenantRegistry":
        """Build from ``TENANTS_FILE``, falling back to the demo registry."""
        configured = os.environ.get("TENANTS_FILE")
        if configured:
            registry = cls.from_file(configured)
            logger.info("loaded %d tenants from %s", len(registry), configured)
            return registry
        logger.warning(
            "TENANTS_FILE is not set - using the built-in demo tenants. "
            "Do not run this configuration in production."
        )
        return cls.demo()
