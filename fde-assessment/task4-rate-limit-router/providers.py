"""Pluggable model providers.

The router only knows the ``ModelProvider`` interface, so a real endpoint, a
fake HTTP endpoint with injected latency and failures, or an in-process stub
are all interchangeable.

Provider-specific failures are normalised into three gateway-owned exceptions
right at this boundary. Nothing above this layer ever sees an ``httpx``
exception, an upstream status code, or an upstream response body — which is
what makes the "no leaked internals" guarantee structural rather than a
promise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

logger = logging.getLogger("fde.router.providers")


class ProviderError(Exception):
    """Base for every normalised provider failure."""

    def __init__(self, provider: str, internal_detail: str) -> None:
        super().__init__(f"{provider}: {internal_detail}")
        self.provider = provider
        # For the gateway's own logs only.
        self.internal_detail = internal_detail


class ProviderTimeout(ProviderError):
    """The provider did not answer inside the configured budget."""


class ProviderRateLimited(ProviderError):
    """The provider returned HTTP 429."""


class ProviderUnavailable(ProviderError):
    """Connection failure, 5xx, or an undecodable body."""


class ProviderRejected(ProviderError):
    """A non-retryable 4xx. Failing over would just fail again."""


@dataclass
class Completion:
    """Normalised successful response."""

    text: str
    tokens_used: int
    provider: str


class ModelProvider(Protocol):
    name: str

    async def complete(self, prompt: str, max_tokens: int) -> Completion: ...


@dataclass
class HttpModelProvider:
    """Talks to a real (or fake) HTTP completion endpoint.

    ``timeout_seconds`` is enforced here rather than in the router so that the
    deadline covers connect, write, read, and pool-acquire time — a router-level
    ``asyncio.wait_for`` would cancel the task but leave the socket to be
    reaped later.
    """

    name: str
    url: str
    client: httpx.AsyncClient
    timeout_seconds: float = 3.0
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def reset(self) -> None:
        self.calls.clear()

    async def complete(self, prompt: str, max_tokens: int) -> Completion:
        self.calls.append({"prompt": prompt, "max_tokens": max_tokens})
        try:
            response = await self.client.post(
                self.url,
                json={"prompt": prompt, "max_tokens": max_tokens},
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(self.name, f"{type(exc).__name__}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(self.name, f"{type(exc).__name__}: {exc}") from exc

        if response.status_code == 429:
            raise ProviderRateLimited(self.name, f"429: {response.text[:200]!r}")
        if response.status_code >= 500:
            raise ProviderUnavailable(
                self.name, f"{response.status_code}: {response.text[:200]!r}"
            )
        if response.status_code >= 400:
            raise ProviderRejected(self.name, f"{response.status_code}: {response.text[:200]!r}")

        try:
            body = response.json()
            return Completion(
                text=str(body["text"]),
                tokens_used=int(body.get("tokens_used", 0)),
                provider=self.name,
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderUnavailable(
                self.name, f"undecodable body: {response.text[:200]!r}"
            ) from exc


@dataclass
class StubProvider:
    """In-process provider for tests that do not need an HTTP hop."""

    name: str
    text: str = "ok"
    tokens_used: int = 10
    error: Exception | None = None
    delay_seconds: float = 0.0
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def reset(self) -> None:
        self.calls.clear()

    async def complete(self, prompt: str, max_tokens: int) -> Completion:
        import asyncio

        self.calls.append({"prompt": prompt, "max_tokens": max_tokens})
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.error is not None:
            raise self.error
        return Completion(text=self.text, tokens_used=self.tokens_used, provider=self.name)
