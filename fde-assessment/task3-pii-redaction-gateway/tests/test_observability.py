"""Correlation ids and operational behaviour of the streaming gateway.

A streamed response commits its status code with the first byte, so when
something fails mid-stream the only thing a user can quote is the request id.
These tests make sure that id exists, is stable, and reaches both the response
and the logs.
"""

from __future__ import annotations

import logging
import re

import httpx
import pytest

from app import ERROR_SENTINEL, REQUEST_ID_HEADER, create_app, new_request_id
from providers import MockProvider, UpstreamStreamError


def build_client(provider, **kwargs) -> httpx.AsyncClient:
    """An httpx client wired to a gateway using ``provider``."""
    app = create_app(provider=provider, **kwargs)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    )


# --------------------------------------------------------------------------
# Correlation ids
# --------------------------------------------------------------------------
async def test_every_response_carries_a_request_id():
    """A generated id is present and well-formed on an ordinary response."""
    async with build_client(MockProvider(chunks=["hello"])) as client:
        async with client.stream("POST", "/v1/generate", json={"prompt": "x"}) as response:
            assert response.status_code == 200
            request_id = response.headers[REQUEST_ID_HEADER]
            async for _ in response.aiter_bytes():
                pass
    assert re.fullmatch(r"req_[0-9a-f]{16}", request_id), request_id


async def test_a_caller_supplied_request_id_is_honoured():
    """Lets a caller stitch this hop into a trace they already started."""
    async with build_client(MockProvider(chunks=["hello"])) as client:
        async with client.stream(
            "POST", "/v1/generate", json={"prompt": "x"},
            headers={REQUEST_ID_HEADER: "req_from_the_caller"},
        ) as response:
            assert response.headers[REQUEST_ID_HEADER] == "req_from_the_caller"
            async for _ in response.aiter_bytes():
                pass


async def test_request_ids_are_unique_across_requests():
    """Ids do not repeat, so they can actually identify a single request."""
    seen = set()
    async with build_client(MockProvider(chunks=["hi"])) as client:
        for _ in range(25):
            async with client.stream("POST", "/v1/generate", json={"prompt": "x"}) as response:
                seen.add(response.headers[REQUEST_ID_HEADER])
                async for _ in response.aiter_bytes():
                    pass
    assert len(seen) == 25


async def test_a_pre_stream_failure_reports_its_request_id_in_the_body():
    """A pre-stream failure carries its id in both the header and the body, and still leaks nothing."""
    class DeadProvider:
        """A provider that fails before yielding any bytes."""

        def stream(self, prompt):
            """Fail immediately."""
            raise UpstreamStreamError("no api key")

    async with build_client(DeadProvider()) as client:
        response = await client.post("/v1/generate", json={"prompt": "x"})
    assert response.status_code == 502
    body = response.json()
    assert body["error"]["request_id"] == response.headers[REQUEST_ID_HEADER]
    assert "no api key" not in response.text


async def test_a_mid_stream_failure_is_logged_against_the_request_id(caplog):
    """The failure a user can only report by its id must be findable by that id."""
    provider = MockProvider(
        chunks=["safe text "], fail_after=1, failure=UpstreamStreamError("upstream died")
    )
    with caplog.at_level(logging.ERROR, logger="fde.pii_gateway"):
        async with build_client(provider) as client:
            async with client.stream("POST", "/v1/generate", json={"prompt": "x"}) as response:
                request_id = response.headers[REQUEST_ID_HEADER]
                body = "".join([chunk async for chunk in response.aiter_text()])

    assert body.endswith(ERROR_SENTINEL)
    assert any(request_id in record.getMessage() for record in caplog.records), caplog.text
    # The upstream's own words still never reach the client.
    assert "upstream died" not in body


def test_generated_ids_have_the_documented_shape():
    """The id format is stable and unique, so it is safe to quote in a bug report."""
    assert re.fullmatch(r"req_[0-9a-f]{16}", new_request_id())
    assert new_request_id() != new_request_id()


# --------------------------------------------------------------------------
# Input limits
# --------------------------------------------------------------------------
@pytest.mark.parametrize("size", [1, 1_000, 100_000])
async def test_prompts_up_to_the_documented_cap_are_accepted(size):
    """Three sizes up to the cap, so the limit does not fire early on legitimate input."""
    async with build_client(MockProvider(chunks=["ok"])) as client:
        response = await client.post("/v1/generate", json={"prompt": "x" * size})
    assert response.status_code == 200


async def test_prompt_over_the_cap_is_rejected_rather_than_truncated():
    """Over the cap is a refusal, never a silent truncation of the user's prompt."""
    async with build_client(MockProvider(chunks=["ok"])) as client:
        response = await client.post("/v1/generate", json={"prompt": "x" * 100_001})
    assert response.status_code == 422
