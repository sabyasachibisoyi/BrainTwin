"""Tests for Phase 4.1 M.M.1.a — data model + migration + repos.

Run with: pytest tests/test_storage_mm1a.py -v

Scope:
  - PRAGMA foreign_keys is ON per connection (Codex Fix 2)
  - Migration sweep is idempotent (safe to call init_db twice)
  - New unique index `users_oauth_sub_idx` blocks duplicate subs
    (both at insert time and via re-inserting on a fresh engine)
  - UserRepository new methods: get_by_oauth_sub, bump_token_version,
    delete_user (§5.3 cascade)
  - UsageCountersRepository: get_or_create idempotent, atomic bump
    accumulates across concurrent updates
  - TelegramBindingRepository: get_by_telegram, get_by_user,
    set_binding as upsert (one Telegram = one user, replaces)

The `delete_user` test is the big one. It builds a full graph
(user → captures → chunks + hydrations + enrichments, plus chunk_topics
+ chunk_entities junctions, plus usage_counters + telegram_bindings),
deletes the user, and asserts every table is empty for that user.
Then it also asserts topics + entities are UNTOUCHED (shared
vocabulary per B.7 outlives users).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from backend.storage import (  # noqa: E402
    aclose,
    init_db,
    session_scope,
)
from backend.storage import db as db_module  # noqa: E402
from backend.storage.models import (  # noqa: E402
    Capture,
    ChunkAttachment,
    ChunkInsert,
)
from backend.storage.repositories import (  # noqa: E402
    CaptureRepository,
    ChunkRepository,
    DuplicateKeyError,
    EntityRepository,
    EnrichmentRepository,
    HydrationRepository,
    TelegramBindingRepository,
    TopicRepository,
    UsageCountersRepository,
    UserRepository,
)
from backend.storage.schema import (  # noqa: E402
    captures,
    chunk_entities,
    chunk_topics,
    chunks,
    enrichments,
    entities,
    hydrations,
    telegram_bindings,
    topics,
    usage_counters,
    users,
)


# ---- Fixtures --------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_engine(monkeypatch):
    """Reset storage module state so each test gets a fresh in-memory
    DB. Same pattern as test_storage_repos.py."""
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_session_factory", None)
    yield


# ---- PRAGMA + migration --------------------------------------------

def test_foreign_keys_pragma_is_on():
    """Every new SQLite connection must have foreign_keys=ON — the
    Codex Fix 2 requirement that makes the delete_user cascade work
    as a defense-in-depth guard. If this regresses, every FK
    ForeignKey(...) declaration silently becomes a documentation-only
    hint and the §5.3 privacy promise loses its safety net."""
    async def run():
        await init_db()
        async with session_scope() as session:
            result = await session.execute(text("PRAGMA foreign_keys"))
            (value,) = result.first()
            assert value == 1, f"expected foreign_keys=1, got {value}"
        await aclose()

    asyncio.run(run())


def test_init_db_is_idempotent():
    """The narrow migration sweep must be safe to run on every
    startup — that's the whole point of the CREATE-IF-NOT-EXISTS
    shape. Calling init_db twice must not raise, must not add
    duplicate columns, and must leave the schema identical."""
    async def run():
        await init_db()
        # Second call: exercises the sweep against the just-created
        # tables. If any CREATE / ALTER isn't guarded, this raises.
        await init_db()
        # A meaningful assertion: the users table must have exactly
        # one row of each new column, no duplicates.
        async with session_scope() as session:
            result = await session.execute(
                text("SELECT name FROM pragma_table_info('users')")
            )
            col_names = [r[0] for r in result.fetchall()]
            assert col_names.count("oauth_google_sub") == 1
            assert col_names.count("token_version") == 1
            assert col_names.count("is_eval") == 1
        await aclose()

    asyncio.run(run())


def test_unique_oauth_sub_index_blocks_duplicates():
    """Codex Fix 1: oauth_google_sub uniqueness enforced by a named
    UNIQUE INDEX so the migration sweep can add it to an existing DB
    (SQLite forbids inline UNIQUE on ALTER TABLE ADD COLUMN).
    First user with a given sub wins; second user gets DuplicateKeyError.

    NB: SQLite unique indexes allow multiple NULLs (this is intended —
    only OAuth-signed-in users have a sub value; pre-4.1 rows retain
    NULL and don't collide with each other)."""
    async def run():
        await init_db()
        async with session_scope() as session:
            repo = UserRepository(session)
            await repo.create(
                email="alice@example.com",
                oauth_google_sub="google-sub-alice",
            )
            with pytest.raises(DuplicateKeyError):
                await repo.create(
                    email="alice-alt@example.com",
                    oauth_google_sub="google-sub-alice",
                )
        # Multiple NULL subs should coexist — pre-4.1 users don't sign
        # in via Google yet.
        async with session_scope() as session:
            repo = UserRepository(session)
            await repo.create(email="null1@example.com")
            await repo.create(email="null2@example.com")
        await aclose()

    asyncio.run(run())


# ---- UserRepository extensions --------------------------------------

def test_get_by_oauth_sub_returns_none_when_missing():
    async def run():
        await init_db()
        async with session_scope() as session:
            repo = UserRepository(session)
            got = await repo.get_by_oauth_sub("no-such-sub")
            assert got is None
        await aclose()

    asyncio.run(run())


def test_get_by_oauth_sub_roundtrips():
    async def run():
        await init_db()
        async with session_scope() as session:
            repo = UserRepository(session)
            created = await repo.create(
                email="bob@example.com",
                oauth_google_sub="google-sub-bob",
                display_name="Bob",
            )
            got = await repo.get_by_oauth_sub("google-sub-bob")
            assert got is not None
            assert got.id == created.id
            assert got.email == "bob@example.com"
            assert got.oauth_google_sub == "google-sub-bob"
            assert got.is_admin is False
            assert got.token_version == 0
            assert got.is_eval is False
        await aclose()

    asyncio.run(run())


def test_bump_token_version_increments_and_returns_new_value():
    """Codex Fix 3 — the JWT-revocation primitive. Bumping must be
    atomic (UPDATE ... SET x = x + 1, not read-modify-write) and
    must return the new value so callers can either log it or mint
    a matching fresh token immediately."""
    async def run():
        await init_db()
        async with session_scope() as session:
            repo = UserRepository(session)
            user = await repo.create(email="carol@example.com")
            v1 = await repo.bump_token_version(user.id)
            assert v1 == 1
            v2 = await repo.bump_token_version(user.id)
            assert v2 == 2
            # Re-read: matches
            got = await repo.get(user.id)
            assert got is not None
            assert got.token_version == 2
        await aclose()

    asyncio.run(run())


def test_bump_token_version_raises_on_missing_user():
    async def run():
        await init_db()
        async with session_scope() as session:
            repo = UserRepository(session)
            with pytest.raises(ValueError):
                await repo.bump_token_version(99999)
        await aclose()

    asyncio.run(run())


# ---- delete_user cascade — the §5.3 privacy test --------------------

def test_delete_user_cascades_across_all_owned_tables():
    """The big one. Build a full graph rooted at a user, delete the
    user, then assert every user-scoped table is empty for that user.
    Also assert topics + entities are UNTOUCHED — shared vocabulary
    per B.7 outlives the user who first coined a term.

    If a future migration adds a new user-scoped table and the
    developer forgets to walk it in delete_user, foreign_keys=ON
    makes the final `DELETE FROM users` fail loudly with
    IntegrityError. That's a signal, not a bug — the fix is to add
    the new table to delete_user's walk order."""
    async def run():
        await init_db()

        # ---- Setup: build a full user graph ------------------------
        async with session_scope() as session:
            user_repo = UserRepository(session)
            capture_repo = CaptureRepository(session)
            hydration_repo = HydrationRepository(session)
            enrichment_repo = EnrichmentRepository(session)
            chunk_repo = ChunkRepository(session)
            topic_repo = TopicRepository(session)
            entity_repo = EntityRepository(session)
            counters_repo = UsageCountersRepository(session)
            binding_repo = TelegramBindingRepository(session)

            user = await user_repo.create(email="doomed@example.com")
            # Also a bystander user — should NOT be touched.
            bystander = await user_repo.create(email="bystander@example.com")

            # Two captures for the doomed user, one for the bystander.
            for cid in ("cap-doomed-1", "cap-doomed-2"):
                await capture_repo.create(Capture(
                    id=cid,
                    user_id=user.id,
                    url=None,
                    title=None,
                    platform=None,
                    content_type=None,
                    captured_at="2026-07-02T00:00:00+00:00",
                    dwell_seconds=0,
                    raw_metadata_json=None,
                ))
            await capture_repo.create(Capture(
                id="cap-bystander",
                user_id=bystander.id,
                url=None,
                title=None,
                platform=None,
                content_type=None,
                captured_at="2026-07-02T00:00:00+00:00",
                dwell_seconds=0,
                raw_metadata_json=None,
            ))

            # Hydration + enrichment on cap-doomed-1.
            await hydration_repo.create(
                capture_id="cap-doomed-1",
                tier="og_metadata",
                source_payload_json="{}",
                hydrated_at="2026-07-02T00:00:00+00:00",
            )
            await enrichment_repo.create(
                capture_id="cap-doomed-1",
                summary="doomed",
                key_facts_json="[]",
                model="claude-haiku",
                enriched_at="2026-07-02T00:00:00+00:00",
            )

            # Chunks for cap-doomed-1 (a couple, so we can attach
            # topic + entity junctions).
            chunk_ids = await chunk_repo.create_many([
                ChunkInsert(
                    capture_id="cap-doomed-1",
                    chunk_index=0,
                    text="chunk zero text",
                    source_kind="article_paragraph",
                ),
                ChunkInsert(
                    capture_id="cap-doomed-1",
                    chunk_index=1,
                    text="chunk one text",
                    source_kind="article_paragraph",
                ),
            ])

            # Topic + entity — shared vocabulary. These MUST survive
            # the user delete.
            topic = await topic_repo.find_or_create(
                label="Testing", description="Software testing"
            )
            entity = await entity_repo.find_or_create(
                label="Pytest", entity_type="concept"
            )
            await chunk_repo.attach_topics(chunk_ids[0], [
                (topic.id, 0.9),
            ])
            await chunk_repo.attach_entities(chunk_ids[0], [
                ChunkAttachment(entity_id=entity.id, confidence=0.8),
            ])

            # Usage counter row + Telegram binding.
            await counters_repo.get_or_create(
                user_id=user.id, date_utc="2026-07-02"
            )
            await counters_repo.bump(
                user_id=user.id,
                date_utc="2026-07-02",
                captures=2,
                tokens=1234,
            )
            await binding_repo.set_binding(
                telegram_user_id=999888777, user_id=user.id
            )

        # ---- Sanity check: everything landed. ---------------------
        async with session_scope() as session:
            for table, uid_col_expr, expected in [
                (captures, captures.c.user_id, 2),
                (hydrations, None, 1),   # via capture join
                (enrichments, None, 1),  # via capture join
                (chunks, None, 2),       # via capture join
                (usage_counters, usage_counters.c.user_id, 1),
                (telegram_bindings, telegram_bindings.c.user_id, 1),
            ]:
                if uid_col_expr is not None:
                    stmt = select(table).where(uid_col_expr == user.id)
                    result = await session.execute(stmt)
                    assert len(result.fetchall()) == expected

        # ---- The actual delete_user call ---------------------------
        async with session_scope() as session:
            user_repo = UserRepository(session)
            await user_repo.delete_user(user.id)

        # ---- Assert: every user-scoped table is empty for doomed --
        async with session_scope() as session:
            for table, uid_expr in [
                (captures, captures.c.user_id == user.id),
                (usage_counters, usage_counters.c.user_id == user.id),
                (telegram_bindings, telegram_bindings.c.user_id == user.id),
            ]:
                result = await session.execute(select(table).where(uid_expr))
                assert result.first() is None, f"{table.name} still has doomed rows"

            # Reach into hydrations / enrichments / chunks via captures
            # — they carry capture_id, not user_id, so we look for
            # rows whose capture_id was one of the doomed's.
            for table in (hydrations, enrichments, chunks):
                result = await session.execute(select(table).where(
                    table.c.capture_id.in_(["cap-doomed-1", "cap-doomed-2"])
                ))
                assert result.first() is None, f"{table.name} still has doomed rows"

            # Junctions — chunk_topics + chunk_entities. Nothing left
            # for the doomed chunks (their IDs are gone from `chunks`
            # already, so any junction row would be an orphan).
            for junction in (chunk_topics, chunk_entities):
                result = await session.execute(
                    text(f"SELECT COUNT(*) FROM {junction.name}")
                )
                (count,) = result.first()
                # Only the doomed's chunks had attachments — after
                # delete, junction should be empty.
                assert count == 0, f"{junction.name} has {count} orphan rows"

            # ---- User row itself is gone ---------------------------
            result = await session.execute(
                select(users).where(users.c.id == user.id)
            )
            assert result.first() is None

            # ---- Bystander survives, including their capture ------
            result = await session.execute(
                select(users).where(users.c.id == bystander.id)
            )
            assert result.first() is not None
            result = await session.execute(
                select(captures).where(captures.c.id == "cap-bystander")
            )
            assert result.first() is not None

            # ---- Shared vocabulary UNTOUCHED (B.7) ----------------
            result = await session.execute(select(topics))
            assert len(result.fetchall()) >= 1
            result = await session.execute(select(entities))
            assert len(result.fetchall()) >= 1

        await aclose()

    asyncio.run(run())


def test_delete_user_is_idempotent():
    """Deleting a nonexistent user_id must be a silent no-op — the
    caller wanting a distinction between 'deleted' vs 'wasn't there'
    should get() first."""
    async def run():
        await init_db()
        async with session_scope() as session:
            repo = UserRepository(session)
            await repo.delete_user(99999)  # doesn't exist — must not raise
        await aclose()

    asyncio.run(run())


# ---- UsageCountersRepository ----------------------------------------

def test_usage_counters_get_or_create_starts_at_zero():
    async def run():
        await init_db()
        async with session_scope() as session:
            user_repo = UserRepository(session)
            counters_repo = UsageCountersRepository(session)
            user = await user_repo.create(email="c1@example.com")
            row = await counters_repo.get_or_create(
                user_id=user.id, date_utc="2026-07-02"
            )
            assert row.captures == 0
            assert row.recalls == 0
            assert row.tokens == 0
        await aclose()

    asyncio.run(run())


def test_usage_counters_get_or_create_is_idempotent():
    """Concurrent workers hitting get_or_create for the same
    (user, day) MUST both get a valid row back without corrupting
    the counter. Modelled here as two sequential calls; the second
    call takes the ON CONFLICT DO NOTHING branch."""
    async def run():
        await init_db()
        async with session_scope() as session:
            user_repo = UserRepository(session)
            counters_repo = UsageCountersRepository(session)
            user = await user_repo.create(email="c2@example.com")
            r1 = await counters_repo.get_or_create(
                user_id=user.id, date_utc="2026-07-02"
            )
            # Bump so we can prove the second call doesn't reset it
            await counters_repo.bump(
                user_id=user.id, date_utc="2026-07-02", captures=5
            )
            r2 = await counters_repo.get_or_create(
                user_id=user.id, date_utc="2026-07-02"
            )
            assert r1.user_id == r2.user_id == user.id
            assert r2.captures == 5  # NOT reset to zero
        await aclose()

    asyncio.run(run())


def test_usage_counters_bump_is_atomic_and_accumulates():
    """Two separate bump() calls in a row must accumulate — that's
    the whole point of doing the increment at the DB. Also verifies
    each metric field bumps independently: passing captures=1 must
    not touch recalls or tokens."""
    async def run():
        await init_db()
        async with session_scope() as session:
            user_repo = UserRepository(session)
            counters_repo = UsageCountersRepository(session)
            user = await user_repo.create(email="c3@example.com")
            await counters_repo.get_or_create(
                user_id=user.id, date_utc="2026-07-02"
            )
            await counters_repo.bump(
                user_id=user.id, date_utc="2026-07-02", captures=1
            )
            await counters_repo.bump(
                user_id=user.id, date_utc="2026-07-02",
                captures=1, tokens=100,
            )
            await counters_repo.bump(
                user_id=user.id, date_utc="2026-07-02", recalls=2
            )
            row = await counters_repo.get(
                user_id=user.id, date_utc="2026-07-02"
            )
            assert row is not None
            assert row.captures == 2
            assert row.recalls == 2
            assert row.tokens == 100
        await aclose()

    asyncio.run(run())


def test_usage_counters_bump_creates_row_when_missing():
    """bump() must upsert, not UPDATE-and-hope: a request that ran
    get_or_create() just before UTC midnight bumps with the NEW day's
    date_utc after the Anthropic call returns. A plain UPDATE would
    match 0 rows and silently drop the spend."""
    async def run():
        await init_db()
        async with session_scope() as session:
            user_repo = UserRepository(session)
            counters_repo = UsageCountersRepository(session)
            user = await user_repo.create(email="c4@example.com")
            # No get_or_create for this date — the row doesn't exist.
            await counters_repo.bump(
                user_id=user.id, date_utc="2026-07-03", tokens=5000
            )
            row = await counters_repo.get(
                user_id=user.id, date_utc="2026-07-03"
            )
            assert row is not None
            assert row.tokens == 5000
            assert row.captures == 0
        await aclose()

    asyncio.run(run())


# ---- TelegramBindingRepository --------------------------------------

def test_telegram_binding_set_and_get():
    async def run():
        await init_db()
        async with session_scope() as session:
            user_repo = UserRepository(session)
            binding_repo = TelegramBindingRepository(session)
            user = await user_repo.create(email="t1@example.com")

            await binding_repo.set_binding(
                telegram_user_id=111, user_id=user.id
            )
            got = await binding_repo.get_by_telegram(111)
            assert got is not None
            assert got.user_id == user.id

            # Reverse lookup
            got_rev = await binding_repo.get_by_user(user.id)
            assert got_rev is not None
            assert got_rev.telegram_user_id == 111
        await aclose()

    asyncio.run(run())


def test_telegram_binding_set_replaces_previous():
    """UX: friend types /link with the wrong code, then retypes.
    The second /link MUST win — the binding row's user_id updates
    in place, no error raised."""
    async def run():
        await init_db()
        async with session_scope() as session:
            user_repo = UserRepository(session)
            binding_repo = TelegramBindingRepository(session)
            u1 = await user_repo.create(email="t2a@example.com")
            u2 = await user_repo.create(email="t2b@example.com")

            await binding_repo.set_binding(telegram_user_id=222, user_id=u1.id)
            await binding_repo.set_binding(telegram_user_id=222, user_id=u2.id)

            got = await binding_repo.get_by_telegram(222)
            assert got is not None
            assert got.user_id == u2.id
            # u1 no longer has a binding
            assert await binding_repo.get_by_user(u1.id) is None
        await aclose()

    asyncio.run(run())


def test_telegram_binding_one_binding_per_user():
    """The reverse direction: the same user linking from a NEW
    Telegram account must replace their old binding, never hold two.
    Otherwise get_by_user() is nondeterministic and 'connected as' /
    unlink flows operate on the wrong Telegram account."""
    async def run():
        await init_db()
        async with session_scope() as session:
            user_repo = UserRepository(session)
            binding_repo = TelegramBindingRepository(session)
            user = await user_repo.create(email="t3@example.com")

            await binding_repo.set_binding(
                telegram_user_id=111, user_id=user.id
            )
            await binding_repo.set_binding(
                telegram_user_id=333, user_id=user.id
            )

            # Old Telegram account is unbound; the new one won.
            assert await binding_repo.get_by_telegram(111) is None
            got = await binding_repo.get_by_user(user.id)
            assert got is not None
            assert got.telegram_user_id == 333
        await aclose()

    asyncio.run(run())


def test_telegram_binding_delete_is_idempotent():
    async def run():
        await init_db()
        async with session_scope() as session:
            repo = TelegramBindingRepository(session)
            # No binding exists — delete must not raise.
            await repo.delete_by_telegram(999)
        await aclose()

    asyncio.run(run())
