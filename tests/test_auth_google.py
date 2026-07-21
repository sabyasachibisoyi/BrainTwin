"""Tests for backend/auth/google.py — Phase 4.1 M.M.1.d.

id_token verification is mostly delegated to google-auth (which we
trust), so most tests monkeypatch its `verify_oauth2_token` to
return controlled dicts and confirm our OWN checks fire correctly:

  - happy path returns VerifiedIdentity
  - `email_verified=false` → InvalidIdToken (impersonation defense)
  - missing `sub` claim → InvalidIdToken
  - missing `email` claim → InvalidIdToken
  - google-auth's ValueError → InvalidIdToken (signature failure,
    audience mismatch, expiry — collapsed to one exception class)
  - unset GOOGLE_OAUTH_CLIENT_ID → RuntimeError (config error, NOT
    per-request; caller maps to 503)

We do NOT test google-auth's own logic (JWKS fetch, RS256 verify,
issuer/audience checks). Those are their responsibility; a wrong-
version pin or major-version upgrade breaking behaviour would surface
in local smoke, not unit tests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from google.auth import exceptions as g_exceptions  # noqa: E402

from backend.auth.google import (  # noqa: E402
    IdTokenUnavailable,
    InvalidIdToken,
    VerifiedIdentity,
    verify_google_id_token,
)
from backend.config import settings  # noqa: E402


@pytest.fixture
def _client_id(monkeypatch):
    """Set a plausible client_id so the config check passes."""
    monkeypatch.setattr(
        settings,
        "google_oauth_client_id",
        "1234567890-abc.apps.googleusercontent.com",
    )
    yield "1234567890-abc.apps.googleusercontent.com"


def _patch_verify(claims):
    """Convenience: replace google-auth's verify_oauth2_token with a
    stub that returns the given claims dict. Used as a context manager
    inside each test so patches are scoped, not global."""
    return patch(
        "backend.auth.google.g_id_token.verify_oauth2_token",
        return_value=claims,
    )


# ---- Happy path -----------------------------------------------------

def test_happy_path_returns_verified_identity(_client_id):
    claims = {
        "sub": "117240938409128374012",
        "email": "friend@example.com",
        "email_verified": True,
        "aud": _client_id,
        "iss": "https://accounts.google.com",
        "exp": 9999999999,
        "iat": 1000000000,
    }
    with _patch_verify(claims):
        result = verify_google_id_token("dummy-id-token-string")
    assert isinstance(result, VerifiedIdentity)
    assert result.sub == "117240938409128374012"
    assert result.email == "friend@example.com"


# ---- Config error ---------------------------------------------------

def test_unset_client_id_raises_runtimeerror(monkeypatch):
    """Unset GOOGLE_OAUTH_CLIENT_ID is a CONFIG bug — RuntimeError,
    not InvalidIdToken. Caller (the callback route) maps this to 503
    fail-closed, matching bearer.py's contract."""
    monkeypatch.setattr(settings, "google_oauth_client_id", "")
    with pytest.raises(RuntimeError, match="GOOGLE_OAUTH_CLIENT_ID"):
        verify_google_id_token("any-token")


# ---- email_verified enforcement (Codex Fix 4) -----------------------

def test_email_not_verified_raises_invalididtoken(_client_id):
    """The impersonation defense. An attacker registers a Google
    account claiming `friend@example.com` but never clicks the
    verification link — Google issues a valid id_token with
    email_verified=false. Without this check, we'd sign the attacker
    in as the real friend."""
    claims = {
        "sub": "attacker-google-sub",
        "email": "friend@example.com",
        "email_verified": False,  # <-- the trap
        "aud": _client_id,
        "iss": "https://accounts.google.com",
        "exp": 9999999999,
    }
    with _patch_verify(claims):
        with pytest.raises(InvalidIdToken, match="not verified"):
            verify_google_id_token("token")


def test_missing_email_verified_treated_as_false(_client_id):
    """A future Google API change dropping `email_verified` from the
    claims must NOT default-open. `claims.get('email_verified')`
    returns None, which is falsy → we reject. Regression lock."""
    claims = {
        "sub": "some-sub",
        "email": "friend@example.com",
        # email_verified deliberately absent
        "aud": _client_id,
    }
    with _patch_verify(claims):
        with pytest.raises(InvalidIdToken, match="not verified"):
            verify_google_id_token("token")


# ---- Missing-claim guards -------------------------------------------

def test_missing_sub_raises_invalididtoken(_client_id):
    claims = {
        # sub deliberately absent
        "email": "friend@example.com",
        "email_verified": True,
        "aud": _client_id,
    }
    with _patch_verify(claims):
        with pytest.raises(InvalidIdToken, match="missing required claim"):
            verify_google_id_token("token")


def test_missing_email_raises_invalididtoken(_client_id):
    claims = {
        "sub": "some-sub",
        # email deliberately absent
        "email_verified": True,
        "aud": _client_id,
    }
    with _patch_verify(claims):
        with pytest.raises(InvalidIdToken, match="missing required claim"):
            verify_google_id_token("token")


# ---- google-auth failure translation --------------------------------

def test_google_auth_valueerror_becomes_invalididtoken(_client_id):
    """google-auth raises ValueError for every verification failure
    (bad sig, wrong audience, expired, non-Google issuer). We collapse
    those into one InvalidIdToken since the HTTP response is a single
    401 either way. If a future google-auth version raises a different
    exception type, this test surfaces it."""
    with patch(
        "backend.auth.google.g_id_token.verify_oauth2_token",
        side_effect=ValueError("Wrong recipient, payload audience != required audience"),
    ):
        with pytest.raises(InvalidIdToken, match="google verification failed"):
            verify_google_id_token("token")


def test_transport_error_becomes_id_token_unavailable(_client_id):
    """A JWKS-fetch network failure (Google's certs endpoint unreachable)
    raises google-auth's TransportError, which is NOT a ValueError and NOT
    a bad token. It must map to IdTokenUnavailable (→ 503 retryable), not
    InvalidIdToken (→ 401 'your credential is bad')."""
    with patch(
        "backend.auth.google.g_id_token.verify_oauth2_token",
        side_effect=g_exceptions.TransportError("certs endpoint unreachable"),
    ):
        with pytest.raises(IdTokenUnavailable, match="could not reach google"):
            verify_google_id_token("token")
