"""Tests for backend/storage/repositories/ — Phase 3 Step 1b.

Run with: pytest tests/test_storage_repos.py -v

Covers:
  - User: create, get, get_by_email, duplicate-email error
  - Capture: create, get with tenant check, list_by_user pagination,
    count_by_user, exists() ignores tenant
  - Hydration: create + list_by_capture (tenant-checked via JOIN)
  - Enrichment: create, get_by_capture, enriched_capture_ids,
    list_by_user with outer-joined captures
  - Chunk: create_many, list_by_capture, get_by_ids, attach_topics
    (idempotent), attach_entities (idempotent)
  - Topic / Entity: find_or_create reuses existing slugs;
    normalize_slug behaves on weird inputs; tenant violations
    return None / empty (no leak)

All tests run against `sqlite:///:memory:` so they don't touch the
real `data/braintwin.db`. The `clean_engine` fixture matches the
pattern in test_storage_schema.py.
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
    TopicRepository,
    UserRepository,
    normalize_slug,
)


# ---- Fixtures --------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_engine(monkeypatch):
    """Reset storage module state so each test gets a fresh in-memory
    DB. Identical to the fixture in test_storage_schema.py."""
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_session_factory", None)
    yield


def _make_capture(
    *,
    capture_id: str = "cap-001",
    user_id: int = 1,
    title: str = "An Article",
    captured_at: str = "2026-05-06T00:00:00+00:00",
) -> Capture:
    return Capture(
        id=capture_id,
        user_id=user_id,
        url="https://example.com/x",
        title=title,
        platform="general",
        content_type="article",
        captured_at=captured_at,
        dwell_seconds=42,
        raw_metadata_json=None,
    )


async def _seed_two_users() -> None:
    """Create users 1 and 2 so cross-tenant tests have a target."""
    await init_db()
    async with session_scope() as session:
        users_repo = UserRepository(session)
        await users_repo.create(
            email="sabya@example.com", display_name="Sabya", user_id=1,
        )
        await users_repo.create(
            email="other@example.com", display_name="Other", user_id=2,
        )


# ---- normalize_slug --------------------------------------------------

class TestNormalizeSlug:
    @pytest.mark.parametrize("raw,expected", [
        ("Kanban", "kanban"),
        ("Kanban Method", "kanban-method"),
        ("  Spaced  Out  ", "spaced-out"),
        ("ML / AI", "ml-ai"),
        ("UPPER_CASE", "uppercase"),  # underscore stripped (not alnum or hyphen)
        ("multiple   spaces", "multiple-spaces"),
        ("---hyphens---", "hyphens"),
        ("a" * 100, "a" * 64),
    ])
    def test_normalize(self, raw, expected):
        assert normalize_slug(raw) == expected


# ---- UserRepository --------------------------------------------------

class TestUserRepository:
    def test_create_and_get(self):
        async def go():
            await init_db()
            async with session_scope() as session:
                repo = UserRepository(session)
                user = await repo.create(
                    email="sabya@example.com",
                    display_name="Sabya",
                    user_id=1,
                )
            async with session_scope() as session:
                repo = UserRepository(session)
                fetched = await repo.get(1)
                by_email = await repo.get_by_email("sabya@example.com")
                missing = await repo.get(999)
                missing_by_email = await repo.get_by_email("nope@example.com")
            await aclose()
            return user, fetched, by_email, missing, missing_by_email

        user, fetched, by_email, missing, missing_by_email = asyncio.run(go())
        assert user.id == 1 and user.email == "sabya@example.com"
        assert fetched is not None and fetched.id == 1
        assert by_email is not None and by_email.id == 1
        assert missing is None
        assert missing_by_email is None

    def test_duplicate_email_raises(self):
        async def go():
            await init_db()
            async with session_scope() as session:
                repo = UserRepository(session)
                await repo.create(email="x@example.com")
            with pytest.raises(DuplicateKeyError):
                async with session_scope() as session:
                    repo = UserRepository(session)
                    await repo.create(email="x@example.com")  # collision
            await aclose()
        asyncio.run(go())


# ---- CaptureRepository ----------------------------------------------

class TestCaptureRepository:
    def test_round_trip(self):
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                repo = CaptureRepository(session)
                await repo.create(_make_capture(capture_id="cap-1", user_id=1))
            async with session_scope() as session:
                repo = CaptureRepository(session)
                cap = await repo.get("cap-1", user_id=1)
                count = await repo.count_by_user(user_id=1)
            await aclose()
            return cap, count

        cap, count = asyncio.run(go())
        assert cap is not None and cap.id == "cap-1"
        assert count == 1

    def test_tenant_isolation(self):
        """A capture owned by user 1 must NOT be visible to user 2."""
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                repo = CaptureRepository(session)
                await repo.create(_make_capture(capture_id="cap-1", user_id=1))
            async with session_scope() as session:
                repo = CaptureRepository(session)
                # Right tenant
                ok = await repo.get("cap-1", user_id=1)
                # Wrong tenant — must return None, NOT leak existence
                leak = await repo.get("cap-1", user_id=2)
                # User 2 sees zero captures
                count_other = await repo.count_by_user(user_id=2)
            await aclose()
            return ok, leak, count_other

        ok, leak, count_other = asyncio.run(go())
        assert ok is not None
        assert leak is None
        assert count_other == 0

    def test_exists_ignores_tenant(self):
        """exists() is the migration-only escape hatch — no tenant check."""
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                repo = CaptureRepository(session)
                await repo.create(_make_capture(capture_id="cap-1", user_id=1))
            async with session_scope() as session:
                repo = CaptureRepository(session)
                yes = await repo.exists("cap-1")
                no = await repo.exists("cap-missing")
            await aclose()
            return yes, no

        yes, no = asyncio.run(go())
        assert yes is True
        assert no is False

    def test_list_by_user_orders_newest_first(self):
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                repo = CaptureRepository(session)
                await repo.create(_make_capture(
                    capture_id="cap-old", user_id=1,
                    captured_at="2026-05-05T10:00:00+00:00",
                ))
                await repo.create(_make_capture(
                    capture_id="cap-new", user_id=1,
                    captured_at="2026-05-06T10:00:00+00:00",
                ))
                await repo.create(_make_capture(
                    capture_id="cap-other-user", user_id=2,
                    captured_at="2026-05-06T11:00:00+00:00",
                ))
            async with session_scope() as session:
                repo = CaptureRepository(session)
                rows = await repo.list_by_user(user_id=1, limit=10)
            await aclose()
            return rows

        rows = asyncio.run(go())
        assert [c.id for c in rows] == ["cap-new", "cap-old"]


# ---- HydrationRepository --------------------------------------------

class TestHydrationRepository:
    def test_create_and_list(self):
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                cap_repo = CaptureRepository(session)
                hyd_repo = HydrationRepository(session)
                await cap_repo.create(_make_capture(capture_id="cap-1", user_id=1))
                await hyd_repo.create(
                    capture_id="cap-1",
                    tier="og_metadata",
                    source_payload_json='{"og": {"source": "og"}}',
                    hydrated_at="2026-05-06T00:00:01+00:00",
                )
                await hyd_repo.create(
                    capture_id="cap-1",
                    tier="video_transcript",
                    source_payload_json='{"transcript": {"chars": 1247}}',
                    hydrated_at="2026-05-06T00:00:02+00:00",
                )
            async with session_scope() as session:
                repo = HydrationRepository(session)
                rows = await repo.list_by_capture("cap-1", user_id=1)
                # Wrong tenant returns empty
                leak = await repo.list_by_capture("cap-1", user_id=2)
            await aclose()
            return rows, leak

        rows, leak = asyncio.run(go())
        assert len(rows) == 2
        assert {r.tier for r in rows} == {"og_metadata", "video_transcript"}
        assert leak == []


# ---- EnrichmentRepository -------------------------------------------

class TestEnrichmentRepository:
    def test_create_and_get(self):
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                cap_repo = CaptureRepository(session)
                enr_repo = EnrichmentRepository(session)
                await cap_repo.create(_make_capture(capture_id="cap-1", user_id=1))
                await enr_repo.create(
                    capture_id="cap-1",
                    summary="Short summary.",
                    key_facts_json='["fact one", "fact two"]',
                    model="claude-haiku-4-5-20251001",
                    enriched_at="2026-05-06T00:00:03+00:00",
                )
            async with session_scope() as session:
                repo = EnrichmentRepository(session)
                got = await repo.get_by_capture("cap-1", user_id=1)
                leak = await repo.get_by_capture("cap-1", user_id=2)
                ids = await repo.enriched_capture_ids(user_id=1)
            await aclose()
            return got, leak, ids

        got, leak, ids = asyncio.run(go())
        assert got is not None and got.summary == "Short summary."
        assert leak is None  # tenant isolation
        assert ids == {"cap-1"}

    def test_list_by_user_with_outer_join(self):
        """Captures without enrichment still appear, with enrichment=None."""
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                cap_repo = CaptureRepository(session)
                enr_repo = EnrichmentRepository(session)
                await cap_repo.create(_make_capture(
                    capture_id="cap-enriched", user_id=1,
                    captured_at="2026-05-06T01:00:00+00:00",
                ))
                await cap_repo.create(_make_capture(
                    capture_id="cap-bare", user_id=1,
                    captured_at="2026-05-06T02:00:00+00:00",
                ))
                await enr_repo.create(
                    capture_id="cap-enriched",
                    summary="Has enrichment.",
                    key_facts_json="[]",
                    model="claude-haiku-4-5-20251001",
                    enriched_at="2026-05-06T01:00:01+00:00",
                )
            async with session_scope() as session:
                repo = EnrichmentRepository(session)
                rows = await repo.list_by_user(user_id=1, limit=10)
            await aclose()
            return rows

        rows = asyncio.run(go())
        # Newest first: cap-bare then cap-enriched
        assert len(rows) == 2
        assert rows[0].capture.id == "cap-bare"
        assert rows[0].enrichment is None
        assert rows[1].capture.id == "cap-enriched"
        assert rows[1].enrichment is not None
        assert rows[1].enrichment.summary == "Has enrichment."


# ---- ChunkRepository ------------------------------------------------

class TestChunkRepository:
    def test_create_many_and_list(self):
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                cap_repo = CaptureRepository(session)
                chunk_repo = ChunkRepository(session)
                await cap_repo.create(_make_capture(capture_id="cap-1", user_id=1))
                ids = await chunk_repo.create_many([
                    ChunkInsert(
                        capture_id="cap-1", chunk_index=0,
                        text="First paragraph.", source_kind="article_paragraph",
                    ),
                    ChunkInsert(
                        capture_id="cap-1", chunk_index=1,
                        text="Second paragraph.", source_kind="article_paragraph",
                    ),
                ])
            async with session_scope() as session:
                chunk_repo = ChunkRepository(session)
                rows = await chunk_repo.list_by_capture("cap-1", user_id=1)
                leak = await chunk_repo.list_by_capture("cap-1", user_id=2)
                fetched = await chunk_repo.get_by_ids(ids, user_id=1)
            await aclose()
            return ids, rows, leak, fetched

        ids, rows, leak, fetched = asyncio.run(go())
        assert len(ids) == 2
        assert [c.chunk_index for c in rows] == [0, 1]
        assert leak == []
        assert {c.id for c in fetched} == set(ids)

    def test_attach_topics_is_idempotent(self):
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                cap_repo = CaptureRepository(session)
                chunk_repo = ChunkRepository(session)
                topic_repo = TopicRepository(session)
                await cap_repo.create(_make_capture(capture_id="cap-1", user_id=1))
                ids = await chunk_repo.create_many([
                    ChunkInsert(
                        capture_id="cap-1", chunk_index=0,
                        text="x", source_kind="summary",
                    ),
                ])
                t_kanban = await topic_repo.find_or_create(label="Kanban")
                t_agile = await topic_repo.find_or_create(label="Agile")

                # First attach: 2 rows
                await chunk_repo.attach_topics(
                    ids[0],
                    [(t_kanban.id, 0.9), (t_agile.id, 0.7)],
                )
                # Second attach: same pairs, should be no-ops
                await chunk_repo.attach_topics(
                    ids[0],
                    [(t_kanban.id, 0.95), (t_agile.id, 0.75)],
                )

            # Verify: chunk should have exactly 2 topic links
            async with session_scope() as session:
                from sqlalchemy import select
                from backend.storage.schema import chunk_topics
                result = await session.execute(
                    select(chunk_topics).where(chunk_topics.c.chunk_id == ids[0])
                )
                rows = result.fetchall()
            await aclose()
            return rows

        rows = asyncio.run(go())
        assert len(rows) == 2

    def test_attach_entities_with_mention_positions(self):
        """Same entity at different mention_positions yields multiple rows."""
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                cap_repo = CaptureRepository(session)
                chunk_repo = ChunkRepository(session)
                ent_repo = EntityRepository(session)
                await cap_repo.create(_make_capture(capture_id="cap-1", user_id=1))
                ids = await chunk_repo.create_many([
                    ChunkInsert(
                        capture_id="cap-1", chunk_index=0,
                        text="Anthropic announced... Anthropic also...",
                        source_kind="article_paragraph",
                    ),
                ])
                ent = await ent_repo.find_or_create(
                    label="Anthropic", entity_type="company",
                )
                await chunk_repo.attach_entities(
                    ids[0],
                    [
                        ChunkAttachment(entity_id=ent.id, confidence=0.9, mention_position=0),
                        ChunkAttachment(entity_id=ent.id, confidence=0.85, mention_position=22),
                    ],
                )
                # Re-attach: should be no-ops
                await chunk_repo.attach_entities(
                    ids[0],
                    [
                        ChunkAttachment(entity_id=ent.id, confidence=0.99, mention_position=0),
                    ],
                )
            async with session_scope() as session:
                from sqlalchemy import select
                from backend.storage.schema import chunk_entities
                result = await session.execute(
                    select(chunk_entities).where(chunk_entities.c.chunk_id == ids[0])
                )
                rows = result.fetchall()
            await aclose()
            return rows

        rows = asyncio.run(go())
        # Same entity, two mention positions = two rows
        assert len(rows) == 2
        positions = {r.mention_position for r in rows}
        assert positions == {0, 22}


# ---- TopicRepository -----------------------------------------------

class TestTopicRepository:
    def test_find_or_create_returns_existing(self):
        """Calling find_or_create twice with the same label returns
        the same topic id — that's the controlled-vocabulary
        guarantee."""
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                repo = TopicRepository(session)
                a = await repo.find_or_create(label="Kanban Method")
                b = await repo.find_or_create(label="kanban method")  # case variant
                c = await repo.find_or_create(
                    label="Kanban Method!",  # punctuation variant -> same slug
                )
            await aclose()
            return a, b, c

        a, b, c = asyncio.run(go())
        assert a.id == b.id == c.id
        assert a.slug == "kanban-method"

    def test_find_or_create_carries_embedding(self):
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                repo = TopicRepository(session)
                topic = await repo.find_or_create(
                    label="Kanban",
                    description="Visual work-management system",
                    embedding=b"\x00\x01\x02\x03",
                )
            await aclose()
            return topic

        topic = asyncio.run(go())
        assert topic.embedding == b"\x00\x01\x02\x03"
        assert topic.description == "Visual work-management system"

    def test_get_by_slug_misses_return_none(self):
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                repo = TopicRepository(session)
                got = await repo.get_by_slug("does-not-exist")
            await aclose()
            return got

        assert asyncio.run(go()) is None


# ---- EntityRepository ----------------------------------------------

class TestEntityRepository:
    def test_find_or_create_with_type(self):
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                repo = EntityRepository(session)
                a = await repo.find_or_create(
                    label="Deepika Padukone", entity_type="person",
                )
                b = await repo.find_or_create(
                    label="DEEPIKA PADUKONE", entity_type="person",
                )
            await aclose()
            return a, b

        a, b = asyncio.run(go())
        assert a.id == b.id
        assert a.slug == "deepika-padukone"
        assert a.entity_type == "person"

    def test_unknown_entity_type_raises(self):
        async def go():
            await _seed_two_users()
            with pytest.raises(ValueError):
                async with session_scope() as session:
                    repo = EntityRepository(session)
                    await repo.find_or_create(
                        label="X", entity_type="not-a-real-type",
                    )
            await aclose()
        asyncio.run(go())
