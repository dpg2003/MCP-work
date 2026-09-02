"""HTTP-level tests: real streaming, TTFT, and mid-stream failure handling."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app import ERROR_SENTINEL, create_app
from conftest import EMAIL, SSN, VISA_SPACED, live_server
from providers import MockProvider, SSEMockProvider, UpstreamStreamError
from redactor import REPLACEMENT


def build_client(provider, **kwargs) -> httpx.AsyncClient:
    """An httpx client wired to a gateway using ``provider``."""
    app = create_app(provider=provider, **kwargs)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    )


async def collect(provider, prompt: str = "hello", **kwargs) -> str:
    """Stream a full response and return it as one string."""
    async with build_client(provider, **kwargs) as client:
        async with client.stream("POST", "/v1/generate", json={"prompt": prompt}) as response:
            assert response.status_code == 200
            return "".join([chunk async for chunk in response.aiter_text()])


# --------------------------------------------------------------------------
# Streaming redaction end to end
# --------------------------------------------------------------------------
async def test_split_pii_is_redacted_over_http():
    """The end-to-end proof over real HTTP, with near-miss text preserved alongside the redactions."""
    provider = MockProvider(
        chunks=[
            "Her email is john.doe@exam",
            "ple.com and her ssn is 123-",
            "45-6789 with card 4111 1111 ",
            "1111 1111. Ticket 123456789 at 192.168.1.1.",
        ]
    )
    body = await collect(provider)
    assert body == (
        f"Her email is {REPLACEMENT} and her ssn is {REPLACEMENT} with card "
        f"{REPLACEMENT}. Ticket 123456789 at 192.168.1.1."
    )
    for secret in (EMAIL, SSN, "4111", "exam", "ple.com"):
        assert secret not in body


async def test_response_is_chunked_not_one_blob():
    """Chunks reach the client as they settle, not as one buffered body.

    ``aiter_raw`` is used rather than ``aiter_bytes``: the latter runs the body
    through a decoder that coalesces, which would hide exactly what is being
    asserted here.
    """
    provider = MockProvider(
        chunks=[f"part {index} of a long answer. " for index in range(40)],
        delay_seconds=0.005,
    )
    async with live_server(create_app(provider=provider)) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as client:
            async with client.stream("POST", "/v1/generate", json={"prompt": "x"}) as response:
                received = [chunk async for chunk in response.aiter_raw() if chunk]
    assert len(received) > 1, "the response arrived as a single blob"
    assert b"".join(received).startswith(b"part 0 of a long answer. ")


async def test_healthz():
    """The liveness probe answers and names the active provider."""
    async with build_client(MockProvider(chunks=["hi"])) as client:
        response = await client.get("/healthz")
    assert response.json()["status"] == "ok"


async def test_empty_prompt_is_rejected():
    """An empty prompt is a validation failure, not a request to the provider."""
    async with build_client(MockProvider(chunks=["hi"])) as client:
        response = await client.post("/v1/generate", json={"prompt": ""})
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Time to first token
# --------------------------------------------------------------------------
async def test_first_chunk_arrives_before_the_stream_finishes():
    """The gateway must not buffer the whole response before flushing."""
    total_chunks = 20
    per_chunk_delay = 0.05
    provider = MockProvider(
        chunks=[f"sentence {index} with ordinary prose in it. " for index in range(total_chunks)],
        delay_seconds=per_chunk_delay,
    )
    async with live_server(create_app(provider=provider)) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as client:
            start = time.perf_counter()
            async with client.stream("POST", "/v1/generate", json={"prompt": "x"}) as response:
                first_at = None
                stream = response.aiter_raw()
                async for chunk in stream:
                    if chunk:
                        first_at = time.perf_counter() - start
                        break
                async for _ in stream:  # drain the rest through the same iterator
                    pass
            total = time.perf_counter() - start

    full_stream_time = total_chunks * per_chunk_delay
    assert first_at is not None
    # Arrived after roughly one upstream chunk, not after all of them.
    assert first_at < full_stream_time / 3, f"TTFT {first_at:.3f}s of {total:.3f}s total"


async def test_ttft_is_not_delayed_by_the_hold_back_window():
    """Prose cannot start a PII pattern, so the first chunk flushes whole."""
    provider = MockProvider(
        chunks=["Hello there, how can I help? ", "More text follows."], delay_seconds=0.02
    )
    async with live_server(create_app(provider=provider)) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as client:
            async with client.stream("POST", "/v1/generate", json={"prompt": "x"}) as response:
                first = None
                stream = response.aiter_raw()
                async for chunk in stream:
                    if chunk:
                        first = chunk
                        break
                async for _ in stream:
                    pass
    assert first == b"Hello there, how can I help? "


# --------------------------------------------------------------------------
# Upstream failures
# --------------------------------------------------------------------------
async def test_upstream_drops_mid_stream_closes_cleanly_with_an_error():
    """A mid-stream failure ends with a sentinel and a closed connection rather than a hang, carrying none of the upstream's words."""
    provider = MockProvider(
        chunks=["Some safe text. ", "More safe text. "],
        fail_after=2,
        failure=UpstreamStreamError("connection reset by peer"),
    )
    body = await asyncio.wait_for(collect(provider), timeout=10)
    assert body.startswith("Some safe text. More safe text. ")
    assert body.endswith(ERROR_SENTINEL)
    # Nothing about the upstream failure leaks.
    assert "connection reset" not in body and "Traceback" not in body


