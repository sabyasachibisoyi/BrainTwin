"""Tests for the Phase 4 M.1 FTS5 chunks index + BM25 search.

Covers (per the milestone scope in docs/phase4-vague-recall-design.md M.1):

  - init_db() creates the chunks_fts virtual table AND all three sync
    triggers (verified by reading sqlite_master).
  - The AFTER INSERT trigger auto-indexes new chunks (search finds them).
  - The AFTER UPDATE OF text trigger re-indexes when text changes
    (old text no longer matches; new text does).
  - The AFTER DELETE trigger drops rows from the index.
  - search_by_bm25 returns expected ranking on a small fixture corpus,
    and the score is positive (sign-flipped from SQLite's negative
    bm25() convention).
  - Tenant isolation — user A's chunks must NOT appear in user B's
    BM25 search results, even when both users own a chunk that
    matches the query.
  - The proper-noun case from the design doc — "Tamasha" surfaces the
    right chunk even when other chunks in the same user's corpus are
    longer and more topical-looking. BM25 alone, not paired with
    vector here (vector hybrid lands in M.2).

Mirrors the style of tests/test_storage_repos.py:
  - asyncio.run inside a sync test method
  - in-memory SQLite via the conftest.py-set DATABASE_URL
  - autouse clean_engine fixture resets the lazy storage globals

Run with: pytest tests/test_chunks_fts.py -v
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

from sqlalchemy import text, update  # noqa: E402

from backend.storage import (  # noqa: E402
    CaptureRepository,
    ChunkRepository,
    ChunkInsert,
    UserRepository,
    aclose,
    init_db,
    session_scope,
)
from backend.storage import db as db_module  # noqa: E402
from backend.storage.models import Capture  # noqa: E402
from backend.storage.schema import chunks  # noqa: E402


# ---- Fixtures --------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_engine(monkeypatch):
    """Reset storage module state so each test gets a fresh in-memory
    DB. Matches the fixture in test_storage_repos.py / test_storage_schema.py.
    """
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_session_factory", None)
    yield


# ---- Helpers ---------------------------------------------------------

def _make_capture(
    *,
    capture_id: str,
    user_id: int,
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
    """Create users 1 and 2 so cross-tenant tests have a target. Matches
    the helper in test_storage_repos.py."""
    await init_db()
    async with session_scope() as session:
        users_repo = UserRepository(session)
        await users_repo.create(
            email="sabya@example.com", display_name="Sabya", user_id=1,
        )
        await users_repo.create(
            email="other@example.com", display_name="Other", user_id=2,
        )


async def _sqlite_master_names() -> set[str]:
    """All object names in sqlite_master — tables, views, triggers,
    indexes. Used to verify init_db created the FTS5 table + triggers."""
    async with session_scope() as session:
        result = await session.execute(text(
            "SELECT name FROM sqlite_master"
        ))
        return {row[0] for row in result}


# ---- M.1 — schema migration creates FTS5 table + triggers ------------

class TestInitDbCreatesFtsObjects:
    """init_db() must create the chunks_fts virtual table and the three
    sync triggers. Idempotent — re-running must not raise."""

    def test_init_creates_fts_table_and_triggers(self):
        async def go():
            await init_db()
            names = await _sqlite_master_names()
            await aclose()
            return names

        names = asyncio.run(go())
        assert "chunks_fts" in names, "FTS5 virtual table missing"
        for trig in (
            "chunks_fts_after_insert",
            "chunks_fts_after_update",
            "chunks_fts_after_delete",
        ):
            assert trig in names, f"trigger {trig} missing"

    def test_init_is_idempotent(self):
        async def go():
            await init_db()
            await init_db()  # second call must be a no-op
            names = await _sqlite_master_names()
            await aclose()
            return names

        names = asyncio.run(go())
        # Still exactly one of each — re-running can't dup or err.
        assert "chunks_fts" in names
        assert "chunks_fts_after_insert" in names


# ---- M.1 — INSERT / UPDATE / DELETE triggers keep FTS in sync --------

class TestFtsTriggersKeepIndexInSync:
    """The whole point of the trigger trio: every mutation of
    chunks.text propagates to chunks_fts immediately. No explicit
    backfill needed in the live write path."""

    def test_insert_auto_indexes(self):
        """A fresh chunk INSERT must land in the FTS index without
        any explicit FTS write — the AFTER INSERT trigger handles it."""
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                cap_repo = CaptureRepository(session)
                chunk_repo = ChunkRepository(session)
                await cap_repo.create(_make_capture(capture_id="cap-1", user_id=1))
                await chunk_repo.create_many([
                    ChunkInsert(
                        capture_id="cap-1", chunk_index=0,
                        text="I watched the movie Tamasha last night.",
                        source_kind="article_paragraph",
                    ),
                ])
            async with session_scope() as session:
                repo = ChunkRepository(session)
                hits = await repo.search_by_bm25("Tamasha", user_id=1, limit=10)
            await aclose()
            return hits

        hits = asyncio.run(go())
        assert len(hits) == 1
        assert hits[0].chunk.text.startswith("I watched the movie Tamasha")

    def test_update_reindexes_text(self):
        """Updating chunks.text must drop the old tokens from the FTS
        index and add the new ones. The AFTER UPDATE OF text trigger
        handles this via the delete/insert dance documented for
        external-content FTS5 tables."""
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                cap_repo = CaptureRepository(session)
                chunk_repo = ChunkRepository(session)
                await cap_repo.create(_make_capture(capture_id="cap-1", user_id=1))
                ids = await chunk_repo.create_many([
                    ChunkInsert(
                        capture_id="cap-1", chunk_index=0,
                        text="A chunk about hash collisions.",
                        source_kind="article_paragraph",
                    ),
                ])
                # Direct SQL UPDATE on the chunks table — exercises the
                # trigger path. (Repository write-path tests live
                # separately in test_storage_repos.py.)
                await session.execute(
                    update(chunks)
                    .where(chunks.c.id == ids[0])
                    .values(text="Completely different content about Tamasha.")
                )
            async with session_scope() as session:
                repo = ChunkRepository(session)
                old_hits = await repo.search_by_bm25(
                    "hash collisions", user_id=1, limit=10,
                )
                new_hits = await repo.search_by_bm25(
                    "Tamasha", user_id=1, limit=10,
                )
            await aclose()
            return old_hits, new_hits

        old_hits, new_hits = asyncio.run(go())
        # Old tokens no longer match — the trigger removed them.
        assert old_hits == []
        # New tokens match.
        assert len(new_hits) == 1
        assert "Tamasha" in new_hits[0].chunk.text

    def test_delete_drops_from_index(self):
        """Deleting a chunk row must remove it from the FTS index."""
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                cap_repo = CaptureRepository(session)
                chunk_repo = ChunkRepository(session)
                await cap_repo.create(_make_capture(capture_id="cap-1", user_id=1))
                ids = await chunk_repo.create_many([
                    ChunkInsert(
                        capture_id="cap-1", chunk_index=0,
                        text="Tamasha is a 2015 film.",
                        source_kind="article_paragraph",
                    ),
                ])
                await session.execute(
                    chunks.delete().where(chunks.c.id == ids[0])
                )
            async with session_scope() as session:
                repo = ChunkRepository(session)
                hits = await repo.search_by_bm25(
                    "Tamasha", user_id=1, limit=10,
                )
            await aclose()
            return hits

        hits = asyncio.run(go())
        assert hits == []


# ---- M.1 — search_by_bm25 ranking + score convention -----------------

class TestSearchByBm25Ranking:
    """Exercise the BM25 ranking on a small corpus and confirm the
    score-sign-flip contract (positive = better, higher = better)."""

    def test_bm25_ranks_more_specific_match_higher(self):
        """A chunk that mentions the query token multiple times in a
        short text should score higher than a chunk that mentions it
        once in a long text — that's the heart of BM25."""
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                cap_repo = CaptureRepository(session)
                chunk_repo = ChunkRepository(session)
                await cap_repo.create(_make_capture(capture_id="cap-A", user_id=1))
                await cap_repo.create(_make_capture(capture_id="cap-B", user_id=1))
                await chunk_repo.create_many([
                    # Concentrated mention of the rare token.
                    ChunkInsert(
                        capture_id="cap-A", chunk_index=0,
                        text="Tamasha Tamasha review",
                        source_kind="article_paragraph",
                    ),
                    # One mention in a long, off-topic chunk.
                    ChunkInsert(
                        capture_id="cap-B", chunk_index=0,
                        text=(
                            "Bollywood produces hundreds of films each year, "
                            "covering many genres and languages, including "
                            "a 2015 release called Tamasha which was one of "
                            "many that year."
                        ),
                        source_kind="article_paragraph",
                    ),
                ])
            async with session_scope() as session:
                repo = ChunkRepository(session)
                hits = await repo.search_by_bm25(
                    "Tamasha", user_id=1, limit=10,
                )
            await aclose()
            return hits

        hits = asyncio.run(go())
        assert len(hits) == 2
        # Concentrated mention ranks first.
        assert hits[0].chunk.capture_id == "cap-A"
        assert hits[1].chunk.capture_id == "cap-B"
        # All scores positive (sign-flipped from SQLite's negative BM25),
        # and the top score is strictly greater than the second.
        assert hits[0].score > 0
        assert hits[1].score > 0
        assert hits[0].score > hits[1].score

    def test_empty_query_returns_empty_list(self):
        """A blank query must not error and must not return rows."""
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                cap_repo = CaptureRepository(session)
                chunk_repo = ChunkRepository(session)
                await cap_repo.create(_make_capture(capture_id="cap-1", user_id=1))
                await chunk_repo.create_many([
                    ChunkInsert(
                        capture_id="cap-1", chunk_index=0,
                        text="some text",
                        source_kind="article_paragraph",
                    ),
                ])
            async with session_scope() as session:
                repo = ChunkRepository(session)
                blank = await repo.search_by_bm25("", user_id=1)
                whitespace = await repo.search_by_bm25("   ", user_id=1)
            await aclose()
            return blank, whitespace

        blank, whitespace = asyncio.run(go())
        assert blank == []
        assert whitespace == []

    def test_limit_caps_results(self):
        """The `limit` argument must cap returned rows."""
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                cap_repo = CaptureRepository(session)
                chunk_repo = ChunkRepository(session)
                await cap_repo.create(_make_capture(capture_id="cap-1", user_id=1))
                await chunk_repo.create_many([
                    ChunkInsert(
                        capture_id="cap-1", chunk_index=i,
                        text=f"the quick brown fox number {i}",
                        source_kind="article_paragraph",
                    )
                    for i in range(5)
                ])
            async with session_scope() as session:
                repo = ChunkRepository(session)
                hits = await repo.search_by_bm25(
                    "fox", user_id=1, limit=2,
                )
            await aclose()
            return hits

        hits = asyncio.run(go())
        assert len(hits) == 2


