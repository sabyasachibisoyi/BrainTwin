"""OAuth 2.0 authorization-code flow routes — Phase 4.1 M.M.1.d.

Two endpoints implementing the Google sign-in dance:

    GET /auth/google/start    → redirects to Google's auth URL
    GET /auth/google/callback → completes the exchange, mints a JWT,
                                 redirects to the landing page with
                                 the JWT in the URL fragment

Isolated in a FastAPI APIRouter so main.py only does
`app.include_router(auth_router)` — the OAuth surface lives in one
place, and future auth-adjacent routes (POST /auth/logout, GET
/auth/whoami, DELETE /account when M.M.2 lands) drop in here rather
than growing main.py further.

Flow (Layer-3 view — see M.M.1.c package docstring for the layers):

    Browser        Backend                 Google
       |              |                       |
       | GET /start   |                       |
       |------------->| generate state, PKCE  |
       |              | store_state           |
       |              | 302 to auth URL       |
       |<---------- 302 -----------------------
       |                                      |
       | GET auth URL --------------------->  | (Google's UI)
       |                                      |
       | 302 to /callback?code&state  <-------|
       |              |                       |
       | GET /callback|                       |
       |------------->| consume_state (one-   |
       |              |   shot; anti-replay)  |
       |              | POST token endpoint --|
       |              |<----- id_token -------|
       |              | verify_google_id_token|
       |              | look up user_by_sub OR|
       |              |          _by_email    |
       |              | mint_user_jwt(user)   |
       |              | 302 to landing#token= |
       |<---------- 302 ----------------------|

Failure modes surface as HTTP responses to the browser (so the user
sees a page, not a JSON blob):

    /start:
      503 — GOOGLE_OAUTH_CLIENT_ID unset (config error)

    /callback:
      400 — missing/invalid state param (never issued, expired, or
            already consumed = replay attempt), OR the state doesn't
            match the browser's binding cookie (login-CSRF attempt)
      400 — missing `code` param (Google redirected without one —
            usually because the user clicked "Deny" at Google's
            consent screen; Google sends error=access_denied instead
            of code)
      401 — Google token exchange failed (auth code invalid / already
            used / bad client_secret) or returned a malformed body
      401 — id_token verification failed (see backend.auth.google)
      403 — verified email is not in our allowlist (Sabya-only signup)
      403 — email is allowlisted but the row is already bound to a
            DIFFERENT Google sub (account-rebind attempt)
      503 — GOOGLE_OAUTH_CLIENT_ID/SECRET unset (config error), or
            Google's JWKS endpoint was unreachable (transient)

Post-callback UX: we redirect to `LANDING_PATH#token=<jwt>` and let
client-side JS grab the token from `window.location.hash`. Fragment
(not query string) so the token doesn't hit our access logs / any
downstream service accidentally.

M.M.2 flips /capture, /recall, /stats, /failures to require the JWT.
Until then, this substep only lets you obtain a JWT — nothing
enforces it. That's deliberate; deploying M.M.1.d to prod is safe
because no existing behavior changes.
"""

from __future__ import annotations

import dataclasses
import logging
import secrets
import urllib.parse
from typing import Optional

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.deps import get_session
from backend.auth.google import (
    IdTokenUnavailable,
    InvalidIdToken,
    VerifiedIdentity,
    verify_google_id_token,
)
from backend.auth.jwt import mint_user_jwt
from backend.auth.pkce import (
    DEFAULT_STATE_TTL_MINUTES,
    consume_state,
    derive_challenge,
    generate_state,
    generate_verifier,
    store_state,
)
from backend.config import reveal, settings
from backend.storage.models import User
from backend.storage.repositories import UserRepository


logger = logging.getLogger(__name__)


