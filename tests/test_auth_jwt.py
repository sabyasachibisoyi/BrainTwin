"""Tests for backend/auth/jwt.py — Phase 4.1 M.M.1.c.

Pure crypto/parsing tests — no DB. Covers:
  - mint→decode roundtrip preserves all required claims
  - custom TTL is honored
  - RuntimeError on unset JWT_SECRET (config error, not per-request)
  - ExpiredToken when exp is in the past
  - InvalidToken on signature failure (wrong secret at decode)
  - InvalidToken on malformed / unsigned token (alg=none rejection)
  - InvalidToken on missing required claim
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import jwt as pyjwt  # noqa: E402

from backend.auth.jwt import (  # noqa: E402
    ExpiredToken,
    InvalidToken,
    decode_jwt,
    mint_user_jwt,
)
from backend.config import settings  # noqa: E402
from backend.storage.models import User  # noqa: E402


# ---- Fixtures --------------------------------------------------------

@pytest.fixture
def _jwt_secret(monkeypatch):
    """Set a deterministic JWT secret for tests. `reveal()` accepts a
    plain str, so we bypass the SecretStr wrap for test brevity."""
    monkeypatch.setattr(settings, "jwt_secret", "test-secret-for-mm1c-32-chars-min")
    yield "test-secret-for-mm1c-32-chars-min"


def _make_user(**overrides) -> User:
    """Minimal user factory for JWT tests. Fields not overridden get
    sensible defaults from the User dataclass."""
    return User(
        id=42,
        email="jane@example.com",
        display_name="Jane",
        created_at="2026-07-02T00:00:00+00:00",
        token_version=7,
        **overrides,
    )


# ---- mint / decode round-trip ---------------------------------------

def test_mint_decode_roundtrip_preserves_claims(_jwt_secret):
    user = _make_user()
    token = mint_user_jwt(user)
    claims = decode_jwt(token)
    assert claims["sub"] == "42"                # str per RFC 7519
    assert claims["email"] == "jane@example.com"
    assert claims["tv"] == 7
    assert "iat" in claims
    assert "exp" in claims
    assert claims["exp"] > claims["iat"]


def test_mint_honors_custom_ttl(_jwt_secret):
    """A 1-minute TTL should place exp exactly 60 seconds past iat."""
    user = _make_user()
    token = mint_user_jwt(user, ttl_minutes=1)
    claims = decode_jwt(token)
    assert claims["exp"] - claims["iat"] == 60


def test_mint_uses_default_ttl_from_settings(_jwt_secret, monkeypatch):
    monkeypatch.setattr(settings, "jwt_ttl_minutes", 15)
    user = _make_user()
    token = mint_user_jwt(user)
    claims = decode_jwt(token)
    assert claims["exp"] - claims["iat"] == 15 * 60


# ---- Config errors --------------------------------------------------

def test_mint_raises_runtimeerror_when_secret_unset(monkeypatch):
    """Unset secret is a CONFIG bug, not a per-request error — RuntimeError
    so a startup smoke test can catch it, and so it doesn't get eaten
    by an "invalid token" 401 down the request chain."""
    monkeypatch.setattr(settings, "jwt_secret", "")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        mint_user_jwt(_make_user())


def test_decode_raises_runtimeerror_when_secret_unset(monkeypatch, _jwt_secret):
    # Mint with a valid secret first.
    token = mint_user_jwt(_make_user())
    # Then unset the secret and try to decode.
    monkeypatch.setattr(settings, "jwt_secret", "")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        decode_jwt(token)


def test_short_secret_raises_runtimeerror(monkeypatch):
    """HS256 is exactly as strong as the secret's entropy. A short
    secret is offline-crackable from any captured token → arbitrary
    token minting for any user_id. Anything under 32 chars must fail
    loudly at startup, not sign weakly."""
    monkeypatch.setattr(settings, "jwt_secret", "hunter2")
    with pytest.raises(RuntimeError, match="too short"):
        mint_user_jwt(_make_user())


# ---- Expiry ----------------------------------------------------------

def test_expired_token_raises_expiredtoken(_jwt_secret):
    """Mint with a negative TTL so exp is in the past. Sleep a beat to
    ensure the timestamp math actually catches — PyJWT compares against
    wall clock at decode time."""
    user = _make_user()
    token = mint_user_jwt(user, ttl_minutes=-1)
    time.sleep(0.01)
    with pytest.raises(ExpiredToken):
        decode_jwt(token)


# ---- Signature / algorithm attacks ----------------------------------

def test_wrong_secret_raises_invalidtoken(_jwt_secret, monkeypatch):
    """Classic tampering scenario: token minted with our secret, then
    attacker (or config drift) tries to decode with a different secret.
    Signature check fails → InvalidToken."""
    user = _make_user()
    token = mint_user_jwt(user)
    monkeypatch.setattr(settings, "jwt_secret", "different-secret-32-chars-min-ok")
    with pytest.raises(InvalidToken):
        decode_jwt(token)


def test_unsigned_token_rejected(_jwt_secret):
    """The classic JWT-library CVE class: attacker crafts a token with
    `alg: "none"` (unsigned) hoping the decoder accepts it. We pin
    `algorithms=["HS256"]` in `decode_jwt` — this test locks that in."""
    # Manually craft an unsigned token with all our required claims.
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=5)
    payload = {
        "sub": "42",
        "email": "jane@example.com",
        "tv": 7,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    unsigned_token = pyjwt.encode(payload, "", algorithm="none")
    with pytest.raises(InvalidToken):
        decode_jwt(unsigned_token)


def test_malformed_token_raises_invalidtoken(_jwt_secret):
    with pytest.raises(InvalidToken):
        decode_jwt("not-even-close-to-a-jwt")


# ---- Missing-claim guard --------------------------------------------

def test_missing_sub_claim_raises_invalidtoken(_jwt_secret):
    """decode_jwt has a belt-and-braces required-claims check.
    Craft a token missing `sub` (signed correctly so PyJWT doesn't
    reject on signature) and confirm we still raise InvalidToken."""
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=5)
    payload = {
        # sub deliberately absent
        "email": "jane@example.com",
        "tv": 7,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = pyjwt.encode(payload, _jwt_secret, algorithm="HS256")
    with pytest.raises(InvalidToken, match="missing required claim: sub"):
        decode_jwt(token)


def test_missing_tv_claim_raises_invalidtoken(_jwt_secret):
    """Without `tv`, `get_current_user` can't check revocation — that's
    a critical claim. The required-claims guard catches this before
    the request layer sees a token with unknown revocation state."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "42",
        "email": "jane@example.com",
        # tv deliberately absent
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    token = pyjwt.encode(payload, _jwt_secret, algorithm="HS256")
    with pytest.raises(InvalidToken, match="missing required claim: tv"):
        decode_jwt(token)