# ---- M.1 — tenant isolation ------------------------------------------

class TestBm25TenantIsolation:
    """User A's chunks must NOT surface in user B's BM25 results, even
    when both users own chunks that match the query. Same posture as
    every other tenant-scoped repository method."""

    def test_cross_tenant_chunks_invisible(self):
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                cap_repo = CaptureRepository(session)
                chunk_repo = ChunkRepository(session)
                # User 1's capture + chunk
                await cap_repo.create(_make_capture(
                    capture_id="cap-user1", user_id=1,
                ))
                await chunk_repo.create_many([
                    ChunkInsert(
                        capture_id="cap-user1", chunk_index=0,
                        text="User 1 wrote about hash collisions today.",
                        source_kind="article_paragraph",
                    ),
                ])
                # User 2's capture + chunk — also about hash collisions
                await cap_repo.create(_make_capture(
                    capture_id="cap-user2", user_id=2,
                ))
                await chunk_repo.create_many([
                    ChunkInsert(
                        capture_id="cap-user2", chunk_index=0,
                        text="User 2 also wrote about hash collisions today.",
                        source_kind="article_paragraph",
                    ),
                ])
            async with session_scope() as session:
                repo = ChunkRepository(session)
                u1_hits = await repo.search_by_bm25(
                    "hash collisions", user_id=1, limit=10,
                )
                u2_hits = await repo.search_by_bm25(
                    "hash collisions", user_id=2, limit=10,
                )
                # And a user with no captures at all.
                u99_hits = await repo.search_by_bm25(
                    "hash collisions", user_id=99, limit=10,
                )
            await aclose()
            return u1_hits, u2_hits, u99_hits

        u1_hits, u2_hits, u99_hits = asyncio.run(go())
        assert len(u1_hits) == 1
        assert u1_hits[0].chunk.capture_id == "cap-user1"
        assert len(u2_hits) == 1
        assert u2_hits[0].chunk.capture_id == "cap-user2"
        assert u99_hits == []


