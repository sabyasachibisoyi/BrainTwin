"""Tests for backend/auth/routes.py — Phase 4.1 M.M.1.d.

Exercises the /auth/google/start and /auth/google/callback routes
via FastAPI TestClient. Uses a stub google-auth (patched at import
site inside routes.py's caller `google.py`) and a stub httpx client
for the token-exchange leg — no real network calls.

Coverage:
  /start:
    - happy path returns 302 to google's auth URL with all required
      query params (client_id, redirect_uri, response_type, scope,
      state, code_challenge, code_challenge_method=S256)
    - state row is persisted to oauth_state
    - unset client_id → 503

  /callback:
    - happy path: sets up state, mocks Google token endpoint + id_token
      verify, expects 302 with #token=<jwt> in the redirect
    - google returned error=access_denied → 400
    - missing code param → 400
    - missing state param → 400
    - unknown state → 400 (never issued OR already consumed = replay)
    - expired state → 400
    - google token exchange returned 400 → 401
    - id_token verification failed → 401
    - email NOT in allowlist → 403
    - happy path for pre-allowlisted user (email in DB, sub NULL):
      first sign-in backfills users.oauth_google_sub
    - unset client_secret → 503

  post-callback:
    - JWT in the redirect fragment decodes cleanly with our
      mint_user_jwt/decode_jwt pair
"""

from __future__ import annotations

import asyncio
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from backend.auth.jwt import decode_jwt  # noqa: E402
from backend.auth.pkce import (  # noqa: E402
    derive_challenge,
    generate_state,
    generate_verifier,
    store_state,
)
from backend.auth.routes import (  # noqa: E402
    GOOGLE_TOKEN_URL,
    STATE_COOKIE_NAME,
    router as oauth_router,
)
from backend.config import settings  # noqa: E402
from backend.storage import aclose, init_db, session_scope  # noqa: E402
from backend.storage import db as db_module  # noqa: E402
from backend.storage.repositories import UserRepository  # noqa: E402
from backend.storage.schema import oauth_state, users  # noqa: E402


# ---- App factory + fixtures ----------------------------------------

def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(oauth_router)
    return app


@pytest.fixture(autouse=True)
def _clean_engine(monkeypatch):
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_session_factory", None)
    yield
    try:
        asyncio.run(aclose())
    except Exception:
        pass


@pytest.fixture
def _oauth_config(monkeypatch):
    """Set all four OAuth-related config values to plausible-looking
    strings so the config-check routes take the happy path. Tests that
    want to exercise the unset-config path override individually."""
    monkeypatch.setattr(
        settings,
        "google_oauth_client_id",
        "test-client-id.apps.googleusercontent.com",
    )
    monkeypatch.setattr(
        settings,
        "google_oauth_client_secret",
        "GOCSPX-test-client-secret-value",
    )
    monkeypatch.setattr(
        settings,
        "google_oauth_redirect_uri",
        "http://localhost:8000/auth/google/callback",
    )
    monkeypatch.setattr(
        settings,
        "jwt_secret",
        "test-jwt-secret-min-32-chars-abcdefg",
    )


def _seed_user(email: str = "friend@example.com", **extra):
    """Create a user row for callback-side allowlist tests."""

    async def run():
        await init_db()
        async with session_scope() as session:
            return await UserRepository(session).create(email=email, **extra)

    return asyncio.run(run())


def _init_db_only():
    """Just build the schema; don't insert anything (for callback tests
    where the allowlist is deliberately empty)."""

    async def run():
        await init_db()

    asyncio.run(run())


def _seed_oauth_state(state: str, verifier: str, *, expired: bool = False):
    """Insert a valid or expired oauth_state row. Used by callback tests
    that need to bypass the /start step and jump straight to callback
    exercise."""

    async def run():
        await init_db()
        now = datetime.now(timezone.utc)
        expires = now - timedelta(minutes=5) if expired else now + timedelta(minutes=10)
        async with session_scope() as session:
            await session.execute(
                insert(oauth_state).values(
                    state=state,
                    code_verifier=verifier,
                    created_at=now.isoformat(),
                    expires_at=expires.isoformat(),
                )
            )

    asyncio.run(run())


def _fetch_stored_states():
    """Return every row in oauth_state — used by /start tests to
    confirm persistence."""

    async def run():
        async with session_scope() as session:
            return (await session.execute(select(oauth_state))).fetchall()

    return asyncio.run(run())


