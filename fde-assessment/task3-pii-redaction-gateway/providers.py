"""Upstream text-generation providers.

Two implementations behind one interface (``AsyncIterator[str]`` of text
deltas), selected by the ``LLM_PROVIDER`` environment variable:

* ``mock``      -- a scripted stream with configurable chunking, per-chunk
                   latency, and failure injection. The default, so the gateway
                   runs and is testable with no API key.
* ``anthropic`` -- the real Anthropic Messages API with streaming enabled.

Keeping the provider boundary at "async iterator of text deltas" is what lets
the redaction layer be tested exhaustively without any network at all.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field


class UpstreamStreamError(Exception):
    """The upstream stream failed or produced something undecodable."""


@dataclass
class MockProvider:
    """Scripted stream. The chunk boundaries are the whole point of the tests."""

    chunks: Sequence[str] = field(default_factory=list)
    delay_seconds: float = 0.0
    fail_after: int | None = None
    failure: Exception | None = None

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        for index, chunk in enumerate(self.chunks):
            if self.fail_after is not None and index >= self.fail_after:
                raise self.failure or UpstreamStreamError("upstream dropped the connection")
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            yield chunk
        if self.fail_after is not None and self.fail_after >= len(self.chunks):
            raise self.failure or UpstreamStreamError("upstream dropped the connection")


@dataclass
class SSEMockProvider:
    """Mock that emits raw SSE frames, so decode failures can be exercised.

    Real providers hand you framed events, and a truncated or corrupt frame is
    a genuine failure mode. This provider parses its own frames the way the
    Anthropic path does, so ``UpstreamStreamError`` is raised from the same
    place either way.
    """

    frames: Sequence[str] = field(default_factory=list)
    delay_seconds: float = 0.0

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        for frame in self.frames:
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            if not frame.startswith("data: "):
                continue
            body = frame[len("data: ") :].strip()
            if body == "[DONE]":
                return
            try:
                event = json.loads(body)
            except ValueError as exc:
                raise UpstreamStreamError("undecodable event from upstream") from exc
            if not isinstance(event, dict):
                raise UpstreamStreamError("upstream event was not an object")
            if event.get("type") == "content_block_delta":
                delta = event.get("delta") or {}
                text = delta.get("text")
                if isinstance(text, str):
                    yield text


DEFAULT_SCRIPT = [
    "Sure — here is the customer summary you asked for.\n\n",
    "The account owner is Jane Roe and her contact address is jane.r",
    "oe@example.com. She last logged in on Tuesday.\n",
    "Her social security number on file is 123-",
    "45-",
    "6789, and the card ending in the visible digits is ",
    "4111 1111 ",
    "1111 1111.\n",
    "Her support ticket reference is 123456789 and the server she reported ",
    "the issue from was 192.168.1.1 running v1.2.3 — neither of which is PII.\n",
]


class AnthropicProvider:
    """Real Anthropic streaming. Requires ``ANTHROPIC_API_KEY``."""

    def __init__(self, model: str | None = None, max_tokens: int = 1024) -> None:
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
        self.max_tokens = max_tokens
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:  # pragma: no cover
                raise UpstreamStreamError("anthropic SDK is not installed") from exc
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise UpstreamStreamError("ANTHROPIC_API_KEY is not set")
            self._client = AsyncAnthropic()
        return self._client

    async def stream(self, prompt: str) -> AsyncIterator[str]:  # pragma: no cover - needs a key
        client = self._get_client()
        try:
            async with client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        except Exception as exc:
            # Never let a provider-specific exception shape reach the client.
            raise UpstreamStreamError(f"anthropic stream failed: {type(exc).__name__}") from exc


def provider_from_env():
    """Build the provider named by ``LLM_PROVIDER`` (default ``mock``)."""
    name = os.environ.get("LLM_PROVIDER", "mock").lower()
    if name == "anthropic":
        return AnthropicProvider()
    if name == "mock":
        return MockProvider(
            chunks=DEFAULT_SCRIPT,
            delay_seconds=float(os.environ.get("MOCK_CHUNK_DELAY_SECONDS", "0.05")),
        )
    raise ValueError(f"unknown LLM_PROVIDER: {name!r} (expected 'mock' or 'anthropic')")
