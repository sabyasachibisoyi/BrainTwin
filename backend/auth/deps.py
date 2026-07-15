"""FastAPI auth dependencies — Phase 4.1 M.M.1.c.

`get_current_user` is the M.M.2 target — every existing route wired
via `Depends(require_bearer_token)` gets a mechanical swap to
`Depends(get_current_user)` and starts scoping data per-user via the
returned `User`. This substep just ships the dependency; the route
swap is M.M.2.

Response codes (design doc §4.3 + Codex Fix 5):
    401  — missing bearer, malformed token, invalid signature, expired,
           or `tv` claim doesn't match the DB (revoked)
    403  — token decodes cleanly and `tv` matches, but the user_id
           doesn't exist in the DB anymore (account deleted between
           token mint and this request — treat as "you're forbidden
           from acting on behalf of this deleted account")
    503  — JWT_SECRET unset/invalid on the server (misconfig). Same
           fail-closed contract as bearer.py: a config bug must be
           loud and distinct from credential failures, never a 500.

Codex Fix 5 — `Header(None)` NOT `Header(...)`:
    FastAPI's `Header(...)` treats the header as REQUIRED and returns
    422 Unprocessable Entity if it's missing. For an auth header, 401
    is the semantically correct response — that's what tells a client
    library to prompt for credentials. Using `Header(None)` (default
    None) lets our handler emit its own 401 with WWW-Authenticate,
    matching the RFC 7235 contract every HTTP client expects.
"""

from __future__ import annotations

import logging
from typing import Annotated, AsyncIterator

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.bearer import _extract_bearer
from backend.auth.jwt import ExpiredToken, InvalidToken, decode_jwt
from backend.storage import session_scope
from backend.storage.models import User
from backend.storage.repositories import UserRepository


logger = logging.getLogger(__name__)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency wrapper around `session_scope`.

    Yields exactly one session per request; commits on success,
    rolls back on exception. Splitting this from `get_current_user`
    lets other route handlers `Depends(get_session)` too and share
    the same session with the auth check (FastAPI dedupes Depends by
    identity, so a request that does both `get_current_user` and its
    own DB work uses ONE session, ONE transaction).
    """
    async with session_scope() as session:
        yield session


async def get_current_user(
    # Header(None) — see the Codex Fix 5 note in the module docstring.
    # `Annotated[str | None, Header()]` produces the same runtime
    # behaviour as `Header(default=None)` under FastAPI's dep resolver;
    # keeping the Annotated form for consistency with `bearer.py` and
    # the FastAPI-recommended style.
    authorization: Annotated[str | None, Header()] = None,
    session: AsyncSession = Depends(get_session),
) -> User:
    """Extract JWT, decode, load user, check revocation, return the User.

    Failure paths, in order — each raises immediately, no fall-through:

      1. No Authorization header (or wrong scheme, or empty token)
         → 401 "bearer token required"
      2. Token expired
         → 401 "token expired"
      3. Token invalid (bad signature / malformed / missing claim)
         → 401 "invalid token: <reason>"
      4. `sub` or `tv` claim isn't parseable as int
         → 401 "invalid token: <claim> is not an int"
      5. User doesn't exist in DB (deleted account, or `sub` is fabricated)
         → 403 "user not found"
      6. `tv` claim doesn't match DB
         → 401 "token revoked — sign in again"

    Config failure (JWT_SECRET unset/too short) is not a numbered path —
    it 503s before any of the above, mirroring bearer.py.

    Success: returns a fully-populated User dataclass. Route handlers
    that need the caller's identity take
    `user: User = Depends(get_current_user)`.
    """
    token = _extract_bearer(authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = decode_jwt(token)
    except RuntimeError as e:
        # Unset/weak JWT_SECRET — a *configuration* bug, not a
        # credential failure. Mirror bearer.py's fail-closed 503 so
        # operators can tell misconfig from bad tokens at a glance
        # instead of getting a generic 500.
        logger.error("JWT auth misconfigured: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="auth not configured",
        )
    except ExpiredToken:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except InvalidToken as e:
        # Message from decode_jwt already reads like a debug string
        # ("missing required claim: sub"); pass through so the caller
        # can log it. Not leaking anything sensitive — the message is
        # about the token's SHAPE, not its content.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_id = int(claims["sub"])
    except (ValueError, TypeError):
        # decode_jwt's required-claims check ensures `sub` exists; this
        # catches the case where mint (or a tampered token that
        # somehow passed sig verification, i.e. an attacker who
        # obtained the secret) put a non-int value there.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token: sub is not an int",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        token_tv = int(claims["tv"])
    except (ValueError, TypeError):
        # Same trust boundary as the sub guard above: a non-numeric tv
        # can only come from a mint bug or a forged token. 401, not an
        # uncaught ValueError → 500.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token: tv is not an int",
            headers={"WWW-Authenticate": "Bearer"},
        )

    repo = UserRepository(session)
    user = await repo.get(user_id)
    if user is None:
        # 403 not 401 — the token is valid, we just no longer recognize
        # the identity. Signing in again with the same account won't
        # help (the account is gone); the user needs to be re-added to
        # the allowlist by an admin.
        logger.info(
            "get_current_user: token decoded for user_id=%d but not in DB "
            "(deleted account? DB rollback?)",
            user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="user not found",
        )

    if token_tv != int(user.token_version):
        # Revocation: the DB's token_version was bumped since this JWT
        # was minted. 401 (not 403) so client libraries treat it as
        # "sign in again" rather than "you can never sign in".
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token revoked — sign in again",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


__all__ = ["get_session", "get_current_user"]
