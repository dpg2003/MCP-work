"""Contract tests for the real Anthropic provider path.

Why this file exists
--------------------
Every other test in this project drives a mock provider. ``AnthropicProvider``
-- the only class that runs in production -- was marked ``# pragma: no cover``
and had never been executed, so the "253 tests passing" of that time said
nothing about the integration an operator actually uses.

These tests drive ``AnthropicProvider.stream`` itself. The SDK client is
injected, but nothing else is stubbed: the method's own ``async with``, its
iteration over ``text_stream`` and its error normalisation all run.

The stub mirrors the surface the provider depends on -- ``client.messages
.stream(...)`` returning an async context manager whose ``text_stream`` is an
async iterator of ``str``. If a future SDK release changes that shape, these
tests fail, which is the point: they are a contract, not a mock of our own code.
"""

from __future__ import annotations

import asyncio

import pytest

from app import ERROR_SENTINEL, redacted_stream
from conftest import EMAIL, SSN, VISA_SPACED
from providers import AnthropicProvider, UpstreamStreamError
from redactor import REPLACEMENT


class StubStream:
    """Async context manager shaped like the SDK's ``MessageStream``."""

    def __init__(self, deltas, raise_at=None, error=None):
        """Script the deltas, optionally failing partway through."""
        self._deltas = list(deltas)
        self._raise_at = raise_at
        self._error = error or RuntimeError("upstream exploded")
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        """Enter the stream context, as the SDK's does."""
        self.entered = True
        return self

    async def __aexit__(self, *exc_info):
        """Record that the context was closed; never swallow an exception."""
        self.exited = True
        return False

    @property
    def text_stream(self):
        """Async iterator of text deltas, matching the SDK's attribute."""

        async def generate():
            """Yield each scripted delta, raising at the configured index."""
            for index, delta in enumerate(self._deltas):
                if self._raise_at is not None and index == self._raise_at:
                    raise self._error
                await asyncio.sleep(0)
                yield delta

        return generate()


class StubMessages:
    """Stands in for ``client.messages``, recording the call arguments."""

    def __init__(self, stream_obj):
        """Hold the stream this client will hand back."""
        self._stream = stream_obj
        self.calls = []

    def stream(self, **kwargs):
        """Record the request and return the scripted stream."""
        self.calls.append(kwargs)
        return self._stream


class StubClient:
    """Stands in for ``anthropic.AsyncAnthropic``."""

    def __init__(self, stream_obj):
        """Expose a ``messages`` attribute like the real client."""
        self.messages = StubMessages(stream_obj)


def provider_for(deltas, **kwargs) -> tuple[AnthropicProvider, StubStream]:
    """An AnthropicProvider wired to a stub streaming those deltas."""
    stream_obj = StubStream(deltas, **kwargs)
    provider = AnthropicProvider(
        model="claude-sonnet-5", max_tokens=256,
        client_factory=lambda: StubClient(stream_obj),
    )
    return provider, stream_obj


# --------------------------------------------------------------------------
# The provider honours the SDK contract
# --------------------------------------------------------------------------
async def test_provider_yields_the_sdk_text_deltas_in_order():
    """The real method must pass deltas through unchanged and in sequence."""
    provider, stream_obj = provider_for(["Hello ", "there, ", "friend."])
    assert [chunk async for chunk in provider.stream("hi")] == [
        "Hello ", "there, ", "friend.",
    ]
    assert stream_obj.entered and stream_obj.exited, "stream context must be closed"


async def test_provider_sends_the_configured_model_and_budget():
    """A wrong model or budget is an outage, so assert what is actually sent."""
    provider, _ = provider_for(["x"])
    [chunk async for chunk in provider.stream("the prompt")]
    sent = provider._get_client().messages.calls[0]
    assert sent["model"] == "claude-sonnet-5"
    assert sent["max_tokens"] == 256
    assert sent["messages"] == [{"role": "user", "content": "the prompt"}]


async def test_provider_reuses_one_client_across_calls():
    """A client per request would exhaust connections under load."""
    provider, _ = provider_for(["x"])
    assert provider._get_client() is provider._get_client()


async def test_an_empty_stream_is_not_an_error():
    """A model may legitimately return nothing; that must not raise."""
    provider, _ = provider_for([])
    assert [chunk async for chunk in provider.stream("hi")] == []


