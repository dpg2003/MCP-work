"""Compact HMAC-signed bearer tokens.

Format (JWT-shaped, deliberately not JWT):

    base64url(json_payload) "." base64url(hmac_sha256(secret, base64url_payload))

Why not real OAuth / JWT?

* The gateway is the only issuer *and* the only verifier, so there is no
  third party that needs to validate a signature with a public key. A
  symmetric HMAC is the right primitive for that topology and needs no key
  distribution, no JWKS endpoint, and no dependency beyond ``hmac``.
* Real JWT brings ``alg`` negotiation with it, and ``alg: none`` /
  algorithm-confusion is the single most common JWT vulnerability. This format
  has exactly one algorithm and no header field to lie in, so that class of
  attack does not exist here.
* Swapping in RS256 JWTs later means replacing ``verify()`` only; everything
  downstream of it consumes a ``Principal``.

Verification is fail-closed at every step: bad base64, bad JSON, a payload that
is not an object, a missing/invalid ``exp``, a signature mismatch, or an
unrecognised role all raise ``InvalidToken``.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import time
from dataclasses import dataclass
from hashlib import sha256

# Roles are matched case-sensitively and exactly. Anything not in this set --
# "superadmin", "Admin", null, absent -- is rejected rather than mapped to a
# default, so a typo in an issuer can never silently mint a privileged token.
ADMIN_ROLE = "admin"
VIEWER_ROLE = "viewer"
KNOWN_ROLES = frozenset({ADMIN_ROLE, VIEWER_ROLE})

DEFAULT_TTL_SECONDS = 3600
_DEV_SECRET = "dev-only-insecure-secret-change-me"


class InvalidToken(Exception):
    """Raised for any token that cannot be trusted, for any reason."""


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str
    expires_at: int

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN_ROLE


def get_secret() -> bytes:
    """Signing secret, from ``GATEWAY_TOKEN_SECRET`` or an obvious dev default."""
    return os.environ.get("GATEWAY_TOKEN_SECRET", _DEV_SECRET).encode("utf-8")


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload_segment: str, secret: bytes) -> str:
    digest = hmac.new(secret, payload_segment.encode("ascii"), sha256).digest()
    return _b64encode(digest)


def issue(subject: str, role: str, ttl_seconds: int = DEFAULT_TTL_SECONDS,
          secret: bytes | None = None, issued_at: int | None = None) -> str:
    """Mint a token. Used by the test suite and the ``mint-token`` CLI."""
    if role not in KNOWN_ROLES:
        raise ValueError(f"refusing to issue a token for unknown role {role!r}")
    now = int(time.time()) if issued_at is None else issued_at
    payload = {"sub": subject, "role": role, "iat": now, "exp": now + ttl_seconds}
    segment = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{segment}.{_sign(segment, secret or get_secret())}"


def verify(token: str, secret: bytes | None = None, now: float | None = None) -> Principal:
    """Verify a token and return its ``Principal``, or raise ``InvalidToken``."""
    if not isinstance(token, str) or not token:
        raise InvalidToken("empty token")

    parts = token.split(".")
    if len(parts) != 2:
        raise InvalidToken("malformed token structure")
    payload_segment, signature = parts

    expected = _sign(payload_segment, secret or get_secret())
    # Constant-time: a byte-by-byte compare leaks the signature one byte at a time.
    if not hmac.compare_digest(expected, signature):
        raise InvalidToken("signature mismatch")

    try:
        claims = json.loads(_b64decode(payload_segment))
    except Exception as exc:  # base64 or JSON failure
        raise InvalidToken("undecodable payload") from exc
    if not isinstance(claims, dict):
        raise InvalidToken("payload is not an object")

    expires_at = claims.get("exp")
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        raise InvalidToken("missing or non-integer exp claim")
    if (time.time() if now is None else now) >= expires_at:
        raise InvalidToken("token expired")

    role = claims.get("role")
    if role not in KNOWN_ROLES:
        # Fail closed: an unknown, null or absent role is not "probably a viewer".
        raise InvalidToken(f"unrecognised role claim: {role!r}")

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise InvalidToken("missing sub claim")

    return Principal(subject=subject, role=role, expires_at=expires_at)


def parse_authorization_header(header: str | None) -> Principal:
    """Extract and verify a principal from an ``Authorization`` header value."""
    if header is None:
        raise InvalidToken("missing Authorization header")
    parts = header.split(" ", 1)
    if len(parts) != 2:
        raise InvalidToken("malformed Authorization header")
    scheme, credentials = parts
    # RFC 7235 says the auth scheme is case-insensitive; the token is not.
    if scheme.lower() != "bearer":
        raise InvalidToken("unsupported authorization scheme")
    credentials = credentials.strip()
    if not credentials:
        raise InvalidToken("empty bearer credentials")
    return verify(credentials)


if __name__ == "__main__":  # pragma: no cover - developer convenience
    import sys

    role = sys.argv[1] if len(sys.argv) > 1 else VIEWER_ROLE
    subject = sys.argv[2] if len(sys.argv) > 2 else f"{role}@example.com"
    print(issue(subject, role))
