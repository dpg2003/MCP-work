"""Proof that the possessive-quantifier ``PARTIAL`` regex is equivalent.

``PARTIAL`` decides how much text is held back, so an error here either leaks
PII (holding back too little) or truncates output (holding back too much). The
possessive rewrite is a performance change to that decision, so it is checked
against the original backtracking form rather than trusted.
"""

from __future__ import annotations

import random
import re
import string

import pytest

from conftest import AMEX, EMAIL, MASTERCARD, SSN, VISA, VISA_SPACED
from redactor import DEFAULT_MAX_HOLD, PARTIAL, _partial_prefix_start

# The original, backtracking form. Kept here as the oracle only.
REFERENCE = re.compile(
    r"(?:"
    r"[A-Za-z0-9._%+\-]+(?:@[A-Za-z0-9.\-]*)?"
    r"|\d{1,3}(?:[- ]\d{0,2}(?:[- ]\d{0,4})?)?"
    r"|\d(?:[ -]?\d)*[ -]?"
    r")\Z"
)


def reference_start(text: str) -> int:
    """What the backtracking pattern would report, searching from index 0."""
    match = REFERENCE.search(text)
    return match.start() if match else len(text)


def settled_limit(text: str, start: int, max_hold: int = DEFAULT_MAX_HOLD) -> int:
    """The value ``StreamRedactor._settled_limit`` derives from a partial start."""
    return min(max(max(0, len(text) - max_hold), start), len(text))


def assert_equivalent(text: str, max_hold: int = DEFAULT_MAX_HOLD) -> None:
    """The bounded possessive search must yield the same settled limit."""
    assert settled_limit(text, _partial_prefix_start(text, max_hold), max_hold) == settled_limit(
        text, reference_start(text), max_hold
    ), repr(text)


# --------------------------------------------------------------------------
# Concrete cases
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "", " ", "a", "9", "@", "-", ".",
        "john.doe@exam", "john.doe@example.com", "a@b@c", "ab@cd",
        "123-45", "123-45-6789", "4111 1111 ", "4111 1111 1111 1111",
        "987 65", "prose with no pii", "trailing space ", "ends with dot.",
        "9" * 40, "a" * 40 + "@", "x@y.", "%%%+++...", "  123  ",
    ],
)
def test_concrete_cases_match_the_backtracking_form(text):
    assert_equivalent(text)


@pytest.mark.parametrize("secret", [EMAIL, SSN, VISA, VISA_SPACED, MASTERCARD, AMEX])
def test_every_prefix_of_every_secret_matches(secret):
    """Every partially-arrived secret is where the hold-back decision matters."""
    for length in range(len(secret) + 1):
        assert_equivalent("some prose " + secret[:length])


# --------------------------------------------------------------------------
# Randomised
# --------------------------------------------------------------------------
ALPHABET = string.ascii_letters + string.digits + " .-_@%+:/\t\n" + "é🚀"


def test_random_strings_match_the_backtracking_form():
    """20,000 random strings over a PII-adjacent alphabet."""
    rng = random.Random(24681357)
    for _ in range(20_000):
        text = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 80)))
        assert_equivalent(text)


def test_random_strings_match_at_small_hold_windows():
    """The window bound must not change the answer at any max_hold."""
    rng = random.Random(13579)
    for _ in range(5_000):
        text = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 300)))
        assert_equivalent(text, max_hold=rng.choice([64, 100, 256, 512]))


def test_long_buffers_beyond_the_window_match():
    """Where the window bound actually bites: buffers longer than max_hold."""
    rng = random.Random(2468)
    for _ in range(500):
        filler = rng.choice(["9", "a", "9a", ". ", "@", "-"])
        text = (filler * 400)[: rng.randint(257, 400)]
        assert_equivalent(text)


def test_the_pattern_really_is_possessive():
    """Guard the optimisation itself: a rewrite must keep the quantifiers."""
    assert "++" in PARTIAL.pattern
    assert "*+" in PARTIAL.pattern