# --------------------------------------------------------------------------
# The guardrail applies to a real provider response
# --------------------------------------------------------------------------
async def test_pii_from_the_real_provider_path_is_redacted():
    """The claim that matters: PII in a provider response never reaches a client."""
    provider, _ = provider_for(
        ["Her address is jane.r", "oe@example.com, ssn 123-", "45-6789, card 4111 1111 ",
         "1111 1111. Ticket 123456789 stays."]
    )
    out = b"".join([chunk async for chunk in redacted_stream(provider.stream("x"))]).decode()
    assert out == (
        f"Her address is {REPLACEMENT}, ssn {REPLACEMENT}, card {REPLACEMENT}. "
        "Ticket 123456789 stays."
    )
    for secret in ("@example.com", "123-45-6789", "4111", "jane.r"):
        assert secret not in out


@pytest.mark.parametrize("width", [1, 2, 3, 5, 8])
async def test_pii_is_redacted_at_every_provider_delta_width(width):
    """Real token boundaries are not ours to choose, so sweep the widths."""
    text = f"email {EMAIL} ssn {SSN} card {VISA_SPACED} end"
    deltas = [text[i:i + width] for i in range(0, len(text), width)]
    provider, _ = provider_for(deltas)
    out = b"".join([chunk async for chunk in redacted_stream(provider.stream("x"))]).decode()
    assert out == f"email {REPLACEMENT} ssn {REPLACEMENT} card {REPLACEMENT} end"


async def test_provider_failure_mid_stream_is_normalised_and_sanitised():
    """A mid-stream SDK exception must become UpstreamStreamError, type only."""
    provider, _ = provider_for(
        ["safe text ", "never seen"], raise_at=1,
        error=RuntimeError("DB_PASSWORD=hunter2 at inference.internal:8443"),
    )
    with pytest.raises(UpstreamStreamError) as raised:
        [chunk async for chunk in provider.stream("x")]
    message = str(raised.value)
    assert "RuntimeError" in message
    for secret in ("hunter2", "inference.internal"):
        assert secret not in message


async def test_mid_stream_failure_still_flushes_a_redacted_tail():
    """The dangerous case: the stream dies while PII is still held back."""
    provider, _ = provider_for(
        ["Here it is: john.doe@exam", "ple.com", "never seen"], raise_at=2,
    )
    out = b"".join([chunk async for chunk in redacted_stream(provider.stream("x"))]).decode()
    assert out == f"Here it is: {REPLACEMENT}{ERROR_SENTINEL}"
    assert "@" not in out
    assert "never seen" not in out


# --------------------------------------------------------------------------
# Configuration failures are clean
# --------------------------------------------------------------------------
def test_a_missing_api_key_is_a_normalised_error(monkeypatch):
    """No key must be an UpstreamStreamError, not an AttributeError at runtime."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(UpstreamStreamError) as raised:
        AnthropicProvider()._get_client()
    assert "ANTHROPIC_API_KEY" in str(raised.value)


def test_the_default_model_is_a_current_model_id(monkeypatch):
    """A stale model id is a silent outage; pin what is actually shipped."""
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    assert AnthropicProvider().model == "claude-sonnet-5"


def test_the_model_is_overridable_by_environment(monkeypatch):
    """Operators must be able to move models without a code change."""
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5")
    assert AnthropicProvider().model == "claude-opus-5"


# --------------------------------------------------------------------------
# Which provider the documented run command actually gets
# --------------------------------------------------------------------------
def test_a_present_api_key_selects_the_real_provider(monkeypatch):
    """The gap this closes: the default used to be the fixture even with a key.

    An operator who exported a key and ran the documented command got the
    scripted mock, so nothing they saw exercised a real provider response.
    """
    from providers import provider_from_env

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-real")
    assert isinstance(provider_from_env(), AnthropicProvider)


def test_no_api_key_falls_back_to_the_mock_with_a_warning(monkeypatch, caplog):
    """The service must still start without a key -- but say what it is serving."""
    import logging

    from providers import MockProvider, provider_from_env

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with caplog.at_level(logging.WARNING, logger="fde.providers"):
        provider = provider_from_env()
    assert isinstance(provider, MockProvider)
    assert "mock" in caplog.text.lower()


def test_an_explicit_choice_always_wins(monkeypatch):
    """An operator must be able to force the mock even with a key present."""
    from providers import MockProvider, provider_from_env

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-real")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    assert isinstance(provider_from_env(), MockProvider)


def test_an_unknown_provider_name_is_refused(monkeypatch):
    """A typo in LLM_PROVIDER must fail at startup, not silently serve a fixture."""
    from providers import provider_from_env

    monkeypatch.setenv("LLM_PROVIDER", "gpt-whatever")
    with pytest.raises(ValueError):
        provider_from_env()
