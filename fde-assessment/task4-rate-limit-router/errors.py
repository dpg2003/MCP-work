"""One error shape for everything the gateway returns.

A client integrating against this gateway should never have to parse two
formats, and should never receive an upstream provider's error body, exception
string, internal hostname, or stack trace. Every failure path in the gateway
goes through ``GatewayError`` and comes out as:

    {
      "error": {
        "type":       "rate_limit_exceeded",     # stable machine-readable enum
        "message":    "Token rate limit exceeded",   # fixed, safe prose
        "request_id": "req_9f2c...",             # correlates with the logs
        "details":    {"limit_tokens": 50000}    # only gateway-owned facts
      }
    }

``details`` is populated exclusively from values the gateway itself computed.
Upstream detail is logged against ``request_id`` and goes no further.
"""

from __future__ import annotations

import uuid
from typing import Any

# Stable error types. Clients switch on these; the prose may be reworded.
INVALID_REQUEST = "invalid_request"
UNAUTHENTICATED = "unauthenticated"
RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
UPSTREAM_UNAVAILABLE = "upstream_unavailable"
INTERNAL_ERROR = "internal_error"

_HTTP_STATUS = {
    INVALID_REQUEST: 400,
    UNAUTHENTICATED: 401,
    RATE_LIMIT_EXCEEDED: 429,
    UPSTREAM_UNAVAILABLE: 502,
    INTERNAL_ERROR: 500,
}

_MESSAGES = {
    INVALID_REQUEST: "Invalid request",
    UNAUTHENTICATED: "Missing or unknown API key",
    RATE_LIMIT_EXCEEDED: "Token rate limit exceeded",
    UPSTREAM_UNAVAILABLE: "No model provider was able to serve this request",
    INTERNAL_ERROR: "Internal gateway error",
}


def new_request_id() -> str:
    """Mint a correlation id.

    Returned to the client and written to the logs, so a user can quote an id
    from an error and an operator can find the full internal detail behind it
    without any of that detail having been sent over the wire.
    """
    return f"req_{uuid.uuid4().hex[:16]}"


class GatewayError(Exception):
    """The only error type that reaches a client."""

    def __init__(
        self,
        error_type: str,
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
        internal_detail: str | None = None,
    ) -> None:
        """Create a gateway error.

        Args:
            error_type: One of the stable module-level type constants.
            details: Gateway-owned facts safe to serialise. Never populate
                this from an upstream response.
            request_id: Correlation id; generated when omitted.
            internal_detail: Diagnostics for the log only. Deliberately not
                part of :meth:`to_payload`.
        """
        self.error_type = error_type
        self.details = details or {}
        self.request_id = request_id or new_request_id()
        # Logged, never serialized.
        self.internal_detail = internal_detail
        super().__init__(_MESSAGES.get(error_type, _MESSAGES[INTERNAL_ERROR]))

    @property
    def http_status(self) -> int:
        """HTTP status for this error type, defaulting to 500."""
        return _HTTP_STATUS.get(self.error_type, 500)

    def to_payload(self) -> dict[str, Any]:
        """Serialise to the single client-facing error shape.

        ``internal_detail`` is intentionally absent: it is the field that would
        leak an upstream body or stack trace, so it has no path to the wire.
        """
        return {
            "error": {
                "type": self.error_type,
                "message": _MESSAGES.get(self.error_type, _MESSAGES[INTERNAL_ERROR]),
                "request_id": self.request_id,
                "details": self.details,
            }
        }
