"""Token estimation for admission control.

Why an estimate at all
----------------------
Admission control has to charge *something* before the request is made -- that
is what makes it admission control -- but the true cost is unknown until the
provider answers. So the gateway reserves an estimate and reconciles the
reservation afterwards against the provider's reported usage.

The estimator is pluggable
--------------------------
``HeuristicEstimator`` is the default and needs nothing installed. Where exact
accounting matters more than latency, supply a different :class:`TokenEstimator`
-- a real tokenizer, or a provider's own counting endpoint -- without touching
the limiter or the gateway.

An exact tokenizer is deliberately *not* the default. Anthropic's token counting
is a network call, so making it the admission path would put a round trip in
front of every request purely to decide whether to allow it, and would fail
closed when that endpoint is slow. A fast approximation that is then reconciled
against real usage is the better trade for a gateway.

Why the heuristic looks like this
---------------------------------
``ceil(len(prompt) / 4)`` is the standard rule of thumb for English BPE
tokenizers. Two adjustments make it less wrong on the inputs a gateway actually
sees:

* **Non-Latin text costs more tokens per character.** CJK and emoji commonly
  land at roughly one token per character, so characters outside the Latin-1
  range are counted at a heavier rate instead of being lumped in at 4:1.
* **Output is reserved, not guessed.** ``max_tokens`` is added in full. Charging
  only for the prompt would let a tenant issue tiny prompts with huge output
  budgets and blow through its quota before a single reconciliation landed.

The estimate deliberately errs high. Over-charging is corrected within
milliseconds by reconciliation; under-charging is an unmetered request.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

# Latin text: the classic ~4 characters per token.
LATIN_CHARS_PER_TOKEN = 4
# Characters outside Latin-1 (CJK, emoji, most non-European scripts) tokenize
# far more densely -- roughly one token each, sometimes more.
WIDE_CHARS_PER_TOKEN = 1


@dataclass(frozen=True)
class TokenEstimate:
    """A split estimate, so the two halves can be reported separately."""

    prompt_tokens: int
    reserved_output_tokens: int

    @property
    def total(self) -> int:
        """What admission control charges for the request."""
        return self.prompt_tokens + self.reserved_output_tokens


class TokenEstimator(Protocol):
    """Anything that can price a request before it is made."""

    name: str

    def estimate(self, prompt: str, max_tokens: int) -> TokenEstimate:
        """Return the estimated cost of ``prompt`` plus the reserved output."""
        ...


class HeuristicEstimator:
    """Character-class-weighted approximation. No dependencies, no network."""

    name = "heuristic-v2"

    def estimate(self, prompt: str, max_tokens: int) -> TokenEstimate:
        """Price a prompt by character class, and reserve the output budget.

        Args:
            prompt: The prompt text.
            max_tokens: The caller's output budget, reserved in full.

        Returns:
            The split estimate; ``total`` is never below 1, so a request can
            always be distinguished from no request at all.

        Raises:
            ValueError: ``max_tokens`` is negative.
        """
        if max_tokens < 0:
            raise ValueError("max_tokens must be non-negative")

        wide = sum(1 for character in prompt if ord(character) > 0xFF)
        latin = len(prompt) - wide
        prompt_tokens = math.ceil(latin / LATIN_CHARS_PER_TOKEN) + math.ceil(
            wide / WIDE_CHARS_PER_TOKEN
        )
        return TokenEstimate(
            prompt_tokens=max(1, prompt_tokens) if prompt else 0,
            reserved_output_tokens=max_tokens,
        )


DEFAULT_ESTIMATOR: TokenEstimator = HeuristicEstimator()


def estimate_tokens(prompt: str, max_tokens: int,
                    estimator: TokenEstimator | None = None) -> int:
    """Total tokens to reserve for one request. Always at least 1.

    Kept as a module-level function so existing callers are unaffected by the
    estimator becoming pluggable.
    """
    chosen = estimator or DEFAULT_ESTIMATOR
    return max(1, chosen.estimate(prompt, max_tokens).total)