async def test_partial_pii_in_the_tail_is_redacted_even_when_upstream_dies():
    """The dangerous case: the stream fails while PII is still held back."""
    provider = MockProvider(
        chunks=["Her address is john.doe@example.com"],
        fail_after=1,
        failure=UpstreamStreamError("boom"),
    )
    body = await collect(provider)
    assert body == f"Her address is {REPLACEMENT}{ERROR_SENTINEL}"
    assert EMAIL not in body


async def test_undecodable_upstream_event_is_handled():
    """A corrupt provider frame stops the stream cleanly; text after the bad frame is never emitted."""
    provider = SSEMockProvider(
        frames=[
            'data: {"type":"content_block_delta","delta":{"text":"Hello "}}',
            "data: {this is not json",
            'data: {"type":"content_block_delta","delta":{"text":"never seen"}}',
        ]
    )
    body = await asyncio.wait_for(collect(provider), timeout=10)
    assert body == "Hello " + ERROR_SENTINEL
    assert "never seen" not in body


async def test_sse_provider_happy_path_redacts():
    """The SSE parsing path redacts identically to the plain one, including across frames."""
    provider = SSEMockProvider(
        frames=[
            'data: {"type":"content_block_delta","delta":{"text":"ssn 123-"}}',
            'data: {"type":"content_block_delta","delta":{"text":"45-6789 done"}}',
            "data: [DONE]",
        ]
    )
    assert await collect(provider) == f"ssn {REPLACEMENT} done"


async def test_unexpected_provider_exception_is_sanitized():
    """An unexpected exception type is caught too, and its message, which carries a password and hostname, does not reach the client."""
    provider = MockProvider(
        chunks=["safe "],
        fail_after=1,
        failure=RuntimeError("DB_PASSWORD=hunter2 at db-primary.internal:5432"),
    )
    body = await collect(provider)
    assert body == "safe " + ERROR_SENTINEL
    for secret in ("hunter2", "db-primary.internal", "RuntimeError", "Traceback"):
        assert secret not in body


async def test_provider_unavailable_before_any_bytes_gives_a_502():
    """Failing before the first byte still allows a real status code, so this path returns 502 rather than a sentinel."""
    class DeadProvider:
        """A provider that fails before yielding any bytes at all."""

        def stream(self, prompt):
            """Fail immediately, while a real status code is still available."""
            raise UpstreamStreamError("ANTHROPIC_API_KEY is not set")

    async with build_client(DeadProvider()) as client:
        response = await client.post("/v1/generate", json={"prompt": "x"})
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_unavailable"
    assert "ANTHROPIC_API_KEY" not in response.text


# --------------------------------------------------------------------------
# Memory over HTTP
# --------------------------------------------------------------------------
async def test_long_stream_keeps_gateway_memory_bounded():
    """A quarter-megabyte response streams through without the gateway accumulating it."""
    chunk_count = 5_000
    provider = MockProvider(
        chunks=[f"line {index}: the quick brown fox jumps over the lazy dog. " for index in range(chunk_count)]
    )
    total = 0
    async with build_client(provider) as client:
        async with client.stream("POST", "/v1/generate", json={"prompt": "x"}) as response:
            async for chunk in response.aiter_bytes():
                total += len(chunk)
    assert total > 250_000