# ---- M.1 — the design-doc proper-noun case ---------------------------

class TestProperNounSurfacing:
    """The motivating case from docs/phase4-vague-recall-design.md
    V.1: "Tamasha" must surface the right chunk even when the corpus
    is dominated by chunks that look topically related but don't
    contain the proper noun. We're testing BM25 *alone* here — the
    hybrid fusion with Chroma is M.2's job.

    BM25 only ever indexes literal tokens, so this is the easy half
    of the proper-noun problem: if the token is in the corpus, BM25
    finds it. The hard half (cross-spelling Bengaluru/Bangalore, etc.)
    is genuinely Chroma's job, not FTS5's."""

    def test_tamasha_query_surfaces_tamasha_chunk(self):
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                cap_repo = CaptureRepository(session)
                chunk_repo = ChunkRepository(session)
                # A capture whose body never mentions "Tamasha" but
                # is otherwise full of Bollywood-ish chatter that a
                # vector embedder would happily score near a Tamasha
                # query. Several such chunks to dominate the candidate
                # pool by volume.
                await cap_repo.create(_make_capture(
                    capture_id="cap-bollywood", user_id=1,
                ))
                await chunk_repo.create_many([
                    ChunkInsert(
                        capture_id="cap-bollywood", chunk_index=i,
                        text=(
                            f"Bollywood film number {i} features dance "
                            "sequences, romance arcs, and an item song "
                            "that goes viral on social media."
                        ),
                        source_kind="article_paragraph",
                    )
                    for i in range(5)
                ])
                # The actual Tamasha chunk — short, on-point.
                await cap_repo.create(_make_capture(
                    capture_id="cap-tamasha", user_id=1,
                ))
                await chunk_repo.create_many([
                    ChunkInsert(
                        capture_id="cap-tamasha", chunk_index=0,
                        text="Tamasha is a 2015 Imtiaz Ali film.",
                        source_kind="article_paragraph",
                    ),
                ])
            async with session_scope() as session:
                repo = ChunkRepository(session)
                hits = await repo.search_by_bm25(
                    "Tamasha", user_id=1, limit=20,
                )
            await aclose()
            return hits

        hits = asyncio.run(go())
        # Even with 5 "Bollywood" chunks in the corpus, the single
        # Tamasha chunk is the only one that matches the query — BM25
        # only ranks chunks that contain the literal token.
        assert len(hits) == 1
        assert hits[0].chunk.capture_id == "cap-tamasha"
        assert "Tamasha" in hits[0].chunk.text

    def test_diacritic_normalization_matches_accented_token(self):
        """The unicode61 tokenizer with `remove_diacritics 2` must
        let "café" match against an ASCII-only "cafe" in the text and
        vice versa. This is one of the explicit design choices in M.1."""
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                cap_repo = CaptureRepository(session)
                chunk_repo = ChunkRepository(session)
                await cap_repo.create(_make_capture(capture_id="cap-1", user_id=1))
                await chunk_repo.create_many([
                    ChunkInsert(
                        capture_id="cap-1", chunk_index=0,
                        text="visited a cafe in Bengaluru last week",
                        source_kind="article_paragraph",
                    ),
                ])
            async with session_scope() as session:
                repo = ChunkRepository(session)
                ascii_hits = await repo.search_by_bm25(
                    "cafe", user_id=1, limit=10,
                )
                accented_hits = await repo.search_by_bm25(
                    "café", user_id=1, limit=10,
                )
            await aclose()
            return ascii_hits, accented_hits

        ascii_hits, accented_hits = asyncio.run(go())
        assert len(ascii_hits) == 1
        assert len(accented_hits) == 1
        assert ascii_hits[0].chunk.id == accented_hits[0].chunk.id


