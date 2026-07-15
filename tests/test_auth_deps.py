"""Tests for backend/auth/deps.py — Phase 4.1 M.M.1.c.

Wires a tiny FastAPI app with one route that Depends(get_current_user)
and asserts every failure path returns the expected status + body:

  200 — happy path with valid JWT for existing user
  401 — no Authorization header
  401 — Authorization header not Bearer scheme
  401 — Bearer but no token
  401 — expired JWT
  401 — invalid signature
  401 — malformed token
  401 — sub claim not parseable as int
  401 — tv claim doesn't match DB (revocation)
  403 — user_id in JWT no longer exists in DB

Uses the same in-memory-SQLite fixture pattern as
test_storage_mm1a.py + test_auth_pkce.py.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt as pyjwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from backend.auth.deps import get_current_user  # noqa: E402
from backend.auth.jwt import mint_user_jwt  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.storage import aclose, init_db, session_scope  # noqa: E402
from backend.storage import db as db_module  # noqa: E402
from backend.storage.models import User  # noqa: E402
from backend.storage.repositories import UserRepository  # noqa: E402


# ---- App factory + fixtures ----------------------------------------

def _make_app() -> FastAPI:
    """One-route test app: /whoami echoes the authenticated user's id."""
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user: User = Depends(get_current_user)):
        return {"id": user.id, "email": user.email, "tv": user.token_version}

    return app


