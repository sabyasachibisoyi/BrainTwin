"""Tests for backend/auth/pkce.py — Phase 4.1 M.M.1.c.

Two axes:
  1. Pure crypto helpers (generate_verifier/state, derive_challenge)
     — deterministic-given-input, correct RFC 7636 shape.
  2. oauth_state table ops — round-trip store→consume, one-shot
     delete-on-read, expiry handling, sweep behavior.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy import insert, select  # noqa: E402

from backend.auth.pkce import (  # noqa: E402
    consume_state,
    derive_challenge,
    generate_state,
    generate_verifier,
    store_state,
    sweep_expired,
)
from backend.storage import aclose, init_db, session_scope  # noqa: E402
from backend.storage import db as db_module  # noqa: E402
from backend.storage.schema import oauth_state  # noqa: E402


# Base64url alphabet (RFC 4648 §5), no padding.
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# ---- Fixture (reused pattern from test_storage_mm1a.py) -------------

@pytest.fixture(autouse=True)
def clean_engine(monkeypatch):
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_session_factory", None)
    yield


# ---- Pure crypto helpers --------------------------------------------

def test_generate_verifier_is_base64url_length_43():
    """RFC 7636 requires 43-128 chars; our 32-byte input → 43 chars."""
    v = generate_verifier()
    assert len(v) == 43
    assert _B64URL_RE.match(v), f"not base64url: {v!r}"


def test_generate_verifier_is_random():
    """CSPRNG source → two calls back-to-back must never collide."""
    assert generate_verifier() != generate_verifier()


def test_derive_challenge_is_deterministic():
    """derive_challenge(v) must be a pure function of v — Google
    recomputes it server-side using the same formula, so any variation
    at decode-time breaks the exchange."""
    v = generate_verifier()
    assert derive_challenge(v) == derive_challenge(v)


def test_derive_challenge_matches_sha256(monkeypatch):
    """S256 method spec: challenge = base64url(SHA256(verifier))."""
    v = "test-verifier-1234567890"
    expected = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    assert derive_challenge(v) == expected


def test_generate_state_shape_matches_verifier():
    """Same generator underneath — 43 base64url chars."""
    s = generate_state()
    assert len(s) == 43
    assert _B64URL_RE.match(s)


def test_generate_state_and_verifier_are_independent():
    """Different call sites, no shared internal state — mixing them up
    should never collide."""
    assert generate_state() != generate_verifier()


# ---- oauth_state table ops ------------------------------------------

def test_store_and_consume_roundtrip():
    """Happy path: store → consume returns the exact verifier."""
    async def run():
        await init_db()
        state = generate_state()
        verifier = generate_verifier()
        async with session_scope() as session:
            await store_state(session, state=state, code_verifier=verifier)
        # Fresh session — proves we persisted, not just held in memory.
        async with session_scope() as session:
            got = await consume_state(session, state)
            assert got == verifier
        await aclose()

    asyncio.run(run())


def test_consume_state_deletes_row_on_read():
    """One-shot: second consume returns None (row is gone), which is
    the anti-replay guarantee."""
    async def run():
        await init_db()
        state, verifier = generate_state(), generate_verifier()
        async with session_scope() as session:
            await store_state(session, state=state, code_verifier=verifier)
        async with session_scope() as session:
            assert await consume_state(session, state) == verifier
        async with session_scope() as session:
            assert await consume_state(session, state) is None
        await aclose()

    asyncio.run(run())


def test_consume_state_returns_none_when_never_stored():
    async def run():
        await init_db()
        async with session_scope() as session:
            assert await consume_state(session, "never-issued-state") is None
        await aclose()

    asyncio.run(run())


def test_consume_state_returns_none_when_expired_and_deletes_row():
    """Expired state MUST NOT hand back the verifier (attacker window)
    AND the row should be removed on the read so the table stays trim
    even without a sweep having caught it."""
    async def run():
        await init_db()
        state, verifier = generate_state(), generate_verifier()
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        # Hand-insert with a past expires_at to force the expired path.
        async with session_scope() as session:
            await session.execute(
                insert(oauth_state).values(
                    state=state,
                    code_verifier=verifier,
                    created_at=(
                        datetime.now(timezone.utc) - timedelta(minutes=15)
                    ).isoformat(),
                    expires_at=past,
                )
            )
        async with session_scope() as session:
            assert await consume_state(session, state) is None
        # Row should be gone even though we treated it as "expired".
        async with session_scope() as session:
            result = await session.execute(
                select(oauth_state).where(oauth_state.c.state == state)
            )
            assert result.first() is None
        await aclose()

    asyncio.run(run())


def test_store_state_reaps_expired_rows():
    """store_state is the table's ONLY growth path and /auth/google/start
    is unauthenticated, so it sweeps expired rows on every call — table
    size stays bounded by construction, with no external nightly task
    to forget. Regression lock: abandoned sign-ins used to accumulate
    forever."""
    async def run():
        await init_db()
        now = datetime.now(timezone.utc)
        stale_state = generate_state()
        async with session_scope() as session:
            await session.execute(
                insert(oauth_state).values(
                    state=stale_state,
                    code_verifier=generate_verifier(),
                    created_at=(now - timedelta(minutes=30)).isoformat(),
                    expires_at=(now - timedelta(minutes=10)).isoformat(),
                )
            )
        async with session_scope() as session:
            await store_state(
                session,
                state=generate_state(),
                code_verifier=generate_verifier(),
            )
        async with session_scope() as session:
            rows = (await session.execute(select(oauth_state))).fetchall()
            # The stale row was reaped by store_state; only the fresh
            # attempt remains.
            assert len(rows) == 1
            assert rows[0].state != stale_state
        await aclose()

    asyncio.run(run())


def test_sweep_expired_deletes_only_expired_rows():
    """Sweep must leave the still-valid rows alone. Insert one of each
    and confirm."""
    async def run():
        await init_db()
        now = datetime.now(timezone.utc)
        fresh_state = generate_state()
        stale_state = generate_state()
        async with session_scope() as session:
            await session.execute(
                insert(oauth_state).values(
                    state=fresh_state,
                    code_verifier=generate_verifier(),
                    created_at=now.isoformat(),
                    expires_at=(now + timedelta(minutes=5)).isoformat(),
                )
            )
            await session.execute(
                insert(oauth_state).values(
                    state=stale_state,
                    code_verifier=generate_verifier(),
                    created_at=(now - timedelta(minutes=30)).isoformat(),
                    expires_at=(now - timedelta(minutes=10)).isoformat(),
                )
            )
        async with session_scope() as session:
            deleted = await sweep_expired(session)
            assert deleted == 1
        async with session_scope() as session:
            surviving = (await session.execute(select(oauth_state))).fetchall()
            assert len(surviving) == 1
            assert surviving[0].state == fresh_state
        await aclose()

    asyncio.run(run())
