"""Performance *properties*, asserted without depending on absolute timings.

Throughput numbers belong in ``bench/``; a CI box's noise is not a regression.
What is asserted here is algorithmic behaviour that must hold on any machine:
work scales linearly with input, and memory does not scale with stream length.
Each of these fails on the code as it was before the optimisation.
"""

from __future__ import annotations

import time

import pytest

from conftest import EMAIL, SSN, VISA_SPACED
from redactor import (
    PATTERN,
    StreamRedactor,
    _pattern_for,
    find_matches,
    redact_complete,
)


def time_scan(text: str, repeats: int = 5) -> float:
    """Best-of-N seconds to scan ``text`` once. Best-of resists scheduler noise."""
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        list(find_matches(text))
        best = min(best, time.perf_counter() - start)
    return best


# --------------------------------------------------------------------------
# Scanning must scale linearly, not quadratically
# --------------------------------------------------------------------------
@pytest.mark.parametrize("filler", ["9", "a", "9a", ". "])
def test_scan_cost_scales_linearly_with_input_size(filler):
    """Quadratic scanning is the bug this guards; 8x input must not cost ~64x.

    The old email pattern consumed a whole digit run at every start position
    looking for an "@", which put this ratio well above 20 for digit fillers.
    """
    small = (filler * 4_000)[:4_000]
    large = (filler * 32_000)[:32_000]

    ratio = time_scan(large) / max(time_scan(small), 1e-9)
    assert ratio < 20, f"8x the input cost {ratio:.1f}x the time -- superlinear"


def test_a_long_digit_run_does_not_take_the_email_path():
    """The specific input that was quadratic: digits with no '@' anywhere."""
    text = "7" * 50_000
    start = time.perf_counter()
    assert redact_complete(text) == text
    elapsed = time.perf_counter() - start
    # Generously above any reasonable machine, far below the old behaviour.
    assert elapsed < 2.0, f"50k digits took {elapsed:.2f}s"


def test_the_narrowing_gate_selects_the_cheap_pattern():
    """Directly assert the mechanism, not just its timing."""
    assert _pattern_for("1234567890") is not PATTERN          # no '@' -> numeric only
    assert _pattern_for("plain prose here") is None           # neither -> no scan at all
    assert _pattern_for("a@b.cc") is not PATTERN              # no digit -> email only
    assert _pattern_for("a@b.cc 123") is PATTERN              # both -> full alternation


def test_text_with_no_pii_characters_skips_scanning_entirely():
    """Prose with neither an at-sign nor a digit selects no pattern at all, which is the cheapest path."""
    text = "the quick brown fox jumps over the lazy dog" * 100
    assert _pattern_for(text) is None
    assert list(find_matches(text)) == []


# --------------------------------------------------------------------------
# Memory must not scale with stream length
# --------------------------------------------------------------------------
def test_buffer_never_exceeds_the_documented_bound():
    """The invariant the memory guarantee rests on."""
    redactor = StreamRedactor()
    chunk = f"prose {EMAIL} more {SSN} more {VISA_SPACED} and a run of 9999999999999999999999 "
    for _ in range(5_000):
        redactor.feed(chunk)
        assert redactor.buffered_chars <= redactor.max_hold + len(chunk)
    redactor.close()
    assert redactor.peak_buffer_chars <= redactor.max_hold + len(chunk)
    assert redactor.buffered_chars == 0


def test_buffer_is_bounded_under_a_pathological_unbroken_token():
    """A single 'token' longer than the whole window must not grow the buffer."""
    redactor = StreamRedactor()
    for _ in range(2_000):
        redactor.feed("a" * 500)          # one unbroken run, 1 MB total
    redactor.close()
    assert redactor.peak_buffer_chars <= redactor.max_hold + 500


def test_throughput_does_not_collapse_on_numeric_content():
    """A coarse floor: numeric text must stay within an order of magnitude of prose.

    Before the fix, numeric content ran roughly 10x slower than prose. The
    threshold is deliberately loose so this measures the algorithm, not the box.
    """
    prose = "the quick brown fox jumps over the lazy dog. " * 200
    digits = "9" * len(prose)
    ratio = time_scan(digits) / max(time_scan(prose), 1e-9)
    assert ratio < 8, f"numeric content is {ratio:.1f}x slower than prose"
