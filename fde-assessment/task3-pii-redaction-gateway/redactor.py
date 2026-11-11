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
     bound. SSNs (11 characters) and cards (at most 23) always fit inside the
     held window. Emails do not have to: ``EMAIL`` has no length cap, so an
     address longer than ``max_hold`` streams out with its leading characters
     unredacted -- a 300-character local part (312-character address)
     streamed one character at a time leaked its first 54 characters when
     measured. Only the split-width guarantee below (tokens up to
     ``max_hold``) holds.

3. Redact every match that ends at or before ``emit_limit`` (matching is done
   against the *whole* buffer, so ``\\b`` and neighbouring context are correct),
   emit ``buffer[:emit_limit]`` with those substitutions, and keep the rest.

On close, ``emit_limit`` becomes ``len(buffer)``: the tail goes through the same
redaction and is flushed, never dropped. A tail that never completed a pattern
is not PII by these patterns and is flushed as-is -- a stream that stops at
``"Her card is 4111 1111 1111"`` or ``"SSN 123-45-67"`` emits that fragment
unchanged. That includes an upstream failure mid-token (see ``redact_stream``).

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

# Maximum hold-back, and therefore the documented split-width guarantee. It
# covers every SSN and card, and every email up to 256 characters; EMAIL itself
# has no length cap, so a longer address is not guaranteed (see the module
# docstring).
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

# Card: 13-19 digits in a real PAN layout, then a Luhn check (see _passes_luhn).
# Either unseparated, or grouped 4-4-4-4-x (17-19 digits), 4-4-4-x (13-19) or
# 4-6-x (Amex/Diners), each separator a single space or hyphen. Separators may
# differ between groups ("4111 1111-1111 1111"): only the group sizes matter.
# Fixing the group sizes is what stops a card absorbing a neighbouring expiry,
# CVV or quantity ("4111 1111 1111 1111 12/28"): the combined digits fail Luhn,
# and the real card inside them would be lost (see find_matches). Dots are not
# accepted as separators, so "192.168.1.1" and version strings cannot match.
# 8-8 ("41111111 11111111") is not a PAN layout and is deliberately not
# matched: it would also catch pairs of YYYYMMDD dates.
CARD = (
    r"\b(?:"
    r"\d{13,19}"
    r"|\d{4}[ -]\d{4}[ -]\d{4}[ -](?:\d{4}[ -]\d{1,3}|\d{1,7})"
    r"|\d{4}[ -]\d{6}[ -]\d{4,5}"
    r")\b"
)

# The 16-digit reading of a 4-4-4-4-x candidate: "4111 1111 1111 1111 123" is a
# 19-digit card or a 16-digit card plus its CVV, and only Luhn can tell which.
# The longer reading is tried first, so a mistake over-redacts a CVV rather than
# leaking the tail of a 19-digit card; this is the fallback when it fails.
_CARD_FIRST_FOUR_GROUPS = re.compile(
    r"(?P<card>\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4})(?=[ -]\d)"
)

# The full alternation, and two cheaper specialisations of it.
#
# PERFORMANCE: the email local-part class ``[A-Za-z0-9._%+\-]`` includes
# digits, so on a long run of digits the engine consumes the whole run at every
# start position, then backtracks one character at a time looking for an "@"
# that is not there. That is quadratic, and it dominated profiles of
# numeric-heavy streams (147us per scan of a 326-character digit buffer, versus
# 3us for the card pattern on the same input).
#
# The fix is a content gate rather than a rewritten regex: an email cannot
# match text with no "@" in it, and neither SSNs nor cards can match text with
# no ASCII digit in it, so ``_pattern_for`` picks the narrowest variant and the
# quadratic path is simply never entered on numeric text.
#
# LIMITATION: the gate is exact for ASCII digits only. ``_HAS_DIGIT`` probes
# for ``[0-9]``, but SSN and CARD use ``\d``, which also matches non-ASCII
# digits (full-width "１２３-４５-６７８９", Arabic-Indic, ...). Text whose only
# digits are non-ASCII therefore skips the numeric scan that PATTERN would have
# run, and such an SSN or card is redacted only if an ASCII digit happens to
# share the buffer. The equivalence tests use ASCII digits only.
PATTERN = re.compile(
    rf"(?P<email>{EMAIL})|(?P<ssn>{SSN})|(?P<card>{CARD})",
)
_PATTERN_EMAIL_ONLY = re.compile(rf"(?P<email>{EMAIL})")
_PATTERN_NUMERIC_ONLY = re.compile(rf"(?P<ssn>{SSN})|(?P<card>{CARD})")

# Cheap content probes. ``str.find`` and a single-character scan are linear with
# a very small constant, unlike the alternation they guard.
_HAS_DIGIT = re.compile(r"[0-9]").search
_NON_DIGITS = re.compile(r"[^0-9]").sub


def _pattern_for(text: str) -> re.Pattern[str] | None:
    """Narrowest pattern that can still match ``text``, or ``None`` for no match.

    An optimisation: for text whose digits are ASCII, every branch returns a
    pattern whose result on ``text`` is identical to :data:`PATTERN`'s,
    because the alternatives it omits cannot match text that lacks their
    mandatory characters. Not for non-ASCII digits: the probe is ``[0-9]``
    while the patterns use ``\\d``, so text whose only digits are non-ASCII
    gets no numeric scan here although :data:`PATTERN` would find an SSN or
    card in it.
    """
    has_at = "@" in text
    has_digit = _HAS_DIGIT(text) is not None
    if has_at:
        return PATTERN if has_digit else _PATTERN_EMAIL_ONLY
    return _PATTERN_NUMERIC_ONLY if has_digit else None