def _mock_token_response(id_token: str = "fake-id-token", status_code: int = 200):
    """Build a mock httpx.Response for Google's /token endpoint."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = {"id_token": id_token, "access_token": "at"}
    mock.text = f'{{"id_token": "{id_token}"}}' if status_code == 200 else '{"error": "invalid_grant"}'
    return mock


# --------------------------------------------------------------------
# /auth/google/start
# --------------------------------------------------------------------

def test_start_redirects_to_google_with_required_params(_oauth_config):
    _init_db_only()
    client = TestClient(_make_app(), follow_redirects=False)
    r = client.get("/auth/google/start")
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(location).query))
    for required in (
        "client_id", "redirect_uri", "response_type", "scope",
        "state", "code_challenge", "code_challenge_method",
    ):
        assert required in params, f"missing param {required} in {location}"
    assert params["response_type"] == "code"
    assert params["code_challenge_method"] == "S256"
    # scopes: openid + email
    assert "openid" in params["scope"]
    assert "email" in params["scope"]


def test_start_persists_state_row(_oauth_config):
    _init_db_only()
    client = TestClient(_make_app(), follow_redirects=False)
    r = client.get("/auth/google/start")
    location = r.headers["location"]
    state_from_url = dict(urllib.parse.parse_qsl(
        urllib.parse.urlparse(location).query
    ))["state"]
    stored = _fetch_stored_states()
    assert len(stored) == 1
    assert stored[0].state == state_from_url
    # code_verifier is our secret; derive_challenge(verifier) should
    # equal the code_challenge Google saw.
    challenge_from_url = dict(urllib.parse.parse_qsl(
        urllib.parse.urlparse(location).query
    ))["code_challenge"]
    assert derive_challenge(stored[0].code_verifier) == challenge_from_url


def test_start_returns_503_when_client_id_unset(_oauth_config, monkeypatch):
    monkeypatch.setattr(settings, "google_oauth_client_id", "")
    _init_db_only()
    client = TestClient(_make_app(), follow_redirects=False)
    r = client.get("/auth/google/start")
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"]


# --------------------------------------------------------------------
# /auth/google/callback — happy path
# --------------------------------------------------------------------

def test_callback_happy_path_returns_jwt_in_fragment(_oauth_config):
    user = _seed_user(email="friend@example.com")
    state, verifier = generate_state(), generate_verifier()
    _seed_oauth_state(state, verifier)

    # Mock Google's /token endpoint AND our id_token verification.
    with patch(
        "backend.auth.routes.httpx.AsyncClient"
    ) as MockClient, patch(
        "backend.auth.routes.verify_google_id_token"
    ) as mock_verify:
        # httpx.AsyncClient() context manager returns a client whose
        # .post() returns the mocked response.
        mock_ctx = MagicMock()
        mock_post = MagicMock()
        # asyncio-friendly: mock the coroutine's return
        async def fake_post(*args, **kwargs):
            return _mock_token_response(id_token="test-id-token")
        mock_ctx.post = fake_post
        MockClient.return_value.__aenter__.return_value = mock_ctx
        MockClient.return_value.__aexit__.return_value = None

        # verify_google_id_token returns our VerifiedIdentity
        from backend.auth.google import VerifiedIdentity
        mock_verify.return_value = VerifiedIdentity(
            sub="google-sub-for-friend",
            email="friend@example.com",
        )

        client = TestClient(_make_app(), follow_redirects=False)
        client.cookies.set(STATE_COOKIE_NAME, state)
        r = client.get(
            "/auth/google/callback",
            params={"code": "auth-code-123", "state": state},
        )

    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith("/#token=")
    # Pull the JWT out of the URL fragment and decode it.
    token_encoded = location.split("#token=", 1)[1]
    token = urllib.parse.unquote(token_encoded)
    claims = decode_jwt(token)
    assert claims["sub"] == str(user.id)
    assert claims["email"] == "friend@example.com"


# --------------------------------------------------------------------
# /auth/google/callback — guard clauses
# --------------------------------------------------------------------

def test_callback_google_error_returns_400(_oauth_config):
    _init_db_only()
    client = TestClient(_make_app(), follow_redirects=False)
    r = client.get(
        "/auth/google/callback",
        params={"error": "access_denied"},
    )
    assert r.status_code == 400
    assert "access_denied" in r.json()["detail"]


def test_callback_missing_code_returns_400(_oauth_config):
    _init_db_only()
    client = TestClient(_make_app(), follow_redirects=False)
    r = client.get(
        "/auth/google/callback",
        params={"state": "some-state"},
    )
    assert r.status_code == 400


def test_callback_missing_state_returns_400(_oauth_config):
    _init_db_only()
    client = TestClient(_make_app(), follow_redirects=False)
    r = client.get(
        "/auth/google/callback",
        params={"code": "some-code"},
    )
    assert r.status_code == 400


def test_callback_unknown_state_returns_400(_oauth_config):
    """State that was never issued — could be an attacker probe or a
    stale bookmark. Same 400 either way; the message tells the user to
    restart sign-in."""
    _init_db_only()
    client = TestClient(_make_app(), follow_redirects=False)
    client.cookies.set(STATE_COOKIE_NAME, "never-issued-state")
    r = client.get(
        "/auth/google/callback",
        params={"code": "some-code", "state": "never-issued-state"},
    )
    assert r.status_code == 400
    assert "start sign-in again" in r.json()["detail"]


def test_callback_expired_state_returns_400(_oauth_config):
    """Row exists but expires_at is in the past. consume_state returns
    None, we treat it the same as unknown-state — restart required."""
    state, verifier = generate_state(), generate_verifier()
    _seed_oauth_state(state, verifier, expired=True)
    client = TestClient(_make_app(), follow_redirects=False)
    client.cookies.set(STATE_COOKIE_NAME, state)
    r = client.get(
        "/auth/google/callback",
        params={"code": "some-code", "state": state},
    )
    assert r.status_code == 400


def test_callback_google_token_exchange_400_returns_401(_oauth_config):
    """Google's /token endpoint returned non-200 (e.g. invalid_grant
    because the auth code was already used, or bad client_secret).
    We collapse to 401 — no useful client-facing distinction."""
    state, verifier = generate_state(), generate_verifier()
    _seed_oauth_state(state, verifier)

    with patch("backend.auth.routes.httpx.AsyncClient") as MockClient:
        mock_ctx = MagicMock()
        async def fake_post(*args, **kwargs):
            return _mock_token_response(status_code=400)
        mock_ctx.post = fake_post
        MockClient.return_value.__aenter__.return_value = mock_ctx
        MockClient.return_value.__aexit__.return_value = None

        client = TestClient(_make_app(), follow_redirects=False)
        client.cookies.set(STATE_COOKIE_NAME, state)
        r = client.get(
            "/auth/google/callback",
            params={"code": "bad-code", "state": state},
        )
    assert r.status_code == 401


def test_callback_id_token_verification_failed_returns_401(_oauth_config):
    """Token exchange succeeded but id_token failed verification (bad
    signature, wrong audience, email_verified=false, etc.). All
    collapse to 401."""
    state, verifier = generate_state(), generate_verifier()
    _seed_oauth_state(state, verifier)

    from backend.auth.google import InvalidIdToken
    with patch(
        "backend.auth.routes.httpx.AsyncClient"
    ) as MockClient, patch(
        "backend.auth.routes.verify_google_id_token",
        side_effect=InvalidIdToken("email not verified"),
    ):
        mock_ctx = MagicMock()
        async def fake_post(*args, **kwargs):
            return _mock_token_response(id_token="tampered-id-token")
        mock_ctx.post = fake_post
        MockClient.return_value.__aenter__.return_value = mock_ctx
        MockClient.return_value.__aexit__.return_value = None

        client = TestClient(_make_app(), follow_redirects=False)
        client.cookies.set(STATE_COOKIE_NAME, state)
        r = client.get(
            "/auth/google/callback",
            params={"code": "auth-code", "state": state},
        )
    assert r.status_code == 401


def test_callback_email_not_in_allowlist_returns_403(_oauth_config):
    """Signature valid, email_verified, but email isn't in `users`.
    403 with a message that tells the friend to ask Sabya to add them."""
    _init_db_only()  # empty users table
    state, verifier = generate_state(), generate_verifier()
    _seed_oauth_state(state, verifier)

    from backend.auth.google import VerifiedIdentity
    with patch(
        "backend.auth.routes.httpx.AsyncClient"
    ) as MockClient, patch(
        "backend.auth.routes.verify_google_id_token",
        return_value=VerifiedIdentity(
            sub="random-google-sub",
            email="not-on-allowlist@example.com",
        ),
    ):
        mock_ctx = MagicMock()
        async def fake_post(*args, **kwargs):
            return _mock_token_response()
        mock_ctx.post = fake_post
        MockClient.return_value.__aenter__.return_value = mock_ctx
        MockClient.return_value.__aexit__.return_value = None

        client = TestClient(_make_app(), follow_redirects=False)
        client.cookies.set(STATE_COOKIE_NAME, state)
        r = client.get(
            "/auth/google/callback",
            params={"code": "auth-code", "state": state},
        )
    assert r.status_code == 403
    assert "allowlist" in r.json()["detail"].lower()


def test_callback_backfills_oauth_sub_on_first_signin(_oauth_config):
    """A user added by email-only (no oauth_google_sub set) should get
    their sub backfilled on first successful sign-in, so future
    sign-ins short-circuit to get_by_oauth_sub."""
    user = _seed_user(email="preauth@example.com")
    assert user.oauth_google_sub is None  # sanity

    state, verifier = generate_state(), generate_verifier()
    _seed_oauth_state(state, verifier)

    from backend.auth.google import VerifiedIdentity
    with patch(
        "backend.auth.routes.httpx.AsyncClient"
    ) as MockClient, patch(
        "backend.auth.routes.verify_google_id_token",
        return_value=VerifiedIdentity(
            sub="new-google-sub-for-preauth",
            email="preauth@example.com",
        ),
    ):
        mock_ctx = MagicMock()
        async def fake_post(*args, **kwargs):
            return _mock_token_response()
        mock_ctx.post = fake_post
        MockClient.return_value.__aenter__.return_value = mock_ctx
        MockClient.return_value.__aexit__.return_value = None

        client = TestClient(_make_app(), follow_redirects=False)
        client.cookies.set(STATE_COOKIE_NAME, state)
        r = client.get(
            "/auth/google/callback",
            params={"code": "auth-code", "state": state},
        )
    assert r.status_code == 302
    # Verify backfill.

    async def _check():
        async with session_scope() as session:
            u = await UserRepository(session).get(user.id)
            assert u is not None
            assert u.oauth_google_sub == "new-google-sub-for-preauth"

    asyncio.run(_check())


def test_callback_returns_503_when_client_secret_unset(_oauth_config, monkeypatch):
    monkeypatch.setattr(settings, "google_oauth_client_secret", "")
    _init_db_only()
    client = TestClient(_make_app(), follow_redirects=False)
    r = client.get(
        "/auth/google/callback",
        params={"code": "some-code", "state": "some-state"},
    )
    assert r.status_code == 503


# --------------------------------------------------------------------
# /auth/google/callback — CSRF binding, sub-rebind, replay
# --------------------------------------------------------------------

def test_start_sets_state_binding_cookie(_oauth_config):
    """/start must set the state-binding cookie so /callback can prove the
    round-trip belongs to the same browser (login-CSRF defense)."""
    _init_db_only()
    client = TestClient(_make_app(), follow_redirects=False)
    r = client.get("/auth/google/start")
    assert r.status_code == 302
    state_from_url = dict(urllib.parse.parse_qsl(
        urllib.parse.urlparse(r.headers["location"]).query
    ))["state"]
    # The cookie value must equal the state sent to Google.
    assert client.cookies.get(STATE_COOKIE_NAME) == state_from_url


def test_callback_missing_state_cookie_returns_400(_oauth_config):
    """A valid, unconsumed state presented by a browser that never got the
    cookie (the login-CSRF scenario) is rejected before consume_state."""
    state, verifier = generate_state(), generate_verifier()
    _seed_oauth_state(state, verifier)
    client = TestClient(_make_app(), follow_redirects=False)
    # No cookie set — simulates the victim's browser being fed the
    # attacker's (code, state).
    r = client.get(
        "/auth/google/callback",
        params={"code": "auth-code", "state": state},
    )
    assert r.status_code == 400
    assert "start sign-in again" in r.json()["detail"]


def test_callback_mismatched_state_cookie_returns_400(_oauth_config):
    """A cookie that doesn't match the state query param is rejected."""
    state, verifier = generate_state(), generate_verifier()
    _seed_oauth_state(state, verifier)
    client = TestClient(_make_app(), follow_redirects=False)
    client.cookies.set(STATE_COOKIE_NAME, "a-different-state-value")
    r = client.get(
        "/auth/google/callback",
        params={"code": "auth-code", "state": state},
    )
    assert r.status_code == 400


