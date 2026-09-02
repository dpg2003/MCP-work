"""Unit tests for the signed-token scheme itself."""

from __future__ import annotations

import pytest

import tokens

SECRET = b"unit-test-secret"


def test_round_trip():
    principal = tokens.verify(tokens.issue("a@b.c", "admin", secret=SECRET), secret=SECRET)
    assert principal.subject == "a@b.c"
    assert principal.role == "admin"
    assert principal.is_admin


def test_viewer_is_not_admin():
    principal = tokens.verify(tokens.issue("a@b.c", "viewer", secret=SECRET), secret=SECRET)
    assert not principal.is_admin


def test_refuses_to_issue_an_unknown_role():
    with pytest.raises(ValueError):
        tokens.issue("a@b.c", "superadmin", secret=SECRET)


def test_expiry_boundary_is_exclusive():
    token = tokens.issue("a@b.c", "admin", ttl_seconds=100, secret=SECRET, issued_at=1_000)
    assert tokens.verify(token, secret=SECRET, now=1_099).role == "admin"
    with pytest.raises(tokens.InvalidToken):
        tokens.verify(token, secret=SECRET, now=1_100)


@pytest.mark.parametrize(
    "token",
    ["", "abc", "a.b.c", "....", "!!!.???", "eyJ9.x", ".", "a."],
)
def test_structurally_broken_tokens_raise(token):
    with pytest.raises(tokens.InvalidToken):
        tokens.verify(token, secret=SECRET)


def test_wrong_secret_raises():
    token = tokens.issue("a@b.c", "admin", secret=SECRET)
    with pytest.raises(tokens.InvalidToken):
        tokens.verify(token, secret=b"other")


def test_header_parsing_requires_bearer_scheme():
    with pytest.raises(tokens.InvalidToken):
        tokens.parse_authorization_header("Basic abc")
    with pytest.raises(tokens.InvalidToken):
        tokens.parse_authorization_header(None)
