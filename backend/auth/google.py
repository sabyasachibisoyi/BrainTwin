"""Google id_token verification — Phase 4.1 M.M.1.d.

Wraps `google-auth`'s `id_token.verify_oauth2_token` with our layer of
domain checks. The reason this is a thin wrapper and not `id_token`
called inline in the callback route: `verify_oauth2_token` does
signature + iss + aud + exp verification, but it does NOT check
`email_verified` — and skipping that check is the classic OAuth
identity-spoofing bug (an attacker registers a Google account claiming
someone else's email, and we'd happily bind their captures to the
real owner). See Codex Fix 4 in the M.M.1 design doc.

Public API:
    verify_google_id_token(id_token_str) -> VerifiedIdentity
        Raises InvalidIdToken on any failure.

Threat model:
    - Tampered id_token (bad signature)                    → InvalidIdToken
    - id_token from a different Google OAuth client (aud)  → InvalidIdToken
    - id_token not from Google (iss)                       → InvalidIdToken
    - Expired id_token (exp in the past)                   → InvalidIdToken
    - Google user with unverified email (email_verified=0) → InvalidIdToken
                                                              (impersonation
                                                              protection)
    - Missing `sub` or `email` claim (Google always sends
      both for our scope set, but belt+braces)             → InvalidIdToken

We do NOT check `nonce` — that's an OpenID Connect ID-token binding
between the initial auth request and the callback. Our PKCE code_verifier
already binds those two requests (Google recomputes SHA256(verifier)
during code exchange), so nonce would be redundant. If Google ever
changes their behavior to require nonce, add it to the mix in
routes.py's /auth/google/start (pass `nonce=X` in the auth URL) and
here (assert claims["nonce"] == X after passing state through).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from google.auth import exceptions as g_exceptions
from google.auth.transport import requests as g_requests
from google.oauth2 import id_token as g_id_token

from backend.config import reveal, settings


logger = logging.getLogger(__name__)


# Clock-skew tolerance for the id_token's iat/exp checks. google-auth's
# default is 0 — zero slack — which means a server clock even ~1s behind
# Google's makes a freshly-issued token look "used too early" and 401s a
# legitimate sign-in. 10s matches what most OAuth clients allow and
# shrugs off benign drift between Google's servers and ours.
_CLOCK_SKEW_SECONDS = 10


class InvalidIdToken(Exception):
    """Raised when Google id_token verification fails at any step.

    Maps to a 401 in the OAuth callback (from Google's perspective,
    the code exchange succeeded but WE reject the returned id_token —
    which points at either Google giving us a malformed token, or an
    attacker having intercepted + replayed our callback). Message is
    opaque to the client; details land in the logs for the operator."""


class IdTokenUnavailable(Exception):
    """Raised when we cannot verify the id_token because Google's JWKS
    endpoint is unreachable (DNS/TLS/timeout) — a TRANSPORT failure, not
    a bad token.

    Distinct from InvalidIdToken because the right HTTP response differs:
    the token might be perfectly valid; we just couldn't fetch the keys
    to check it. Maps to a 503 (transient, retryable) in the callback
    rather than a 401 (which tells the client the credential is bad)."""


@dataclass(frozen=True)
class VerifiedIdentity:
    """The subset of id_token claims our callback needs.

    `sub` is Google's stable subject — the primary lookup key on
    subsequent sign-ins (email can technically be re-issued;
    `sub` cannot).

    `email` is what we join to `users.email` in the allowlist check.
    `email_verified` is enforced at verification time (see the module
    docstring), so callers can trust `email` without re-checking.

    Not-in-scope claims we intentionally drop:
      `name`, `picture`, `locale`, `given_name`, `family_name` —
        we don't display Google profile data anywhere in v1. If we
        ever build a friend-facing dashboard that shows the avatar,
        extend this dataclass then; don't grab everything upfront.
    """
    sub: str
    email: str


# Reusable HTTP transport for JWKS fetch. `id_token.verify_oauth2_token`
# uses this internally to pull the current signing keys from Google's
# JWKS URI (rotated every few days). Reuse means we amortize the
# TCP/TLS setup cost across sign-ins — matters if the app is warm.
_transport = g_requests.Request()


def verify_google_id_token(id_token_str: str) -> VerifiedIdentity:
    """Verify a Google id_token and return the essential claims.

    Enforces (in order):
      1. Signature valid (via `google-auth`'s JWKS fetch + RS256 verify)
      2. `aud` == our configured `GOOGLE_OAUTH_CLIENT_ID`
         (verify_oauth2_token does this when we pass audience)
      3. `iss` in Google's canonical issuer set (google-auth does this
         internally against `accounts.google.com` / `https://accounts.google.com`)
      4. `exp` in the future (google-auth raises on expired)
      5. `email_verified` is True (WE do this — google-auth doesn't)
      6. `sub` and `email` present (belt-and-braces)

    Any failure → InvalidIdToken. The message includes just enough
    context for a log-scanner to distinguish the failure class without
    leaking token contents.
    """
    client_id = reveal(settings.google_oauth_client_id).strip()
    if not client_id:
        # Not an InvalidIdToken — this is a config bug, not a bad
        # token. Callers (the callback route) should map to 503.
        raise RuntimeError(
            "GOOGLE_OAUTH_CLIENT_ID is not configured — cannot verify "
            "id_tokens. Set /braintwin/google_oauth_client_id via "
            "put-secrets.sh (M.M.1.b)."
        )

    try:
        claims: dict[str, Any] = g_id_token.verify_oauth2_token(
            id_token_str,
            _transport,
            audience=client_id,
            # google-auth defaults clock_skew_in_seconds to 0 (no
            # tolerance), so we must pass this explicitly or benign clock
            # drift 401s legitimate sign-ins. See _CLOCK_SKEW_SECONDS.
            clock_skew_in_seconds=_CLOCK_SKEW_SECONDS,
        )
    except g_exceptions.TransportError as e:
        # JWKS fetch failed (Google's certs endpoint unreachable). This
        # is NOT a ValueError and NOT a bad token — surface it as a
        # distinct "unavailable" so the callback returns 503, not 401.
        logger.warning("google JWKS fetch failed (transport): %s", e)
        raise IdTokenUnavailable(
            f"could not reach google to verify id_token: {e}"
        ) from e
    except ValueError as e:
        # `verify_oauth2_token` raises ValueError for every failure
        # mode: bad signature, wrong audience, expired, malformed,
        # non-Google issuer. Collapse to InvalidIdToken since the
        # HTTP response is the same 401 either way.
        logger.warning("google id_token verification failed: %s", e)
        raise InvalidIdToken(f"google verification failed: {e}") from e

    # Domain checks — google-auth returns claims dict on success but
    # doesn't enforce our extra invariants (email_verified is the big
    # one).
    if not claims.get("email_verified"):
        # This is the impersonation defense: Google allows users to
        # create an account with an arbitrary email but marks
        # email_verified=false until they click the verification link.
        # An attacker could create a Google account claiming
        # friend@example.com, get an id_token with the right email
        # but email_verified=false, and — if we didn't check this —
        # sign in as the real friend. Real users' Google accounts
        # always have email_verified=true, so this reject is safe.
        raise InvalidIdToken(
            "google account email is not verified — cannot use for sign-in"
        )

    email = claims.get("email")
    sub = claims.get("sub")
    if not email or not sub:
        # Google's response for openid+email scope ALWAYS includes
        # these; missing means either google-auth returned a weird
        # dict shape, or someone tampered with the payload in a way
        # that somehow passed signature check (which shouldn't be
        # possible). Fail loud either way.
        raise InvalidIdToken(
            f"google id_token missing required claim (sub={bool(sub)}, "
            f"email={bool(email)})"
        )

    return VerifiedIdentity(sub=str(sub), email=str(email))


__all__ = [
    "InvalidIdToken",
    "IdTokenUnavailable",
    "VerifiedIdentity",
    "verify_google_id_token",
]