@pytest.fixture
def _jwt_secret(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", "test-secret-deps-32-chars-min-ok")
    yield "test-secret-deps-32-chars-min-ok"


@pytest.fixture(autouse=True)
def _clean_engine(monkeypatch):
    """Reset DB engine per test — matches the pattern in
    test_storage_mm1a.py so each test gets its own in-memory SQLite."""
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_session_factory", None)
    yield
    # Best-effort cleanup — the engine may already be disposed by the
    # session_scope in the last request; that's fine.
    try:
        asyncio.run(aclose())
    except Exception:
        pass


def _seed_user(email: str = "friend@example.com") -> User:
    """Create + return a user in the in-memory DB. Runs its own event
    loop; called synchronously from test bodies (which are sync so
    TestClient can drive them)."""

    async def run() -> User:
        await init_db()
        async with session_scope() as session:
            repo = UserRepository(session)
            return await repo.create(email=email)

    return asyncio.run(run())


def _bump_tv(user_id: int) -> int:
    """Bump the user's token_version — used by the revocation test."""

    async def run() -> int:
        async with session_scope() as session:
            repo = UserRepository(session)
            return await repo.bump_token_version(user_id)

    return asyncio.run(run())


def _delete_user(user_id: int) -> None:
    """Delete the user — used by the "user vanished" test."""

    async def run() -> None:
        async with session_scope() as session:
            repo = UserRepository(session)
            await repo.delete_user(user_id)

    asyncio.run(run())


# ---- Happy path -----------------------------------------------------

def test_happy_path_returns_user(_jwt_secret):
    user = _seed_user()
    token = mint_user_jwt(user)
    client = TestClient(_make_app())
    r = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == user.id
    assert body["email"] == user.email
    assert body["tv"] == user.token_version


# ---- 401 — missing / malformed Authorization header -----------------

def test_missing_authorization_header_returns_401(_jwt_secret):
    """Codex Fix 5 lock: Header(None) MUST 401, not 422. If a future
    change to `Annotated[str | None, Header(...)]` slips in, FastAPI
    starts returning 422 and every client library gets confused."""
    _seed_user()
    client = TestClient(_make_app())
    r = client.get("/whoami")
    assert r.status_code == 401
    assert "bearer" in r.json()["detail"].lower()
    # RFC 7235 requires this on a 401 response for a Bearer-scheme resource.
    assert r.headers.get("www-authenticate") == "Bearer"


def test_wrong_scheme_returns_401(_jwt_secret):
    _seed_user()
    client = TestClient(_make_app())
    r = client.get("/whoami", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert r.status_code == 401


def test_bearer_without_token_returns_401(_jwt_secret):
    _seed_user()
    client = TestClient(_make_app())
    r = client.get("/whoami", headers={"Authorization": "Bearer "})
    assert r.status_code == 401


# ---- 401 — token failures --------------------------------------------

def test_expired_token_returns_401(_jwt_secret):
    user = _seed_user()
    token = mint_user_jwt(user, ttl_minutes=-1)
    time.sleep(0.01)
    client = TestClient(_make_app())
    r = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert "expired" in r.json()["detail"].lower()


def test_invalid_signature_returns_401(_jwt_secret, monkeypatch):
    user = _seed_user()
    token = mint_user_jwt(user)
    # Rotate the secret so the token's signature no longer verifies.
    monkeypatch.setattr(settings, "jwt_secret", "different-secret-32-chars-min-ok")
    client = TestClient(_make_app())
    r = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_malformed_token_returns_401(_jwt_secret):
    _seed_user()
    client = TestClient(_make_app())
    r = client.get("/whoami", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401


def test_sub_not_int_returns_401(_jwt_secret):
    """Craft a token where `sub` isn't parseable as int (e.g. a UUID
    fat-fingered in). decode_jwt passes it (it's a string), but
    get_current_user's int() conversion fails and we 401."""
    _seed_user()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "not-an-int",
        "email": "friend@example.com",
        "tv": 0,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    token = pyjwt.encode(payload, _jwt_secret, algorithm="HS256")
    client = TestClient(_make_app())
    r = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert "sub is not an int" in r.json()["detail"]


# ---- 401 — revocation via token_version bump ------------------------

def test_tv_mismatch_returns_401(_jwt_secret):
    """Codex Fix 3 in action: mint a JWT, bump the user's
    token_version, verify the JWT is now rejected. This is THE
    revocation primitive — if it regresses, we lose the ability to
    invalidate live JWTs before their exp."""
    user = _seed_user()
    token = mint_user_jwt(user)  # tv=0 at mint
    _bump_tv(user.id)             # DB tv becomes 1
    client = TestClient(_make_app())
    r = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert "revoked" in r.json()["detail"].lower()


# ---- 403 — user vanished --------------------------------------------

def test_deleted_user_returns_403(_jwt_secret):
    """Token decodes, tv matches (we haven't bumped it), but the user
    row is gone. That's a "you're forbidden from acting on behalf of
    this deleted account" situation, not "your credentials are wrong"
    — 403, not 401. Signing in again with the same account wouldn't
    help (there IS no account); the friend would need to be re-added
    to the allowlist by an admin."""
    user = _seed_user()
    token = mint_user_jwt(user)
    _delete_user(user.id)
    client = TestClient(_make_app())
    r = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    assert "user not found" in r.json()["detail"].lower()


# ---- 503 — auth misconfigured ----------------------------------------

def test_unset_secret_returns_503_not_500(monkeypatch):
    """Unset JWT_SECRET is a CONFIG bug and must surface as the same
    deliberate fail-closed 503 that bearer.py emits — loud and distinct
    from both 401 (credential) and 500 (code bug). Regression lock: the
    RuntimeError from _get_secret used to escape get_current_user
    uncaught and turn into a generic 500."""
    monkeypatch.setattr(settings, "jwt_secret", "")
    _seed_user()
    client = TestClient(_make_app())
    r = client.get("/whoami", headers={"Authorization": "Bearer whatever"})
    assert r.status_code == 503
    assert r.json()["detail"] == "auth not configured"


def test_tv_not_int_returns_401(_jwt_secret):
    """A signed token whose tv claim isn't numeric must 401 like every
    other malformed-claim case — not crash with an uncaught ValueError
    (500). Same trust boundary as the sub-not-int guard."""
    user = _seed_user()
    now = int(time.time())
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "tv": "not-a-number",
        "iat": now,
        "exp": now + 600,
    }
    token = pyjwt.encode(payload, _jwt_secret, algorithm="HS256")
    client = TestClient(_make_app())
    r = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert "tv is not an int" in r.json()["detail"]
