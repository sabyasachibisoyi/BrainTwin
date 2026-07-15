"""JWT mint + decode — Phase 4.1 M.M.1.c.

Symmetric-key JWTs (HS256) — the backend both mints AND verifies with
the same secret; no third-party signature path in scope. That's the
right shape for our threat model: bot → backend and web → backend
both live under our control.

Public API:
    mint_user_jwt(user, ttl_minutes=None) -> str
    decode_jwt(token) -> dict     — raises InvalidToken / ExpiredToken

Claims minted (design doc §4.3):
    sub    — str(user.id). RFC 7519 mandates `sub` be a string;
             callers cast back with int() on read. PyJWT would let
             us encode an int, but reading it as str is safer against
             a lib that silently type-coerces.
    email  — user.email at mint time. Not authoritative — the
             callback layer re-reads the DB on every request via
             `get_current_user`. Present here only as debugging
             convenience (log lines that include the email don't
             require a second DB lookup).
    tv     — user.token_version at mint time (Codex Fix 3).
             `get_current_user` compares to the current DB value and
             401s on mismatch — that's how revocation works despite
             the JWT being otherwise stateless.
    iat    — issued-at, unix seconds. Audit trail.
    exp    — expiry, unix seconds. PyJWT rejects with
             ExpiredSignatureError past this; we translate to
             ExpiredToken.

What we DELIBERATELY do NOT include:
    - No jti (JWT id). We don't have a server-side blocklist, and
      `tv` revocation covers the "kick this user out" case.
    - No aud / iss. Would matter if we federated with a third-party
      verifier; we don't. Leaving them out avoids drift between mint
      and decode configs.
    - No refresh token. 30-day TTL matches the pre-4.1 shared-bearer's
      effectively-unbounded lifetime and is the right friend-scale
      UX bar. A refresh-token flow is trivial to bolt on later
      (mint a shorter-lived `access` + a longer-lived `refresh`, add
      a /auth/refresh route) once real usage tells us the daily
      re-sign-in friction matters.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt as pyjwt

from backend.config import reveal, settings
from backend.storage.models import User


class JwtError(Exception):
    """Base for JWT errors from this module.

    Subclasses map to specific HTTP responses in
    `backend.auth.deps.get_current_user`:

      MissingBearer  → 401  "bearer token required"       (from deps, not this file)
      InvalidToken   → 401  "invalid token: <reason>"
      ExpiredToken   → 401  "token expired"
      TokenRevoked   → 401  "token revoked — sign in again"  (from deps — needs DB)
    """


class InvalidToken(JwtError):
    """Signature failed, malformed, wrong algorithm, missing required claim."""


class ExpiredToken(JwtError):
    """The `exp` claim is in the past. Distinct from InvalidToken so the
    caller can show a specific "please sign in again" message instead of
    a generic "invalid credentials"."""


# Required claim names — mint always sets these; decode requires them
# on the incoming token as a belt-and-braces check against a future
# mint bug that omits one.
_REQUIRED_CLAIMS = ("sub", "tv", "iat", "exp")

# HS256 is exactly as strong as the secret's entropy — a short secret
# is offline-crackable from any captured token, which means arbitrary
# token minting for any user_id. put-secrets.sh recommends
# `openssl rand -hex 32` (64 chars) but reads operator input verbatim,
# so enforce a floor here where every mint/verify passes through.
_MIN_SECRET_CHARS = 32


def _get_secret() -> str:
    """Read the JWT secret, raising a RuntimeError if unset or too short.

    Not a JwtError — an unset/weak secret is a *configuration* bug, not
    a per-request error. Distinct so `get_current_user` can map it to a
    fail-closed 503 instead of swallowing it as "invalid token".
    """
    secret = reveal(settings.jwt_secret).strip()
    if not secret:
        raise RuntimeError(
            "JWT_SECRET is not configured — cannot mint or verify JWTs. "
            "Set /braintwin/jwt_secret via put-secrets.sh (M.M.1.b)."
        )
    if len(secret) < _MIN_SECRET_CHARS:
        raise RuntimeError(
            f"JWT_SECRET is too short ({len(secret)} chars; minimum "
            f"{_MIN_SECRET_CHARS}). Generate with `openssl rand -hex 32` "
            "and set /braintwin/jwt_secret via put-secrets.sh."
        )
    return secret


def mint_user_jwt(user: User, *, ttl_minutes: int | None = None) -> str:
    """Mint a signed JWT for the user.

    `ttl_minutes` overrides `settings.jwt_ttl_minutes` — useful for
    tests (short TTL to exercise expiry) and for the bot's short-lived
    per-request tokens (Fable §4.5.1 recommends 5 minutes).

    Reads `user.token_version` at mint time; the token stays valid
    until `bump_token_version` fires OR `exp` passes.
    """
    now = datetime.now(timezone.utc)
    lifetime = ttl_minutes if ttl_minutes is not None else settings.jwt_ttl_minutes
    exp = now + timedelta(minutes=lifetime)
    payload: dict[str, Any] = {
        "sub": str(user.id),
        "email": user.email,
        "tv": int(user.token_version),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return pyjwt.encode(payload, _get_secret(), algorithm="HS256")


def decode_jwt(token: str) -> dict[str, Any]:
    """Verify signature + parse claims. Returns the claims dict.

    Does NOT check `tv` against the DB — that's `get_current_user`'s
    job, because it requires a session. This module stays pure crypto
    + parsing so it's trivially testable with no DB fixtures.

    Raises:
      ExpiredToken — `exp` is in the past
      InvalidToken — signature failure, malformed, wrong algorithm, or
                     missing required claim
    """
    try:
        # `algorithms=["HS256"]` pins the algorithm; without this,
        # PyJWT would accept whatever's in the token's `alg` header —
        # including "none" (unsigned) in older PyJWT versions, which
        # is the classic JWT-library CVE class.
        claims = pyjwt.decode(token, _get_secret(), algorithms=["HS256"])
    except pyjwt.ExpiredSignatureError as e:
        raise ExpiredToken(f"token expired: {e}") from e
    except pyjwt.PyJWTError as e:
        # Covers InvalidSignatureError, DecodeError, InvalidAlgorithmError,
        # InvalidTokenError — collapse to one exception class since the
        # HTTP response is the same (401) and the message is opaque to
        # the client anyway.
        raise InvalidToken(f"invalid token: {e}") from e

    for claim in _REQUIRED_CLAIMS:
        if claim not in claims:
            raise InvalidToken(f"missing required claim: {claim}")

    return claims


__all__ = [
    "JwtError",
    "InvalidToken",
    "ExpiredToken",
    "mint_user_jwt",
    "decode_jwt",
]