# Suffixes that could still grow into a match. Anchored to the end of the
# buffer; the earliest such start is what gets held back.
#
# Each alternative is the "any non-empty prefix of" version of a pattern above.
# PERFORMANCE: every quantifier here is *possessive* (``++``, ``*+``, ``{m,n}+``).
# The pattern is anchored to the end with ``\Z``, so a greedy quantifier that
# overshoots backtracks one character at a time and fails again at every length
# -- quadratic per start position, and this runs once per chunk. Possessive
# quantifiers forbid that backtracking.
#
# It is safe *because* of the ``\Z`` anchor: the alternatives are built so the
# maximal consumption is the only one that can reach the end of the string, so
# no match is lost. Verified against the backtracking form over 25,000+
# randomised inputs in tests/test_partial_equivalence.py. Measured: 38.6us ->
# 17.1us per search on a representative buffer.
PARTIAL = re.compile(
    r"(?:"
    r"[A-Za-z0-9._%+\-]++(?:@[A-Za-z0-9.\-]*+)?"    # email: local part, maybe @domain-so-far
    r"|\d{1,3}+(?:[- ]\d{0,2}+(?:[- ]\d{0,4}+)?)?"  # ssn: 3-2-4 built up
    r"|\d(?:[ -]?\d)*+[ -]?+"                       # card: digit run, maybe trailing separator
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
    """Post-filter for matches whose regex alone is not specific enough.

    Uses ``groupdict()`` rather than ``group("card")`` because the pattern that
    produced the match may be one of the narrowed variants, which does not
    always define a ``card`` group.
    """
    if match.groupdict().get("card") is not None:
        digits = _NON_DIGITS("", match.group())
        return 13 <= len(digits) <= 19 and _passes_luhn(digits)
    return True


def find_matches(text: str) -> Iterable[re.Match[str]]:
    """Yield the matches that survive post-filtering, in order, non-overlapping.

    A card candidate that fails Luhn is never allowed to consume its span,
    because a real card may overlap it. The 16-digit reading at the same start
    is tried first (a card followed by its CVV); failing that, scanning resumes
    one character after the candidate's *start*, so a card that begins inside
    it (``"1234 4111 1111 1111 1111"``) is still found. ``finditer`` cannot do
    this: it resumes at the end of the regex match, before Luhn has run.

    For text whose digits are ASCII, results are identical to running the same
    scan with :data:`PATTERN`; see :func:`_pattern_for` for why narrowing is
    safe there, and for the non-ASCII-digit case where it is not.
    """
    pattern = _pattern_for(text)
    if pattern is None:
        return
    position = 0
    while (match := pattern.search(text, position)) is not None:
        if _is_real_match(match):
            yield match
            position = match.end()
            continue
        # Only card candidates are ever rejected.
        shorter = _CARD_FIRST_FOUR_GROUPS.match(text, match.start())
        if shorter is not None and shorter.end() < match.end() and _is_real_match(shorter):
            yield shorter
            position = shorter.end()
        else:
            position = match.start() + 1


def _partial_prefix_start(text: str, max_hold: int = DEFAULT_MAX_HOLD) -> int:
    """Index where the trailing "could still become PII" fragment begins.

    ``len(text)`` when the buffer ends in something that cannot start any
    pattern -- the common case for prose, and why TTFT stays low.

    The search starts at ``len(text) - max_hold`` rather than at 0. This does
    not change the result: :meth:`StreamRedactor._settled_limit` already floors
    the answer at that same index, so a partial beginning earlier would be
    clamped to it anyway. Skipping those start positions is pure saved work on
    a long buffer.
    """
    match = PARTIAL.search(text, max(0, len(text) - max_hold))
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
        """Create a redactor.

        Args:
            max_hold: Maximum characters ever held back, and therefore the
                guaranteed split-width: any PII token up to this length is
                caught however it is divided across chunks.

        Raises:
            ValueError: ``max_hold`` is too small to contain the longest token
                the patterns can produce, which would silently weaken the
                guarantee.
        """
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
        """Characters currently held back. Bounded by ``max_hold`` plus one chunk."""
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
        """Flush the tail. Anything held back is redacted, then emitted.

        "Redacted" means matched against the full patterns: a held fragment
        that never completed one (``"4111 1111 1111"``, ``"123-45-67"``) is
        emitted unchanged.
        """
        if self._closed:
            return ""
        self._closed = True
        return self._drain(len(self._buffer))

    # -- internals ----------------------------------------------------------
    def _settled_limit(self) -> int:
        """Index up to which the buffer can no longer be changed by future input.

        The smaller of two bounds: the start of the trailing fragment that
        could still grow into a match, and a hard floor of
        ``len(buffer) - max_hold`` that caps the hold-back. The floor is safe
        for every SSN and card and for emails up to ``max_hold`` characters;
        an email longer than that has its leading characters emitted before
        it completes (``EMAIL`` has no length cap).
        """
        buffer = self._buffer
        # Floor: never hold more than max_hold characters. A token that fits
        # in max_hold always lies entirely inside the held window; only an
        # email longer than that (EMAIL has no length cap) can be cut by it.
        limit = max(0, len(buffer) - self.max_hold)
        limit = max(limit, _partial_prefix_start(buffer, self.max_hold))
        return min(limit, len(buffer))

    def _drain(self, limit: int) -> str:
        """Redact and emit ``buffer[:limit]``, retaining the remainder.

        Matching runs against the whole buffer so ``\\b`` and neighbouring
        context are evaluated correctly, but only matches that end at or
        before ``limit`` are substituted. A match still open at the buffer end
        pulls ``limit`` back to its start, so its prefix is never emitted raw.
        """
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
