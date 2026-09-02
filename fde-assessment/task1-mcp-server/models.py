"""Strict Pydantic input models for every tool exposed by the MCP server.

Design notes (see README for the full rationale):

* ``model_config`` uses ``strict=True`` so JSON type coercion never happens.
  ``"12.50"`` is *not* silently turned into ``12.5``; a bool is *not* an int.
* ``extra="forbid"`` so unexpected keys are a validation failure rather than
  being silently dropped (fail closed).
* ``allow_inf_nan=False`` so ``NaN`` / ``Infinity`` (which Python's ``json``
  module happily parses) are rejected instead of poisoning downstream math.
* Every bound is an explicit *rejection*, never a truncation.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ``CUST-`` prefix + exactly 5 alphanumeric characters.
#
# The prefix is matched CASE-SENSITIVELY on purpose: ``cust-abcde`` is rejected.
# Customer IDs are opaque identifiers used as datastore keys, and accepting
# multiple spellings of the same key is how you end up with two records for one
# customer. The 5 payload characters allow mixed case (``CUST-a1B2c``) because
# the generator that mints them is base62.
CUSTOMER_ID_PATTERN = r"^CUST-[A-Za-z0-9]{5}$"

# Upper bounds exist so that "extremely large input" is a clean -32602 rejection
# rather than an unbounded allocation or a silent truncation further down.
MAX_REFUND_AMOUNT = 1_000_000.0
MAX_REASON_LENGTH = 2_000
MIN_REASON_LENGTH = 10


CustomerId = Annotated[
    str,
    Field(
        pattern=CUSTOMER_ID_PATTERN,
        description="Customer identifier in CUST-XXXXX form (5 alphanumerics).",
    ),
]


class StrictModel(BaseModel):
    """Base class carrying the strict-validation configuration."""

    model_config = ConfigDict(strict=True, extra="forbid")


class GetCustomerRecordInput(StrictModel):
    """Input schema for ``get_customer_record``."""

    customer_id: CustomerId


class TriggerRefundInput(StrictModel):
    """Input schema for ``trigger_refund``."""

    customer_id: CustomerId
    amount: Annotated[
        float,
        Field(
            gt=0,
            le=MAX_REFUND_AMOUNT,
            allow_inf_nan=False,
            description="Refund amount in USD; strictly positive and finite.",
        ),
    ]
    reason: Annotated[
        str,
        Field(
            min_length=MIN_REASON_LENGTH,
            max_length=MAX_REASON_LENGTH,
            description="Human-readable justification, at least 10 non-blank characters.",
        ),
    ]

    @field_validator("reason")
    @classmethod
    def _reason_must_have_substance(cls, value: str) -> str:
        """Reject whitespace-only reasons.

        Documented decision: a reason of ``"          "`` (10 spaces) is
        *invalid*. The minimum length exists to force an auditable
        justification, and whitespace carries none, so the length check is
        applied to the trimmed string. The trimmed value is what gets stored.
        """
        trimmed = value.strip()
        if len(trimmed) < MIN_REASON_LENGTH:
            raise ValueError(
                f"reason must contain at least {MIN_REASON_LENGTH} non-whitespace-padded "
                f"characters (got {len(trimmed)} after trimming)"
            )
        return trimmed


def json_schema_for(model: type[BaseModel]) -> dict:
    """JSON Schema advertised in ``tools/list`` for a tool's input model."""
    return model.model_json_schema()