# Google's OAuth 2.0 endpoints. Constants at module scope so tests can
# monkeypatch them if they want to point at a fake auth server.
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Where to send the browser after successful sign-in. Path-only (no
# domain) so the redirect stays same-origin — the JS on this page
# picks up the token from `window.location.hash` and stashes it.
# Overridable via settings later if we add multiple landing pages;
# for now, hard-coded matches Fable's §5.1 (minimal landing).
LANDING_PATH = "/"


# Scopes the OAuth consent screen asks for. `openid` + `email` are
# both non-sensitive per Google's classification, which is why we
# were able to publish the OAuth app to "In production" without
# Google's verification review (Fable §4.2). Adding e.g. `profile`
# would kick us back into the verification workflow.
OAUTH_SCOPES = ["openid", "email"]


# Name of the cookie that binds a /callback to the browser that hit
# /start. We set it (= the `state` value) in /start and require the
# callback's `state` query param to match it. Without this, `state` is
# only stored server-side and ANY browser presenting ANY unconsumed
# state passes — a login-CSRF hole where an attacker completes their own
# Google consent, then makes the victim's browser visit the callback and
# silently logs the victim into the ATTACKER's account. SameSite=Lax
# lets the cookie ride the top-level GET redirect back from Google
# (Strict would not), so this is a standard double-submit binding.
STATE_COOKIE_NAME = "bt_oauth_state"


router = APIRouter(prefix="/auth/google", tags=["auth"])


# --------------------------------------------------------------------
# Config helpers
# --------------------------------------------------------------------

def _get_client_id() -> str:
    """Read GOOGLE_OAUTH_CLIENT_ID or raise 503.

    Empty string = fail-closed. Same pattern bearer.py uses — an
    unset secret is a CONFIG bug and should look like one (503,
    "auth not configured"), not a request-shape bug (401 or 400).
    """
    value = reveal(settings.google_oauth_client_id).strip()
    if not value:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="auth not configured (GOOGLE_OAUTH_CLIENT_ID unset)",
        )
    return value


def _get_client_secret() -> str:
    """Same fail-closed pattern for the client secret."""
    value = reveal(settings.google_oauth_client_secret).strip()
    if not value:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="auth not configured (GOOGLE_OAUTH_CLIENT_SECRET unset)",
        )
    return value


def _get_redirect_uri() -> str:
    """The redirect_uri passed to Google in BOTH /start (the auth URL)
    AND /callback (the token exchange). MUST match one of the URIs
    registered on the Google OAuth client, or Google rejects with
    `redirect_uri_mismatch`.

    Falls back to the localhost default (from settings) which works
    for local dev. Prod overrides via SSM (Fable's redirect_uri catch
    ensures the value gets into the environment).
    """
    return settings.google_oauth_redirect_uri.strip()


# --------------------------------------------------------------------
# GET /auth/google/start
# --------------------------------------------------------------------

