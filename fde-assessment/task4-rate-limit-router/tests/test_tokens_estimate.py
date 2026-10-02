"""Token estimation behaviour.

Admission control charges an estimate, so the estimate's shape is a product
decision, not an implementation detail: too low and requests go unmetered, too
high and tenants are refused work they could afford.
"""

from __future__ import annotations

import pytest

from tokens_estimate import (
    DEFAULT_ESTIMATOR,
    HeuristicEstimator,
    TokenEstimate,
    estimate_tokens,
)


def test_the_output_budget_is_reserved_in_full():
    """Otherwise tiny prompts with huge budgets slip past the limiter."""
    estimate = DEFAULT_ESTIMATOR.estimate("hi", 1_000)
    assert estimate.reserved_output_tokens == 1_000
    assert estimate.total > 1_000


def test_latin_text_uses_the_four_characters_per_token_rule():
    """The documented heuristic for English BPE, pinned so a change is deliberate."""
    assert DEFAULT_ESTIMATOR.estimate("a" * 40, 0).prompt_tokens == 10


def test_non_latin_text_is_charged_more_per_character():
    """CJK and emoji tokenize roughly one-to-one, so 4:1 under-charges badly."""
    latin = DEFAULT_ESTIMATOR.estimate("a" * 40, 0).prompt_tokens
    cjk = DEFAULT_ESTIMATOR.estimate("東" * 40, 0).prompt_tokens
    assert cjk > latin * 3, f"CJK charged {cjk} against Latin {latin}"


def test_mixed_scripts_are_charged_per_character_class():
    """Real prompts mix scripts, so each class is priced separately rather than averaged."""
    mixed = DEFAULT_ESTIMATOR.estimate("a" * 40 + "東" * 40, 0).prompt_tokens
    assert mixed == 10 + 40


def test_an_empty_prompt_costs_nothing_but_the_request_still_counts():
    """A request must never be free, or it is unmetered."""
    assert DEFAULT_ESTIMATOR.estimate("", 0).prompt_tokens == 0
    assert estimate_tokens("", 0) == 1


def test_the_estimate_errs_high_rather_than_low():
    """Over-charging is corrected by reconciliation; under-charging is a leak."""
    prompt = "The quick brown fox jumps over the lazy dog. " * 10
    assert estimate_tokens(prompt, 100) > 100


def test_a_negative_output_budget_is_refused():
    """A negative budget would credit tokens back through the admission path."""
    with pytest.raises(ValueError):
        DEFAULT_ESTIMATOR.estimate("hi", -1)


def test_the_estimator_is_pluggable():
    """So an exact tokenizer can be swapped in without touching the limiter."""

    class Exact:
        """A stand-in for a real tokenizer."""

        name = "exact"

        def estimate(self, prompt: str, max_tokens: int) -> TokenEstimate:
            """Charge one token per character."""
            return TokenEstimate(prompt_tokens=len(prompt),
                                 reserved_output_tokens=max_tokens)

    assert estimate_tokens("hello", 10, estimator=Exact()) == 15


def test_the_default_estimator_is_named_for_observability():
    """So a usage report can say which scheme produced a figure."""
    assert HeuristicEstimator().name == "heuristic-v2"
