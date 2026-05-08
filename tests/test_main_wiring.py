"""Tests for backend/main.py — Phase 3 Step 4b startup wiring.

Run with: pytest tests/test_main_wiring.py -v

Covers:
  - _ensure_default_user is idempotent (second call is a no-op).
  - _startup's two storage try-blocks are independent: init failure
    doesn't suppress user-seed, and user-seed failure doesn't undo
    init.
  - storage_dual_write=False short-circuits the entire startup
    storage block (no init, no seed).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# In-memory SQLite for the whole file.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

# Importing backend.main runs FastAPI app construction + middleware
# config — fine, side-effect-free past that point. Need to set up
# DATABASE_URL first so any module-level engine init uses the test URL.
from backend import main as main_mod  # noqa: E402
from backend.storage import (  # noqa: E402
    DEFAULT_USER_ID,
    UserRepository,
    init_db,
    session_scope,
)
from backend.storage import db as db_module  # noqa: E402


# ---- Fixtures --------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_engine(monkeypatch):
    """Fresh in-memory DB per test. Mirrors the pattern in
    test_storage_sync.py."""
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_session_factory", None)
    yield


@pytest.fixture
def dual_write_on(monkeypatch):
    """Force storage_dual_write=True (default is True but tests may
    have other monkeypatches in play)."""
    monkeypatch.setattr(main_mod.settings, "storage_dual_write", True)


@pytest.fixture
def no_anthropic(monkeypatch):
    """Empty API key so _startup returns after the storage block —
    we don't want to exercise the LLM init / recovery path here."""
    monkeypatch.setattr(main_mod.settings, "anthropic_api_key", "")


# ---- _ensure_default_user --------------------------------------------

class TestEnsureDefaultUser:
    def test_idempotent(self, dual_write_on):
        """Second call must NOT insert a duplicate row, must NOT raise.
        Required because _startup runs on every boot and would otherwise
        crash on the second app start once a user exists. Stronger
        version of the contract: exactly one users row after two calls."""
        from sqlalchemy import func, select
        from backend.storage.schema import users

        async def go():
            await init_db()
            await main_mod._ensure_default_user()
            await main_mod._ensure_default_user()  # second call
            async with session_scope() as session:
                user = await UserRepository(session).get(DEFAULT_USER_ID)
                count_result = await session.execute(
                    select(func.count()).select_from(users)
                )
                count = int(count_result.scalar_one())
            return user, count

        user, count = asyncio.run(go())
        assert user is not None
        assert user.id == DEFAULT_USER_ID
        assert count == 1

# ---- _startup independence of try-blocks -----------------------------

class TestStartupIndependence:
    def test_user_seed_failure_does_not_undo_init(
        self, dual_write_on, no_anthropic, monkeypatch,
    ):
        """If _ensure_default_user fails (e.g. transient DB hiccup),
        the schema init MUST still have completed — we want the
        recovery on next boot to find the schema and just seed the
        user, not redo everything from scratch."""
        async def boom() -> None:
            raise RuntimeError("simulated user-seed failure")

        monkeypatch.setattr(main_mod, "_ensure_default_user", boom)
        # Run startup. Must NOT raise — the half-on case is handled
        # by independent try/except blocks.
        asyncio.run(main_mod._startup())

        # Schema tables exist. We verify by running a query that would
        # fail with "no such table" if init didn't run.
        from sqlalchemy import select
        from backend.storage.schema import users

        async def check():
            async with session_scope() as session:
                result = await session.execute(select(users))
                return result.fetchall()

        rows = asyncio.run(check())
        assert rows == []  # Schema present, no rows because seed failed.

    def test_init_failure_still_attempts_user_seed(
        self, dual_write_on, no_anthropic, monkeypatch,
    ):
        """If init_storage_db fails, _ensure_default_user must still
        be ATTEMPTED (and likely fail too on missing tables — but the
        independence is what we test here, not the cascading outcome).

        Verifies the two try-blocks aren't fused into one (the bug
        fix's whole point)."""
        seed_called = {"v": False}

        async def boom_init() -> None:
            raise RuntimeError("simulated init failure")

        async def record_seed() -> None:
            seed_called["v"] = True
            # Don't raise — we just want to confirm we got here even
            # though init blew up first.

        monkeypatch.setattr(main_mod, "init_storage_db", boom_init)
        monkeypatch.setattr(main_mod, "_ensure_default_user", record_seed)

        asyncio.run(main_mod._startup())  # must not raise

        assert seed_called["v"] is True, (
            "user-seed was not attempted after init failure — try blocks "
            "are still fused into one"
        )


# ---- storage_dual_write=False short-circuit --------------------------

class TestDualWriteOffStartup:
    def test_dual_write_off_skips_storage_block(
        self, no_anthropic, monkeypatch,
    ):
        """When storage_dual_write=False the operator is signalling
        'don't touch SQL this run'. The startup hook should respect
        that — no schema init, no user seed."""
        init_called = {"v": False}
        seed_called = {"v": False}

        async def record_init() -> None:
            init_called["v"] = True

        async def record_seed() -> None:
            seed_called["v"] = True

        monkeypatch.setattr(main_mod.settings, "storage_dual_write", False)
        monkeypatch.setattr(main_mod, "init_storage_db", record_init)
        monkeypatch.setattr(main_mod, "_ensure_default_user", record_seed)

        asyncio.run(main_mod._startup())

        assert init_called["v"] is False, (
            "init_storage_db ran despite storage_dual_write=False"
        )
        assert seed_called["v"] is False, (
            "_ensure_default_user ran despite storage_dual_write=False"
        )