@router.get("/start", response_class=RedirectResponse, status_code=302)
async def google_start(
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Kick off the OAuth flow: generate PKCE + state, store them,
    redirect the browser to Google's authorize endpoint.

    No inputs from the browser — this is the entry point for a
    fresh sign-in. Anyone with the URL can hit it (that's intentional;
    the allowlist check happens post-callback when we know the
    identity)."""
    client_id = _get_client_id()
    redirect_uri = _get_redirect_uri()

    # PKCE + CSRF setup. Both random, both persisted to the
    # oauth_state table (verifier so /callback can complete the
    # exchange; state so /callback can prove the round-trip belongs
    # to this /start call).
    state = generate_state()
    verifier = generate_verifier()
    challenge = derive_challenge(verifier)

    # Persist BEFORE building the redirect URL — if store_state
    # raises (e.g. DB is down), we haven't yet told the browser to
    # go anywhere, so the failure is a clean 500 rather than a
    # "you're at Google's page but our end doesn't remember you"
    # dead-end.
    await store_state(session, state=state, code_verifier=verifier)

    # Query params per RFC 6749 §4.1 + RFC 7636 §4.3.
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(OAUTH_SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # `access_type=online` — we don't need a refresh token; each
        # sign-in mints a fresh JWT with our own TTL. `prompt=select_account`
        # ensures the friend gets to pick their Google account rather
        # than being silently logged in with their default one, which
        # matters if multiple friends share a browser.
        "access_type": "online",
        "prompt": "select_account",
    }
    auth_url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    response = RedirectResponse(url=auth_url, status_code=302)
    # Bind this flow to the initiating browser (see STATE_COOKIE_NAME).
    # secure is derived from the redirect scheme so local-dev http works
    # while prod https gets a Secure cookie. httponly: JS never needs to
    # read it. max_age matches the state row's TTL so a stale cookie
    # can't outlive the server-side state it pairs with.
    response.set_cookie(
        key=STATE_COOKIE_NAME,
        value=state,
        max_age=DEFAULT_STATE_TTL_MINUTES * 60,
        httponly=True,
        samesite="lax",
        secure=redirect_uri.startswith("https://"),
        path="/auth/google",
    )
    return response


# --------------------------------------------------------------------
# GET /auth/google/callback
# --------------------------------------------------------------------

@router.get("/callback", response_class=RedirectResponse, status_code=302)
async def google_callback(
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
    state_cookie: Optional[str] = Cookie(default=None, alias=STATE_COOKIE_NAME),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Complete the OAuth flow: verify state, exchange code, verify
    id_token, look up user in allowlist, mint JWT, redirect.

    Query params come from Google's redirect — three cases:
      code + state present    → happy path
      state + error present   → user hit "Deny" (or Google errored out)
                                → 400 with the Google-provided error string
      any missing param       → malformed callback (attacker probe?)
                                → 400
    """
    # Fail-closed on missing config BEFORE touching the state table
    # — a callback with a fresh state token would consume-and-delete
    # the row even though the exchange couldn't proceed, forcing the
    # user to re-do the /start step to get a new one. Cleaner to
    # bail before consumption.
    client_id = _get_client_id()
    client_secret = _get_client_secret()
    redirect_uri = _get_redirect_uri()

    # 1. Guard against Google-returned errors (user denied consent).
    if error:
        # `error` = "access_denied" is the common case; other values
        # per RFC 6749 §4.1.2.1 (server_error, temporarily_unavailable,
        # etc.). Pass through in the message for the operator to see
        # in the logs; the browser gets a generic 400.
        logger.info("google callback returned error=%s", error)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"google reported: {error}",
        )

    # 2. Guard against malformed callbacks (attacker probes / bad
    #    redirects hitting our endpoint).
    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing required query parameter (code, state)",
        )

    # 3. CSRF binding: the `state` query param must match the cookie we
    #    set in /start on THIS browser. The server-side oauth_state row
    #    proves the state was issued by us; this proves it was issued to
    #    the browser now completing the flow. Without it, an attacker who
    #    ran /start themselves could feed their own (code, state) into the
    #    victim's browser and log the victim into the attacker's account.
    #    compare_digest avoids a timing side-channel on the comparison.
    if not state_cookie or not secrets.compare_digest(state_cookie, state):
        logger.info("google callback state/cookie mismatch (possible CSRF)")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired state — please start sign-in again",
        )

    # 4. One-shot consume of the state → verifier mapping. If the state
    #    was never issued, expired, or already consumed, this returns
    #    None. Each case is functionally identical from the caller's
    #    perspective — they need to restart the sign-in flow — so we
    #    collapse to one 400.
    verifier = await consume_state(session, state)
    if verifier is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired state — please start sign-in again",
        )

    # Commit the consumption NOW so it survives any later failure. The
    # request shares one session/transaction (get_session), which rolls
    # back on any raised HTTPException — so without this commit, a state
    # consumed here would be RESTORED when the token exchange 401s, the
    # id_token verify 401s, or the allowlist 403s, leaving it replayable
    # until its TTL and voiding the one-shot guarantee. Committing the
    # delete on its own detaches replay-protection from the outcome.
    await session.commit()

    # 5. Exchange (code, verifier) with Google. Google verifies our
    #    client_id + client_secret, recomputes SHA256(verifier) and
    #    checks it against the code_challenge we sent in /start, and
    #    returns an id_token if everything lines up.
    async with httpx.AsyncClient(timeout=10.0) as client:
        token_response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
        )
    if token_response.status_code != 200:
        # Google's error body has an `error` + `error_description`.
        # Log both; return a generic 401 (the caller doesn't need to
        # know whether the code was invalid vs expired vs already-used).
        logger.warning(
            "google token exchange failed: status=%d body=%s",
            token_response.status_code,
            token_response.text[:500],
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="google code exchange failed",
        )

    try:
        token_payload = token_response.json()
    except ValueError:
        # 200 status but a non-JSON body (e.g. an egress proxy / captive
        # portal returning HTML). httpx's .json() raises a ValueError
        # subclass; without this guard it would surface as an uncaught
        # 500 instead of the intended auth-failure response.
        logger.error(
            "google token exchange returned non-JSON 200 body: %s",
            token_response.text[:500],
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="google response malformed",
        )
    id_token_str = token_payload.get("id_token")
    if not id_token_str:
        # Well-formed 200 response but no id_token? Google shouldn't
        # do this for our scope set, but belt-and-braces.
        logger.error("google 200 response missing id_token field")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="google response missing id_token",
        )

    # 6. Verify signature + audience + email_verified via google-auth
    #    (see backend/auth/google.py for the full check list).
    try:
        identity = verify_google_id_token(id_token_str)
    except IdTokenUnavailable as e:
        # Couldn't reach Google's JWKS endpoint to fetch signing keys —
        # transient upstream outage, not a bad token. 503 (retryable),
        # not 401 (which would tell the user their credential is bad).
        logger.warning("id_token verification unavailable: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="sign-in temporarily unavailable — please retry",
        )
    except InvalidIdToken as e:
        logger.warning("id_token verification rejected: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="id_token verification failed",
        )

    # 7. Allowlist lookup. Two strategies in order of preference:
    #      a) by `sub` — the primary key for returning users (email can
    #         change; sub can't)
    #      b) by `email` — for first-time sign-in where the users row
    #         was seeded by Sabya-the-admin BEFORE the friend ever
    #         signed in (so no `sub` yet). On first successful sign-in
    #         we bind `oauth_google_sub` so future lookups take path (a).
    repo = UserRepository(session)
    user = await repo.get_by_oauth_sub(identity.sub)
    if user is None:
        user = await repo.get_by_email(identity.email)
    if user is None:
        # Not in the allowlist. 403 with a message that hints at the
        # right escalation path — the friend messages Sabya, who adds
        # them to `users` (M.M.5 admin UI will make this a click, for
        # now it's a manual SQL INSERT via the runbook).
        logger.info(
            "sign-in rejected: email=%s sub=%s not in allowlist",
            identity.email, identity.sub,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"not authorised: {identity.email} is not on the "
                "allowlist. Ask Sabya to add you."
            ),
        )

    # Bind or verify the Google `sub`.
    if user.oauth_google_sub is None:
        # First sign-in for an email-allowlisted user: bind the sub so
        # future sign-ins short-circuit to get_by_oauth_sub. Update the
        # in-memory object rather than re-reading the row — mint_user_jwt
        # only needs id/email/token_version (none changed), and User is a
        # frozen dataclass, so replace() is the cheap, consistent path.
        await repo.set_oauth_sub(user.id, identity.sub)
        user = dataclasses.replace(user, oauth_google_sub=identity.sub)
    elif user.oauth_google_sub != identity.sub:
        # The row is already bound to a DIFFERENT Google sub. A sub is
        # immutable per Google account, so a mismatch means a different
        # Google identity is presenting this allowlisted email — reject
        # rather than silently rebind the account (and all its captures)
        # to the new identity. Reached only when get_by_oauth_sub missed
        # but get_by_email hit an already-bound row.
        logger.warning(
            "sign-in rejected: email=%s presented sub=%s but the row is "
            "bound to a different sub (possible account-rebind attempt)",
            identity.email, identity.sub,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "not authorised: this account is bound to a different "
                "Google identity. Ask Sabya for help."
            ),
        )

    # 8. Mint the JWT and redirect. Clear the one-shot state cookie on
    #    the way out — it has served its purpose and shouldn't linger.
    token = mint_user_jwt(user)
    landing_url = f"{LANDING_PATH}#token={urllib.parse.quote(token)}"
    logger.info(
        "sign-in success: user_id=%d email=%s tv=%d",
        user.id, user.email, user.token_version,
    )
    response = RedirectResponse(url=landing_url, status_code=302)
    response.delete_cookie(STATE_COOKIE_NAME, path="/auth/google")
    return response


__all__ = [
    "router",
    "GOOGLE_AUTH_URL",
    "GOOGLE_TOKEN_URL",
    "LANDING_PATH",
    "OAUTH_SCOPES",
]