# ---- M.1 — FTS5 query sanitization (bugfix) --------------------------

class TestFtsQuerySanitization:
    """Free-form recall queries must never reach FTS5's query parser
    raw — punctuation like `: ( ) - + ? "` or a leading AND/OR/NOT
    raises sqlite3.OperationalError otherwise. `search_by_bm25`
    normalizes via `_build_fts_match_query` first.
    """

    def test_build_fts_match_query_unit(self):
        from backend.storage.repositories.chunk_repo import (
            _build_fts_match_query,
        )
        # Word tokens preserved (incl. non-ASCII), OR-joined and quoted.
        assert _build_fts_match_query("kanban team") == '"kanban" OR "team"'
        # Punctuation / FTS5 operators stripped to bare tokens.
        assert _build_fts_match_query("notes: kanban?") == '"notes" OR "kanban"'
        assert _build_fts_match_query("team-size (WIP)") == (
            '"team" OR "size" OR "WIP"'
        )
        assert _build_fts_match_query("AND kanban") == '"AND" OR "kanban"'
        # Non-ASCII proper nouns survive (BM25's whole reason to exist).
        assert _build_fts_match_query("Bengaluru café") == (
            '"Bengaluru" OR "café"'
        )
        # All-punctuation reduces to nothing → empty (caller short-circuits).
        assert _build_fts_match_query("?!:()-+") == ""

    @pytest.mark.parametrize("query", [
        "what about kanban?",
        "notes: kanban",
        "team-size tradeoffs about kanban",
        "C++ kanban",
        "kanban (team",
        "AND kanban",
        'the "kanban" article',
    ])
    def test_punctuation_queries_do_not_raise_and_match(self, query):
        """Each of these raised OperationalError before the fix. Now
        they run cleanly and still find the kanban chunk."""
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                cap_repo = CaptureRepository(session)
                chunk_repo = ChunkRepository(session)
                await cap_repo.create(_make_capture(capture_id="cap-1", user_id=1))
                await chunk_repo.create_many([
                    ChunkInsert(
                        capture_id="cap-1", chunk_index=0,
                        text="the kanban article about team size and WIP limits",
                        source_kind="article_paragraph",
                    ),
                ])
            async with session_scope() as session:
                repo = ChunkRepository(session)
                hits = await repo.search_by_bm25(query, user_id=1, limit=10)
            await aclose()
            return hits

        hits = asyncio.run(go())
        assert len(hits) == 1
        assert hits[0].chunk.capture_id == "cap-1"

    def test_all_punctuation_query_returns_empty(self):
        """A query with no word tokens short-circuits to [] without
        touching FTS5."""
        async def go():
            await _seed_two_users()
            async with session_scope() as session:
                cap_repo = CaptureRepository(session)
                chunk_repo = ChunkRepository(session)
                await cap_repo.create(_make_capture(capture_id="cap-1", user_id=1))
                await chunk_repo.create_many([
                    ChunkInsert(
                        capture_id="cap-1", chunk_index=0,
                        text="some kanban text",
                        source_kind="article_paragraph",
                    ),
                ])
            async with session_scope() as session:
                repo = ChunkRepository(session)
                hits = await repo.search_by_bm25("?!:()", user_id=1, limit=10)
            await aclose()
            return hits

        assert asyncio.run(go()) == []
