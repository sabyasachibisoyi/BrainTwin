"""Tests for backend/storage/ — Phase 3 Step 1a: SQL foundation.

Run with: pytest tests/test_storage_schema.py -v

Covers:
  - All expected tables are defined in metadata
  - init_db() creates the schema cleanly in a fresh in-memory SQLite
  - Round-trip insert + select works for every domain table
  - Foreign-key constraints are enforced (capture without user_id fails;
    chunk without capture_id fails)
  - UNIQUE constraints behave (duplicate (capture_id, chunk_index) fails;
    duplicate user.email fails)

These tests run against `sqlite:///:memory:` so they don't touch the
real `data/braintwin.db`. The `clean_engine` fixture also disposes of
the engine between cases so settings.database_url monkeypatches stay
isolated.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Set DATABASE_URL before importing backend.config so the singleton
# `settings` is built with the in-memory URL. Tests then monkey-patch
# the storage module's lazy globals to ensure each case gets a fresh
# engine with no leftover state.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy import insert, select  # noqa: E402

from backend.storage import (  # noqa: E402
    aclose,
    captures,
    chunk_entities,
    chunk_topics,
    chunks,
    entities,
    enrichments,
    hydrations,
    init_db,
    metadata,
    session_scope,
    topics,
    users,
)
from backend.storage import db as db_module  # noqa: E402


# ---- Fixtures --------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_engine(monkeypatch):
    """Reset the storage module's lazy globals before every test so each
    case gets a brand-new in-memory database. Without this, the engine
    from one test leaks into the next and we get cross-contamination."""
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_session_factory", None)
    yield
    # No async teardown here — tests that need it call aclose() inside
    # asyncio.run, otherwise the next test's clean_engine override
    # picks up.


def _seed_user(session):
    """Most tests need at least one user row before they can insert
    captures. Centralised so we don't repeat the literal in every case."""
    return session.execute(
        insert(users).values(
            id=1,
            email="sabya.bisoyi@gmail.com",
            display_name="Sabya",
            created_at="2026-05-06T00:00:00+00:00",
        )
    )


# ---- Schema definition tests ----------------------------------------

class TestMetadata:
    def test_all_expected_tables_defined(self):
        names = set(metadata.tables.keys())
        expected = {
            "users", "captures", "hydrations", "enrichments",
            "chunks", "topics", "entities",
            "chunk_topics", "chunk_entities",
        }
        assert expected.issubset(names), (
            f"missing tables: {expected - names}"
        )

    def test_chunks_has_unique_constraint(self):
        # The (capture_id, chunk_index) UNIQUE constraint exists so re-running
        # enrichment doesn't insert duplicate chunk rows.
        constraints = {c.name for c in chunks.constraints}
        assert "uq_chunks_capture_chunk" in constraints

    def test_topics_and_entities_lack_user_id(self):
        # B.7 — topics and entities are SHARED global vocabulary across
        # users. If a future migration accidentally adds user_id to these,
        # this test fails loudly.
        assert "user_id" not in topics.c
        assert "user_id" not in entities.c

    def test_captures_carry_user_id(self):
        # A.2 — every capture must be tenant-tagged.
        assert "user_id" in captures.c
        # Per the schema, user_id is NOT NULL.
        assert captures.c.user_id.nullable is False


# ---- init_db + round-trip tests --------------------------------------

class TestInitDb:
    def test_init_creates_all_tables(self):
        async def go():
            await init_db()
            engine = db_module.get_engine()
            # Inspect the live database to confirm every table exists.
            from sqlalchemy import text
            async with engine.connect() as conn:
                rows = (await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                )).fetchall()
            await aclose()
            return [row[0] for row in rows]

        live_tables = asyncio.run(go())
        for expected in [
            "captures", "chunk_entities", "chunk_topics", "chunks",
            "entities", "enrichments", "hydrations", "topics", "users",
        ]:
            assert expected in live_tables, f"{expected} not created"

    def test_init_is_idempotent(self):
        async def go():
            await init_db()
            await init_db()  # second call should be a no-op
            await aclose()
        # Just ensuring it doesn't raise.
        asyncio.run(go())


