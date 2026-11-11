"""Unit tests for the signed-token scheme itself."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

import tokens
from conftest import MISSING_OR_SHORT_SECRETS, OLD_PUBLIC_DEFAULT_SECRET, PROJECT_ROOT, set_secret

SECRET = b"unit-test-secret"


def test_round_trip():
    """A minted token verifies back to the same subject and role."""
    principal = tokens.verify(tokens.issue("a@b.c", "admin", secret=SECRET), secret=SECRET)
    assert principal.subject == "a@b.c"
    assert principal.role == "admin"
    assert principal.is_admin


def test_viewer_is_not_admin():
    """The privilege check is a property of the verified principal, not of the caller's request."""
    principal = tokens.verify(tokens.issue("a@b.c", "viewer", secret=SECRET), secret=SECRET)
    assert not principal.is_admin


def test_refuses_to_issue_an_unknown_role():
    """Issuance is fail-closed too: a typo in a role name cannot mint a token the verifier will later have to judge."""
    with pytest.raises(ValueError):
        tokens.issue("a@b.c", "superadmin", secret=SECRET)


def test_expiry_boundary_is_exclusive():
    """A token is valid up to but not at its exp, so the boundary cannot be argued either way."""
    token = tokens.issue("a@b.c", "admin", ttl_seconds=100, secret=SECRET, issued_at=1_000)
    assert tokens.verify(token, secret=SECRET, now=1_099).role == "admin"
    with pytest.raises(tokens.InvalidToken):
        tokens.verify(token, secret=SECRET, now=1_100)


@pytest.mark.parametrize(
    "token",
    ["", "abc", "a.b.c", "....", "!!!.???", "eyJ9.x", ".", "a."],
)
def test_structurally_broken_tokens_raise(token):
    """Eight malformed shapes that must all raise rather than partially parse."""
    with pytest.raises(tokens.InvalidToken):
        tokens.verify(token, secret=SECRET)


def test_wrong_secret_raises():
    """A correctly-shaped token signed with another key is rejected, which is the whole point of the signature."""
    token = tokens.issue("a@b.c", "admin", secret=SECRET)
    with pytest.raises(tokens.InvalidToken):
        tokens.verify(token, secret=b"other")


def test_header_parsing_requires_bearer_scheme():
    """A wrong scheme and an absent header are both rejected before any signature work happens."""
    with pytest.raises(tokens.InvalidToken):
        tokens.parse_authorization_header("Basic abc")
    with pytest.raises(tokens.InvalidToken):
        tokens.parse_authorization_header(None)


# --------------------------------------------------------------------------
# The signing secret: required, at least MIN_SECRET_BYTES, no default
# --------------------------------------------------------------------------
@MISSING_OR_SHORT_SECRETS
def test_missing_or_short_secret_is_refused(monkeypatch, secret_value):
    """Unset, empty and 31-byte secrets all raise, and the error never echoes the value."""
    set_secret(monkeypatch, secret_value)
    with pytest.raises(tokens.SecretNotConfigured) as excinfo:
        tokens.get_secret()
    message = str(excinfo.value)
    assert "GATEWAY_TOKEN_SECRET" in message
    assert str(tokens.MIN_SECRET_BYTES) in message
    if secret_value:
        assert secret_value not in message


@MISSING_OR_SHORT_SECRETS
def test_issue_and_verify_refuse_to_fall_back_to_a_default(monkeypatch, secret_value):
    """With no usable secret configured, neither signing nor verification picks a key of its own."""
    set_secret(monkeypatch, secret_value)
    with pytest.raises(tokens.SecretNotConfigured):
        tokens.issue("a@b.c", "admin")
    with pytest.raises(tokens.SecretNotConfigured):
        tokens.verify("abc.def")


def test_secret_of_exactly_32_bytes_is_accepted(monkeypatch):
    """The minimum is inclusive and counted in UTF-8 bytes, not characters."""
    monkeypatch.setenv("GATEWAY_TOKEN_SECRET", "q" * 32)
    assert tokens.get_secret() == b"q" * 32
    principal = tokens.verify(tokens.issue("a@b.c", "admin"))
    assert principal.is_admin

    monkeypatch.setenv("GATEWAY_TOKEN_SECRET", "é" * 16)  # 16 characters, 32 bytes
    assert tokens.get_secret() == ("é" * 16).encode("utf-8")


def test_token_signed_with_the_old_public_default_is_rejected(monkeypatch):
    """The value the code used to fall back to is public, so a token signed with it must not verify.

    Checked both with a real secret configured (rejected as a forgery) and with
    none configured (no key at all, rather than the old default).
    """
    forged = tokens.issue("attacker", "admin", ttl_seconds=10**9, secret=OLD_PUBLIC_DEFAULT_SECRET)
    with pytest.raises(tokens.InvalidToken):
        tokens.parse_authorization_header(f"Bearer {forged}")

    monkeypatch.delenv("GATEWAY_TOKEN_SECRET")
    with pytest.raises(tokens.SecretNotConfigured):
        tokens.parse_authorization_header(f"Bearer {forged}")


def _run_cli(secret_value: str | None, *args: str) -> subprocess.CompletedProcess:
    """Run ``python tokens.py *args`` with ``GATEWAY_TOKEN_SECRET`` set to ``secret_value`` (None: unset)."""
    env = {k: v for k, v in os.environ.items() if k != "GATEWAY_TOKEN_SECRET"}
    if secret_value is not None:
        env["GATEWAY_TOKEN_SECRET"] = secret_value
    return subprocess.run(
        [sys.executable, "tokens.py", *args], cwd=str(PROJECT_ROOT), env=env,
        capture_output=True, text=True, timeout=30,
    )


@MISSING_OR_SHORT_SECRETS
def test_cli_refuses_to_mint_without_a_valid_secret(secret_value):
    """The mint-token CLI exits with status 1, explains why on stderr, and prints no token."""
    result = _run_cli(secret_value, "admin")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "GATEWAY_TOKEN_SECRET" in result.stderr
    assert str(tokens.MIN_SECRET_BYTES) in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_mints_a_verifiable_token_with_a_valid_secret():
    """The positive control for the refusal test: same invocation, valid secret, a working token."""
    secret = "q" * 32
    result = _run_cli(secret, "admin")
    assert result.returncode == 0, result.stderr
    principal = tokens.verify(result.stdout.strip(), secret=secret.encode())
    assert principal.is_admin