def test_callback_sub_mismatch_returns_403(_oauth_config):
    """An allowlisted row already bound to sub A must reject a sign-in that
    presents the same email with a different sub B (account-rebind guard)."""
    user = _seed_user(email="bound@example.com", oauth_google_sub="existing-sub-A")
    assert user.oauth_google_sub == "existing-sub-A"

    state, verifier = generate_state(), generate_verifier()
    _seed_oauth_state(state, verifier)

    from backend.auth.google import VerifiedIdentity
    with patch(
        "backend.auth.routes.httpx.AsyncClient"
    ) as MockClient, patch(
        "backend.auth.routes.verify_google_id_token",
        return_value=VerifiedIdentity(
            sub="different-sub-B",
            email="bound@example.com",
        ),
    ):
        mock_ctx = MagicMock()
        async def fake_post(*args, **kwargs):
            return _mock_token_response()
        mock_ctx.post = fake_post
        MockClient.return_value.__aenter__.return_value = mock_ctx
        MockClient.return_value.__aexit__.return_value = None

        client = TestClient(_make_app(), follow_redirects=False)
        client.cookies.set(STATE_COOKIE_NAME, state)
        r = client.get(
            "/auth/google/callback",
            params={"code": "auth-code", "state": state},
        )
    assert r.status_code == 403
    assert "different Google identity" in r.json()["detail"]

    # The stored sub must NOT have been overwritten.
    async def _check():
        async with session_scope() as session:
            u = await UserRepository(session).get(user.id)
            assert u is not None
            assert u.oauth_google_sub == "existing-sub-A"

    asyncio.run(_check())


