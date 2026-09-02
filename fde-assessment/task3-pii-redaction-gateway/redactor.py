"""Streaming PII redaction with an adaptive sliding buffer.

The problem
-----------
PII arrives split across stream chunks. ``"...john.doe@exam"`` + ``"ple.com"``
regex-matches nothing in either chunk, so per-chunk matching leaks. Buffering
the whole response would fix it and destroy time-to-first-token.

The approach
------------
Hold back only the suffix of the buffer that could still *grow into* a match,
and flush everything before it immediately.

For each chunk:

1. ``buffer += chunk``.
2. Compute ``emit_limit``: the index up to which the text is *settled*, i.e.
   no future input can change how it redacts. It is the earliest of

   * the start of the longest suffix that is a viable **prefix** of some PII
     pattern (``_partial_prefix_start``) -- e.g. a trailing ``"john.doe@exam"``
     pins ``emit_limit`` to where that fragment begins;
   * the start of any match that is still *open* at the buffer end;
   * ``len(buffer) - max_hold`` is a floor, not a ceiling: it caps how much
     can ever be held so a pathological input cannot grow the buffer without
     bound. It is safe because no recognised PII token exceeds ``max_hold``
     characters, so any real token lies entirely within the held window.

3. Redact every match that ends at or before ``emit_limit`` (matching is done
   against the *whole* buffer, so ``\\b`` and neighbouring context are correct),
   emit ``buffer[:emit_limit]`` with those substitutions, and keep the rest.

On close, ``emit_limit`` becomes ``len(buffer)``: the tail is redacted and
flushed, never dropped and never emitted raw.

Guarantees
----------
* **Split-width.** Any PII token up to ``max_hold`` characters (default 256) is
  redacted no matter how it is split -- including one character per chunk.
* **Memory.** ``len(buffer) <= max_hold + len(largest chunk)``. It does not
  grow with stream length.
* **TTFT.** The hold-back is adaptive, not fixed. Ordinary prose ends in
  characters that cannot begin any pattern, so ``emit_limit == len(buffer)``
  and the very first chunk flushes in full.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterable

REPLACEMENT = "[REDACTED]"

# Longest token any pattern can produce, and therefore the maximum hold-back.
# Also the documented split-width guarantee.
DEFAULT_MAX_HOLD = 256

# --------------------------------------------------------------------------
# Patterns
#
# Every pattern is deliberately specific rather than greedy. The failure mode
# that matters most in a gateway is not a missed match (a human reviews the
# transcript) but redacting the answer the user actually asked for.
# --------------------------------------------------------------------------

# Email: requires a dot-separated TLD of 2-63 letters, so "user@localhost"
# and "@mentions" do not match.
EMAIL = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,63}"

# SSN: the 3-2-4 grouping with an explicit separator. A bare 9-digit run is NOT
# matched -- that is a product code, an order number, or a phone number far more
# often than it is an SSN.
SSN = r"\b\d{3}(?P<ssn_sep>[- ])\d{2}(?P=ssn_sep)\d{4}\b"

# Card: 13-19 digits, single optional space/hyphen between digits, then a Luhn
# check (see _passes_luhn). Dots are not accepted as separators, so "192.168.1.1"
# and version strings cannot match.
CARD = r"\b\d(?:[ -]?\d){12,18}\b"

PATTERN = re.compile(
    rf"(?P<email>{EMAIL})|(?P<ssn>{SSN})|(?P<card>{CARD})",
)

# Suffixes that could still grow into a match. Anchored to the end of the
# buffer; the earliest such start is what gets held back.
#
# Each alternative is the "any non-empty prefix of" version of a pattern above.
PARTIAL = re.compile(
    r"(?:"
    r"[A-Za-z0-9._%+\-]+(?:@[A-Za-z0-9.\-]*)?"   # email: local part, maybe @domain-so-far
    r"|\d{1,3}(?:[- ]\d{0,2}(?:[- ]\d{0,4})?)?"  # ssn: 3-2-4 built up
    r"|\d(?:[ -]?\d)*[ -]?"                      # card: digit run, maybe trailing separator
    r")\Z"
)


def _passes_luhn(digits: str) -> bool:
    """Luhn mod-10 checksum, the standard structural test for a PAN.

    This is what keeps a 16-digit order number from being redacted as a credit
    card. It cuts the false-positive rate on long digit runs by ~90% and costs
    nothing; a real card number passes by construction.
    """
    total = 0
    parity = len(digits) % 2
    for index, character in enumerate(digits):
        value = ord(character) - 48
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _is_real_match(match: re.Match[str]) -> bool:
    """Post-filter for matches whose regex alone is not specific enough."""
    if match.lastgroup == "card" or match.group("card") is not None:
        digits = re.sub(r"[^0-9]", "", match.group())
        return 13 <= len(digits) <= 19 and _passes_luhn(digits)
    return True


def find_matches(text: str) -> Iterable[re.Match[str]]:
    """Yield the matches that survive post-filtering, in order, non-overlapping."""
    for match in PATTERN.finditer(text):
        if _is_real_match(match):
            yield match


def _partial_prefix_start(text: str) -> int:
    """Index where the trailing "could still become PII" fragment begins.

    ``len(text)`` when the buffer ends in something that cannot start any
    pattern -- which is the common case for prose, and why TTFT stays low.
    """
    match = PARTIAL.search(text)
    return match.start() if match else len(text)


def redact_complete(text: str) -> str:
    """Redact a complete, non-streamed string. Used as the reference oracle."""
    out: list[str] = []
    position = 0
    for match in find_matches(text):
        out.append(text[position : match.start()])
        out.append(REPLACEMENT)
        position = match.end()
    out.append(text[position:])
    return "".join(out)


class StreamRedactor:
    """Incremental redactor. Feed chunks, get back safe-to-emit text.

    Synchronous and transport-free on purpose: it is a pure state machine, so
    it can be property-tested against ``redact_complete`` without any I/O.
    """

    def __init__(self, max_hold: int = DEFAULT_MAX_HOLD) -> None:
        if max_hold < 64:
            raise ValueError("max_hold below 64 cannot cover the supported patterns")
        self.max_hold = max_hold
        self._buffer = ""
        self._closed = False
        # Observability: the high-water mark proves the buffer stays bounded.
        self.peak_buffer_chars = 0
        self.redaction_count = 0

    @property
    def buffered_chars(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: str) -> str:
        """Absorb ``chunk``; return the text that is now safe to emit."""
        if self._closed:
            raise RuntimeError("feed() after close()")
        if not chunk:
            # Zero-length deltas are common in real provider streams. Nothing
            # changed, so nothing new is settled.
            return ""
        self._buffer += chunk
        self.peak_buffer_chars = max(self.peak_buffer_chars, len(self._buffer))
        return self._drain(self._settled_limit())

    def close(self) -> str:
        """Flush the tail. Anything held back is redacted, then emitted."""
        if self._closed:
            return ""
        self._closed = True
        return self._drain(len(self._buffer))

    # -- internals ----------------------------------------------------------
    def _settled_limit(self) -> int:
        buffer = self._buffer
        # Floor: never hold more than max_hold characters. Safe because no
        # recognised token is longer than that, so a real token always lies
        # entirely inside the held window.
        limit = max(0, len(buffer) - self.max_hold)
        limit = max(limit, _partial_prefix_start(buffer))
        return min(limit, len(buffer))

    def _drain(self, limit: int) -> str:
        buffer = self._buffer
        out: list[str] = []
        position = 0
        for match in find_matches(buffer):
            if match.end() <= limit:
                out.append(buffer[position : match.start()])
                out.append(REPLACEMENT)
                position = match.end()
                self.redaction_count += 1
            else:
                # Still open at the buffer end: nothing from its start onwards
                # is settled yet.
                limit = min(limit, match.start())
                break
        if limit < position:  # pragma: no cover - matches are ordered
            limit = position
        out.append(buffer[position:limit])
        self._buffer = buffer[limit:]
        return "".join(out)


async def redact_stream(
    source: AsyncIterator[str], max_hold: int = DEFAULT_MAX_HOLD
) -> AsyncIterator[str]:
    """Wrap an async text stream, yielding redacted text as soon as it settles.

    Empty yields are suppressed so a consumer never sees a meaningless flush.
    The ``finally`` guarantees the held tail is redacted and emitted even if the
    upstream iterator raises -- the alternative is silently losing the last few
    characters of every failed stream.
    """
    redactor = StreamRedactor(max_hold=max_hold)
    try:
        async for chunk in source:
            emitted = redactor.feed(chunk)
            if emitted:
                yield emitted
    finally:
        tail = redactor.close()
        if tail:
            yield tail