class TestRoundTrip:
    def test_user_capture_chunk_round_trip(self):
        async def go():
            await init_db()
            async with session_scope() as session:
                await _seed_user(session)
                await session.execute(
                    insert(captures).values(
                        id="cap-001",
                        user_id=1,
                        url="https://example.com/x",
                        title="An article",
                        platform="general",
                        content_type="article",
                        captured_at="2026-05-06T00:00:01+00:00",
                        dwell_seconds=42,
                        raw_metadata_json="{}",
                    )
                )
                await session.execute(
                    insert(chunks).values(
                        capture_id="cap-001",
                        chunk_index=0,
                        text="Hello world.",
                        source_kind="article_paragraph",
                        embedding=None,
                    )
                )

            async with session_scope() as session:
                cap_rows = (await session.execute(select(captures))).fetchall()
                chunk_rows = (await session.execute(select(chunks))).fetchall()
            await aclose()
            return cap_rows, chunk_rows

        cap_rows, chunk_rows = asyncio.run(go())
        assert len(cap_rows) == 1
        assert cap_rows[0].id == "cap-001"
        assert cap_rows[0].user_id == 1
        assert cap_rows[0].dwell_seconds == 42
        assert len(chunk_rows) == 1
        assert chunk_rows[0].capture_id == "cap-001"
        assert chunk_rows[0].chunk_index == 0
        assert chunk_rows[0].text == "Hello world."

    def test_topic_entity_junctions_round_trip(self):
        """Insert a chunk, tag it with a topic and an entity, read back via JOINs."""
        async def go():
            await init_db()
            async with session_scope() as session:
                await _seed_user(session)
                await session.execute(insert(captures).values(
                    id="cap-002", user_id=1, url=None, title="x",
                    platform="general", content_type="article",
                    captured_at="2026-05-06T00:00:02+00:00",
                    dwell_seconds=0, raw_metadata_json="{}",
                ))
                chunk_result = await session.execute(insert(chunks).values(
                    capture_id="cap-002", chunk_index=0, text="…",
                    source_kind="summary", embedding=None,
                ))
                chunk_id = chunk_result.inserted_primary_key[0]
                topic_result = await session.execute(insert(topics).values(
                    slug="kanban", label="Kanban",
                    description="Visual work-management system",
                    embedding=None,
                    coined_at="2026-05-06T00:00:00+00:00",
                ))
                topic_id = topic_result.inserted_primary_key[0]
                ent_result = await session.execute(insert(entities).values(
                    slug="anthropic", label="Anthropic", entity_type="company",
                    embedding=None,
                    coined_at="2026-05-06T00:00:00+00:00",
                ))
                entity_id = ent_result.inserted_primary_key[0]
                await session.execute(insert(chunk_topics).values(
                    chunk_id=chunk_id, topic_id=topic_id, confidence=0.9,
                ))
                await session.execute(insert(chunk_entities).values(
                    chunk_id=chunk_id, entity_id=entity_id,
                    mention_position=0, confidence=0.95,
                ))

            async with session_scope() as session:
                ct = (await session.execute(select(chunk_topics))).fetchall()
                ce = (await session.execute(select(chunk_entities))).fetchall()
            await aclose()
            return ct, ce

        ct_rows, ce_rows = asyncio.run(go())
        assert len(ct_rows) == 1 and ct_rows[0].confidence == 0.9
        assert len(ce_rows) == 1 and ce_rows[0].confidence == 0.95


# ---- Constraint tests ------------------------------------------------

class TestConstraints:
    def test_duplicate_email_fails(self):
        async def go():
            await init_db()
            from sqlalchemy.exc import IntegrityError
            with pytest.raises(IntegrityError):
                async with session_scope() as session:
                    await _seed_user(session)
                    await session.execute(insert(users).values(
                        email="sabya.bisoyi@gmail.com",   # same email
                        display_name="Duplicate",
                        created_at="2026-05-06T00:00:01+00:00",
                    ))
            await aclose()
        asyncio.run(go())

    def test_duplicate_chunk_index_fails(self):
        """Re-inserting (capture_id, chunk_index) must raise."""
        async def go():
            await init_db()
            from sqlalchemy.exc import IntegrityError
            async with session_scope() as session:
                await _seed_user(session)
                await session.execute(insert(captures).values(
                    id="cap-003", user_id=1, url=None, title=None,
                    platform="general", content_type="article",
                    captured_at="2026-05-06T00:00:00+00:00",
                    dwell_seconds=0, raw_metadata_json=None,
                ))
                await session.execute(insert(chunks).values(
                    capture_id="cap-003", chunk_index=0,
                    text="first", source_kind="article_paragraph",
                ))
            with pytest.raises(IntegrityError):
                async with session_scope() as session:
                    await session.execute(insert(chunks).values(
                        capture_id="cap-003", chunk_index=0,   # collision
                        text="second", source_kind="article_paragraph",
                    ))
            await aclose()
        asyncio.run(go())
