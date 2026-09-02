"""Token estimation for admission control.

The limiter has to charge *something* before the request is made — that is the
entire point of admission control — but the true token count is not known until
the provider answers. So the gateway charges an estimate up front and
reconciles the reservation afterwards with the provider's reported usage.

The estimate is ``ceil(len(prompt) / CHARS_PER_TOKEN) + max_tokens``:

* ``len(prompt) / 4`` is the standard rule of thumb for English BPE
  tokenizers. It is an approximation, but it is a *fast* one — no tokenizer to
  load, no per-provider vocabulary to keep in sync, and no chance of the
  admission path becoming the slowest thing in the request.
* Adding ``max_tokens`` reserves the output budget. Charging only for the
  prompt would let a tenant issue a stream of tiny prompts with huge
  ``max_tokens`` and blow straight through the budget before a single
  reconciliation lands.
* The estimate deliberately errs high. Over-charging is corrected within
  milliseconds by ``reconcile()``; under-charging is an unmetered request.
"""

from __future__ import annotations

import math

CHARS_PER_TOKEN = 4


def estimate_tokens(prompt: str, max_tokens: int) -> int:
    """Upper-ish bound on what this request will cost. Always >= 1."""
    prompt_tokens = math.ceil(len(prompt) / CHARS_PER_TOKEN)
    return max(1, prompt_tokens + max(0, max_tokens))
