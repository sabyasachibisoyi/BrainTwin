"""Auth package — bearer token (M.1) + JWT / OAuth (M.M.1.*).

Modules:
    bearer   — shared bearer-token dependency (M.1). Used by every
               protected route today; slated for removal after
               M.M.2 flips routes to `get_current_user`.
    jwt      — mint + decode HS256 JWTs (M.M.1.c). Pure crypto,
               no DB. Uses `settings.jwt_secret`.
    pkce     — Proof Key for Code Exchange helpers + oauth_state
               table access (M.M.1.c). RFC 7636.
    deps     — FastAPI dependencies. `get_current_user` is the
               M.M.2 target; `get_session` is a reusable async
               session Depends() wrapper.

Public re-exports below cover:
    - The pre-4.1 import path `from backend.auth import require_bearer_token`
      that main.py + tests currently use — kept working during the
      package refactor so nothing outside this dir needs to change.
    - `settings` — the test suite monkeypatches `auth.settings.backend_bearer_token`
      to swap tokens between test cases; re-exporting keeps that pattern
      working (module attribute lookup finds the singleton).
"""

from backend.auth.bearer import (
    _expected_token,        # test helper access
    _extract_bearer,        # test helper access
    require_bearer_token,
)
from backend.auth.deps import get_current_user, get_session
from backend.auth.google import (
    IdTokenUnavailable,
    InvalidIdToken,
    VerifiedIdentity,
    verify_google_id_token,
)
from backend.auth.jwt import (
    ExpiredToken,
    InvalidToken,
    JwtError,
    decode_jwt,
    mint_user_jwt,
)
from backend.auth.pkce import (
    DEFAULT_STATE_TTL_MINUTES,
    consume_state,
    derive_challenge,
    generate_state,
    generate_verifier,
    store_state,
    sweep_expired,
)
from backend.auth.routes import router as oauth_router
from backend.config import settings  # re-exported for tests/test_auth.py


__all__ = [
    # M.1 shared bearer (retained during 4.1 transition)
    "require_bearer_token",
    # M.M.1.c JWT primitives
    "JwtError",
    "InvalidToken",
    "ExpiredToken",
    "mint_user_jwt",
    "decode_jwt",
    # M.M.1.c PKCE + oauth_state
    "DEFAULT_STATE_TTL_MINUTES",
    "generate_verifier",
    "derive_challenge",
    "generate_state",
    "store_state",
    "consume_state",
    "sweep_expired",
    # M.M.1.c FastAPI deps
    "get_session",
    "get_current_user",
    # M.M.1.d Google id_token verification + OAuth routes
    "InvalidIdToken",
    "IdTokenUnavailable",
    "VerifiedIdentity",
    "verify_google_id_token",
    "oauth_router",
    # Re-exports for test compat
    "settings",
]
