"""Proof that the pattern-narrowing optimisation is semantics-preserving.

``find_matches`` picks a narrowed regex based on cheap content probes (see
``_pattern_for``). That is a performance optimisation on a security control, so
it is held to a higher bar than "the other tests still pass": every case here
compares it against the *unoptimised* full alternation and requires byte-identical
spans.

If someone later adds a pattern and forgets to update the gate, these fail.
"""

from __future__ import annotations

import random
import re
import string

import pytest

from conftest import AMEX, EMAIL, MASTERCARD, SSN, VISA, VISA_SPACED
from redactor import PATTERN, REPLACEMENT, find_matches, redact_complete


def reference_matches(text: str) -> list[tuple[int, int, str]]:
    """Spans found by the full alternation with no narrowing — the oracle."""
    out = []
    for match in PATTERN.finditer(text):
        if match.groupdict().get("card") is not None:
            digits = re.sub(r"[^0-9]", "", match.group())
            if not (13 <= len(digits) <= 19 and _luhn(digits)):
                continue
        out.append((match.start(), match.end(), match.group()))
    return out


def _luhn(digits: str) -> bool:
    """Independent Luhn implementation, so the oracle shares no code path."""
    total, parity = 0, len(digits) % 2
    for index, character in enumerate(digits):
        value = int(character)
        if index % 2 == parity:
            value = value * 2 - 9 if value * 2 > 9 else value * 2
        total += value
    return total % 10 == 0


def optimised_matches(text: str) -> list[tuple[int, int, str]]:
    """Spans found through the production, narrowed path."""
    return [(m.start(), m.end(), m.group()) for m in find_matches(text)]


def assert_equivalent(text: str) -> None:
    """The narrowed path and the oracle must agree exactly."""
    assert optimised_matches(text) == reference_matches(text), text


# --------------------------------------------------------------------------
# The specific inputs that select each narrowed variant
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "",                                  # neither gate
        "no pii here at all",                # neither gate: pattern is None
        "just letters and punctuation!?.",   # neither gate
        "1234567890",                        # digits only
        "a@b.cc",                            # at-sign, no digit
        "user@example.com",                  # at-sign, no digit
        "user1@example.com",                 # both gates
        "@",                                 # at-sign alone
        "0",                                 # single digit
        "@ 1",                                # both, matching nothing
    ],
)
def test_each_gate_branch_agrees_with_the_oracle(text):
    """One input per branch of the narrowing gate, so no branch is left unexercised."""
    assert_equivalent(text)


def test_email_without_any_digit_is_still_found():
    """The digit-free branch must not lose emails."""
    text = "write to alice@example.com please"
    assert optimised_matches(text) == reference_matches(text)
    assert redact_complete(text) == f"write to {REPLACEMENT} please"


def test_numeric_pii_in_text_with_no_at_sign_is_still_found():
    """The no-at-sign branch must not lose SSNs or cards."""
    text = f"ssn {SSN} card {VISA_SPACED}"
    assert optimised_matches(text) == reference_matches(text)
    assert redact_complete(text) == f"ssn {REPLACEMENT} card {REPLACEMENT}"


def test_digit_run_no_longer_takes_the_quadratic_email_path():
    """The regression this optimisation exists for: a long digit run."""
    text = "9" * 4000
    assert optimised_matches(text) == reference_matches(text)


# --------------------------------------------------------------------------
# Randomised differential testing
# --------------------------------------------------------------------------
ALPHABET = string.ascii_letters + string.digits + " .-_@%+:/\t\n" + "é🚀"

SECRETS = [EMAIL, SSN, VISA, VISA_SPACED, MASTERCARD, AMEX,
           "a@b.cc", "x.y+z%1@sub.domain.co.uk", "987 65 4321", "4111-1111-1111-1111"]

NEAR_MISSES = ["555-123-4567", "192.168.1.1", "v1.2.3", "123456789", "user@localhost",
               "1234567812345678", "2024-01-15", "12-34-5678", "example.com", "@jane"]


def test_random_strings_agree_with_the_oracle():
    """5000 random strings over a PII-adjacent alphabet."""
    rng = random.Random(11223344)
    for _ in range(5000):
        length = rng.randint(0, 120)
        assert_equivalent("".join(rng.choice(ALPHABET) for _ in range(length)))


def test_random_compositions_of_secrets_and_near_misses_agree():
    """5000 strings assembled from real PII, near-misses, and filler."""
    rng = random.Random(55667788)
    fillers = ["", " ", "  ", ".", ", ", "\n", "x", "0", "-", "@", "é", "🚀"]
    for _ in range(5000):
        parts = []
        for _ in range(rng.randint(1, 6)):
            parts.append(rng.choice(SECRETS + NEAR_MISSES))
            parts.append(rng.choice(fillers))
        assert_equivalent("".join(parts))


def test_random_digit_runs_agree():
    """Digit runs of every length around the 13-19 card window."""
    rng = random.Random(99001122)
    for _ in range(2000):
        run = "".join(rng.choice(string.digits) for _ in range(rng.randint(1, 40)))
        separator = rng.choice(["", " ", "-", "  ", " x "])
        assert_equivalent(f"{run}{separator}{run}")


def test_adversarial_at_sign_placements_agree():
    """'@' scattered through digit runs -- the case both gates are live for."""
    rng = random.Random(31415926)
    for _ in range(2000):
        chars = [rng.choice("0123456789@.- ") for _ in range(rng.randint(1, 60))]
        assert_equivalent("".join(chars))


@pytest.mark.parametrize("length", [1, 12, 13, 14, 15, 16, 17, 18, 19, 20, 25, 40])
def test_every_card_length_boundary_agrees(length):
    """Around the 13-19 digit window, in both bare and separated forms."""
    for digits in ("4" * length, "4111111111111111"[:length].ljust(length, "1")):
        for text in (digits, f" {digits} ", "-".join([digits[i:i+4] for i in range(0, len(digits), 4)])):
            assert_equivalent(text)
