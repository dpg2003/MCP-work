"""In-memory mock datastore.

Deliberately tiny and dependency-free: the point of the assessment is the
protocol/validation layer, not persistence. State is per-process, so a fresh
subprocess (as the tests spawn) always starts from the same fixtures.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class CustomerNotFoundError(LookupError):
    """Raised when a well-formed customer id has no record behind it."""

    def __init__(self, customer_id: str) -> None:
        super().__init__(f"No customer record for {customer_id}")
        self.customer_id = customer_id


@dataclass
class Customer:
    customer_id: str
    name: str
    email: str
    plan: str
    lifetime_value_usd: float
    signed_up_at: str
    open_tickets: int
    refunds: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "customer_id": self.customer_id,
            "name": self.name,
            "email": self.email,
            "plan": self.plan,
            "lifetime_value_usd": self.lifetime_value_usd,
            "signed_up_at": self.signed_up_at,
            "open_tickets": self.open_tickets,
            "refund_count": len(self.refunds),
        }


_SEED: list[Customer] = [
    Customer("CUST-A1B2C", "Ada Lovelace", "ada@example.com", "enterprise", 48250.00, "2021-03-14T09:00:00Z", 1),
    Customer("CUST-99999", "Grace Hopper", "grace@example.com", "pro", 12300.50, "2022-07-01T13:30:00Z", 0),
    Customer("CUST-zz001", "Seymour Cray", "seymour@example.com", "starter", 480.00, "2024-01-09T17:45:00Z", 3),
    Customer("CUST-00042", "Radia Perlman", "radia@example.com", "pro", 9900.99, "2023-11-22T08:15:00Z", 0),
]


class Datastore:
    """Thread-safe mock store. A lock keeps refund appends atomic."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._customers: dict[str, Customer] = {}
        self._refund_ids = itertools.count(1)
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._customers = {
                c.customer_id: Customer(**{**c.__dict__, "refunds": []}) for c in _SEED
            }
            self._refund_ids = itertools.count(1)

    def get_customer(self, customer_id: str) -> dict[str, Any]:
        with self._lock:
            customer = self._customers.get(customer_id)
            if customer is None:
                raise CustomerNotFoundError(customer_id)
            return customer.to_dict()

    def record_refund(self, customer_id: str, amount: float, reason: str) -> dict[str, Any]:
        with self._lock:
            customer = self._customers.get(customer_id)
            if customer is None:
                raise CustomerNotFoundError(customer_id)
            refund = {
                "refund_id": f"RF-{next(self._refund_ids):06d}",
                "customer_id": customer_id,
                "amount": round(amount, 2),
                "reason": reason,
                "status": "accepted",
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            customer.refunds.append(refund)
            return dict(refund)


DATASTORE = Datastore()
