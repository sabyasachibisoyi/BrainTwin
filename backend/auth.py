"""Bearer-token auth dependency — Phase 4.0.6 M.1.

Shipped before any cloud resource exists. The single shared bearer
token gates everything that mutates or reads sensitive state:

  /capture, /recall, /stats, /failures   → protected
  /, /health                              → public (UptimeRobot, smoke tests)

Threat model in scope for this dep:
  - Random scanner hitting the cloud URL with no credentials → 401
  - Operator running locally without BACKEND_BEARER_TOKEN set → 503
    (distinct from "wrong token" so misconfig is loud)
  - Bot/extension with the wrong token → 401

Threat model NOT in scope (per design §11):
  - Replay protection (use TLS — Cloudflare + Caddy both encrypt)
  - Per-user accounts / OAuth (Phase 4.1, use case A)
  - Token rotation without restart (Phase 4.0.6.1)

The token is compared with `hmac.compare_digest` to avoid leaking
timing information to an attacker brute-forcing.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from backend.config import reveal, settings

logger = logging.getLogger(__name__)


def _expected_token() -> str | None:
    """Read the configured bearer token from settings, or None if unset.

    Pulled into a function (rather than a module-level constant) so the
    test suite can monkeypatch settings between cases without import-
    order pain. ``reveal`` unwraps the SecretStr (and tolerates a plain
    str, which is what the tests patch in).
    """
    token = reveal(getattr(settings, "backend_bearer_token", "")).strip()
    return token or None


def _extract_bearer(authorization: str | None) -> str | None:
    """Parse `Authorization: Bearer <token>` and return the token.

    Returns None if the header is absent OR the scheme isn't Bearer.
    We don't try to be clever about case here — the spec says the scheme
    is case-insensitive but the canonical form is "Bearer". Accepting
    "bearer", "BEARER", and "Bearer" is enough.
    """
    if not authorization:
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


async def require_bearer_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """FastAPI dependency that enforces the shared bearer token.

    Usage:
        @app.post("/capture", dependencies=[Depends(require_bearer_token)])
        async def capture_content(...): ...

    Or, when you need the call site to know the dep ran:
        @app.post("/recall")
        async def recall(_: None = Depends(require_bearer_token)): ...

    Returns None on success; raises HTTPException on failure. The
    failure codes are distinct so an operator can tell config bugs
    from credential bugs by glancing at the response:

        503  — bearer not configured on server (misconfig)
        401  — bearer required and either missing or wrong (credential)
    """
    expected = _expected_token()
    if expected is None:
        # Fail closed. Don't accept ANY request just because the operator
        # forgot to set the env var — that would silently downgrade prod
        # to no-auth if the SSM parameter fetch ever returned empty.
        logger.error(
            "BACKEND_BEARER_TOKEN is not set; refusing protected request"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="auth not configured",
        )

    presented = _extract_bearer(authorization)
    if not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Constant-time compare. Both sides are ASCII strings; encode to
    # bytes so compare_digest sees the same length each call regardless
    # of unicode normalization quirks in the request.
    if not hmac.compare_digest(
        presented.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bearer token invalid",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Success — return nothing; FastAPI calls Depends purely for side
    # effects when the return value isn't bound.
    return None