def test_callback_case_insensitive_email_allowlist(_oauth_config):
    """A row seeded with mixed-case email is matched against Google's
    lowercase email (case-insensitive allowlist lookup)."""
    _seed_user(email="Mixed.Case@Example.com")

    state, verifier = generate_state(), generate_verifier()
    _seed_oauth_state(state, verifier)

    from backend.auth.google import VerifiedIdentity
    with patch(
        "backend.auth.routes.httpx.AsyncClient"
    ) as MockClient, patch(
        "backend.auth.routes.verify_google_id_token",
        return_value=VerifiedIdentity(
            sub="sub-for-mixedcase",
            email="mixed.case@example.com",
        ),
    ):
        mock_ctx = MagicMock()
        async def fake_post(*args, **kwargs):
            return _mock_token_response()
        mock_ctx.post = fake_post
        MockClient.return_value.__aenter__.return_value = mock_ctx
        MockClient.return_value.__aexit__.return_value = None

        client = TestClient(_make_app(), follow_redirects=False)
        client.cookies.set(STATE_COOKIE_NAME, state)
        r = client.get(
            "/auth/google/callback",
            params={"code": "auth-code", "state": state},
        )
    assert r.status_code == 302


def test_callback_state_consumed_even_when_exchange_fails(_oauth_config):
    """After a failed token exchange (401), the state must be consumed and
    NOT replayable — the one-shot guarantee must hold on failure paths,
    not just the happy path (anti-replay)."""
    state, verifier = generate_state(), generate_verifier()
    _seed_oauth_state(state, verifier)

    # First attempt: token exchange fails → 401.
    with patch("backend.auth.routes.httpx.AsyncClient") as MockClient:
        mock_ctx = MagicMock()
        async def fake_post(*args, **kwargs):
            return _mock_token_response(status_code=400)
        mock_ctx.post = fake_post
        MockClient.return_value.__aenter__.return_value = mock_ctx
        MockClient.return_value.__aexit__.return_value = None

        client = TestClient(_make_app(), follow_redirects=False)
        client.cookies.set(STATE_COOKIE_NAME, state)
        r1 = client.get(
            "/auth/google/callback",
            params={"code": "bad-code", "state": state},
        )
    assert r1.status_code == 401

    # The oauth_state row must be gone (consumed + committed), so a replay
    # of the same state can't get past consume_state.
    stored = _fetch_stored_states()
    assert stored == [], "state row should be consumed even after a failed exchange"

    # Second attempt with the same state → 400 (already consumed).
    client2 = TestClient(_make_app(), follow_redirects=False)
    client2.cookies.set(STATE_COOKIE_NAME, state)
    r2 = client2.get(
        "/auth/google/callback",
        params={"code": "bad-code", "state": state},
    )
    assert r2.status_code == 400
