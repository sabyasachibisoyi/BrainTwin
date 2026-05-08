"""Tests for backend/storage/sync.py — Phase 3 Step 4.

Run with: pytest tests/test_storage_sync.py -v

Uses in-memory SQLite + a tmp ChromaDB directory + a stub embedder.
No real models loaded; tests run fast.

Covered:
  - sync_capture: round-trip insert; idempotent on duplicate id;
    survives SQL hiccups without raising.
  - sync_hydration: round-trip; FK violation surfaces as a swallowed
    log warning (not a crash).
  - sync_enrichment without processed: just inserts the enrichment
    row and the topics/entities, no chunks.
  - sync_enrichment with processed: chunks per source kind, embeds
    in batch, mirrors to Chroma; SQL chunk count == Chroma vector
    count for the capture.
  - Topics + entities go through find_or_create with slug normalization
    so duplicates collapse.
  - Per-chunk topic/entity attachment: every chunk in the capture
    gets every topic/entity attached.
  - Defensive: a sync call with garbage input (None, empty strings,
    empty lists) doesn't raise.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

# chromadb is required because sync_enrichment writes to it. Skip the
# whole file if not installed (matches the convention in
# test_vector_store.py).
chromadb = pytest.importorskip("chromadb")

from sqlalchemy import select  # noqa: E402

from backend.storage import (  # noqa: E402
    CaptureRepository,
    ChunkRepository,
    EntityRepository,
    EnrichmentRepository,
    HydrationRepository,
    TopicRepository,
    chunk_entities,
    chunk_topics,
    chunks,
    entities,
    init_db,
    session_scope,
    topics,
)
from backend.storage import db as db_module  # noqa: E402
from backend.storage.embedder import EMBEDDING_DIM, Embedder  # noqa: E402
from backend.storage.repositories.user_repo import UserRepository  # noqa: E402
from backend.storage.sync import (  # noqa: E402
    DEFAULT_USER_ID,
    sync_capture,
    sync_enrichment,
    sync_hydration,
)
from backend.storage.vector_store import (  # noqa: E402
    COLLECTION_CHUNKS,
    COLLECTION_ENTITIES,
    COLLECTION_TOPICS,
    ChromaVectorStore,
)


# ---- Stubs + fixtures ------------------------------------------------

class _StubEmbedder:
    """Deterministic non-trivial embedder. Same text → same vector,
    different text → different vector. No real model loaded."""

    @property
    def model_name(self) -> str:
        return "stub"

    @property
    def dim(self) -> int:
        return EMBEDDING_DIM

    def _vec(self, text: str) -> np.ndarray:
        seed = abs(hash(text)) % (2**32)
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(EMBEDDING_DIM, dtype=np.float32)
        return (v / max(float(np.linalg.norm(v)), 1e-9)).astype(np.float32)

    def embed(self, text: str) -> np.ndarray:
        return self._vec(text or "")

    def embed_many(self, texts: list[str]) -> list[np.ndarray]:
        return [self._vec(t or "") for t in texts]


@dataclass(frozen=True)
class _ProcessedStub:
    """Light fake of `backend.capture.processor.ProcessedContent`.
    sync_enrichment only needs a few fields: clean_text, transcript,
    image_text, timestamp."""
    clean_text: str = ""
    transcript: str = ""
    image_text: str = ""
    timestamp: str = "2026-05-08T00:00:00+00:00"


@pytest.fixture
def vector_store(tmp_path):
    """Fresh tmp-dir Chroma + the stub embedder. Each test gets its
    own dir so collections don't leak across cases."""
    return ChromaVectorStore(
        embedder=_StubEmbedder(),  # type: ignore[arg-type]
        path=str(tmp_path / "chroma"),
    )


@pytest.fixture
def stub_embedder():
    return _StubEmbedder()


@pytest.fixture(autouse=True)
def clean_engine(monkeypatch):
    """Reset SQL engine + session factory between tests so each gets
    a fresh in-memory DB."""
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_session_factory", None)
    yield


async def _seed_user_and_init() -> None:
    """Most tests need an initialized DB and at least user_id=1."""
    await init_db()
    async with session_scope() as session:
        await UserRepository(session).create(
            email="sabya@example.com",
            display_name="Sabya",
            user_id=DEFAULT_USER_ID,
        )


# ---- sync_capture ----------------------------------------------------

class TestSyncCapture:
    def test_insert_round_trip(self):
        async def go():
            await _seed_user_and_init()
            ok = await sync_capture(
                capture_id="cap-001",
                url="https://example.com/x",
                title="Test",
                platform="general",
                content_type="article",
                captured_at="2026-05-08T10:00:00+00:00",
                dwell_seconds=42,
                raw_metadata_json='{"source": "test"}',
            )
            async with session_scope() as session:
                row = await CaptureRepository(session).get(
                    "cap-001", user_id=DEFAULT_USER_ID,
                )
            return ok, row

        ok, row = asyncio.run(go())
        assert ok is True
        assert row is not None
        assert row.id == "cap-001"
        assert row.user_id == DEFAULT_USER_ID
        assert row.dwell_seconds == 42

    def test_duplicate_capture_id_is_silent_noop(self):
        """sync_capture is idempotent — calling it twice with the
        same id should NOT raise and should return False on the
        second call (signalling skip)."""
        async def go():
            await _seed_user_and_init()
            first = await sync_capture(
                capture_id="cap-dupe",
                url=None, title="x", platform="general",
                content_type="article",
                captured_at="2026-05-08T10:00:00+00:00",
            )
            second = await sync_capture(
                capture_id="cap-dupe",
                url="different-url",
                title="different-title",
                platform="general",
                content_type="article",
                captured_at="2026-05-08T10:00:00+00:00",
            )
            async with session_scope() as session:
                row = await CaptureRepository(session).get(
                    "cap-dupe", user_id=DEFAULT_USER_ID,
                )
            return first, second, row

        first, second, row = asyncio.run(go())
        assert first is True
        assert second is False
        # Original row is preserved — we did NOT overwrite.
        assert row.title == "x"
        assert row.url is None

    def test_failure_returns_false_does_not_raise(self):
        """If the SQL write fails (e.g. no DB initialized), sync_capture
        must catch and log, return False, NOT raise."""
        async def go():
            # Don't init DB. captures table doesn't exist yet.
            return await sync_capture(
                capture_id="cap-no-db",
                url=None, title="x", platform="general",
                content_type="article",
                captured_at="2026-05-08T10:00:00+00:00",
            )

        result = asyncio.run(go())
        assert result is False  # logged + swallowed, not raised

    def test_duplicate_check_is_tenant_scoped(self):
        """Duplicate check must be tenant-scoped. CaptureRepository.exists()
        is a cross-tenant probe (its own docstring forbids application
        use); sync_capture must not call it. Verified indirectly: a
        second user trying to insert the SAME capture_id should NOT be
        silently swallowed as a 'duplicate' — the global TEXT PK will
        surface a real IntegrityError, which the boundary catches and
        logs (returning False)."""
        async def go():
            await _seed_user_and_init()
            # Seed a second user.
            async with session_scope() as session:
                await UserRepository(session).create(
                    email="other@example.com",
                    display_name="Other",
                    user_id=2,
                )
            first = await sync_capture(
                capture_id="cap-shared",
                url="https://x", title="t", platform="general",
                content_type="article",
                captured_at="2026-05-08T10:00:00+00:00",
                user_id=DEFAULT_USER_ID,
            )
            # Same capture_id, different user. With the buggy
            # cross-tenant exists() check this would silently return
            # False without surfacing the conflict. With the
            # tenant-scoped get() check, the insert proceeds and the
            # global PK collision fires — caught + logged + False.
            second = await sync_capture(
                capture_id="cap-shared",
                url="https://x", title="t", platform="general",
                content_type="article",
                captured_at="2026-05-08T10:00:00+00:00",
                user_id=2,
            )
            # Verify the original row is still owned by user 1, and
            # user 2 has no row with this id.
            async with session_scope() as session:
                row1 = await CaptureRepository(session).get(
                    "cap-shared", user_id=DEFAULT_USER_ID,
                )
                row2 = await CaptureRepository(session).get(
                    "cap-shared", user_id=2,
                )
            return first, second, row1, row2

        first, second, row1, row2 = asyncio.run(go())
        assert first is True
        assert second is False  # PK collision logged + swallowed
        assert row1 is not None and row1.user_id == DEFAULT_USER_ID
        assert row2 is None  # user 2 never got a row


# ---- sync_hydration --------------------------------------------------

class TestSyncHydration:
    def test_insert_round_trip(self):
        async def go():
            await _seed_user_and_init()
            await sync_capture(
                capture_id="cap-hyd",
                url="https://x", title="t", platform="instagram",
                content_type="article",
                captured_at="2026-05-08T10:00:00+00:00",
            )
            ok = await sync_hydration(
                capture_id="cap-hyd",
                tier="og_metadata",
                source_payload_json='{"og": {"source": "og"}}',
                hydrated_at="2026-05-08T10:00:01+00:00",
            )
            async with session_scope() as session:
                rows = await HydrationRepository(session).list_by_capture(
                    "cap-hyd", user_id=DEFAULT_USER_ID,
                )
            return ok, rows

        ok, rows = asyncio.run(go())
        assert ok is True
        assert len(rows) == 1
        assert rows[0].tier == "og_metadata"

    def test_orphan_hydration_logs_and_returns_false(self):
        """Hydrating a capture_id that doesn't exist violates the
        FK constraint. The error must be caught, NOT propagated."""
        async def go():
            await _seed_user_and_init()
            return await sync_hydration(
                capture_id="cap-does-not-exist",
                tier="og_metadata",
                source_payload_json='{}',
                hydrated_at="2026-05-08T10:00:01+00:00",
            )

        # SQLite FK enforcement is off by default in older SQLAlchemy
        # versions — the row may insert without an FK error. Either
        # way, the call must not raise.
        result = asyncio.run(go())
        # We don't assert True/False here — depends on FK enforcement —
        # but the call must return cleanly.
        assert result in (True, False)


# ---- sync_enrichment without processed -------------------------------

class TestSyncEnrichmentMetadataOnly:
    def test_enrichment_row_inserted(self, vector_store, stub_embedder):
        async def go():
            await _seed_user_and_init()
            await sync_capture(
                capture_id="cap-enr",
                url="https://x", title="t", platform="general",
                content_type="article",
                captured_at="2026-05-08T10:00:00+00:00",
            )
            ok = await sync_enrichment(
                capture_id="cap-enr",
                summary="A short summary.",
                key_facts_json='["fact 1", "fact 2"]',
                topics=["Kanban", "Agile"],
                entities=[
                    {"label": "Anthropic", "entity_type": "company"},
                    {"label": "Sabya", "entity_type": "person"},
                ],
                model="claude-haiku-4-5-20251001",
                enriched_at="2026-05-08T10:00:02+00:00",
                processed=None,
                embedder=stub_embedder,
                vector_store=vector_store,
            )
            async with session_scope() as session:
                enr = await EnrichmentRepository(session).get_by_capture(
                    "cap-enr", user_id=DEFAULT_USER_ID,
                )
                topic_rows = await TopicRepository(session).list_all()
                entity_rows = await EntityRepository(session).list_all()
            return ok, enr, topic_rows, entity_rows

        ok, enr, topic_rows, entity_rows = asyncio.run(go())
        assert ok is True
        assert enr is not None and enr.summary == "A short summary."
        assert {t.slug for t in topic_rows} == {"kanban", "agile"}
        assert {e.slug for e in entity_rows} == {"anthropic", "sabya"}

    def test_unknown_entity_type_falls_back_to_concept(
        self, vector_store, stub_embedder,
    ):
        """If the LLM emits a weird entity_type ('thing', 'noun', etc.)
        we don't want sync_enrichment to crash. It should fall back
        to 'concept' silently."""
        async def go():
            await _seed_user_and_init()
            await sync_capture(
                capture_id="cap-bad-type",
                url=None, title="t", platform="general",
                content_type="article",
                captured_at="2026-05-08T10:00:00+00:00",
            )
            await sync_enrichment(
                capture_id="cap-bad-type",
                summary="x", key_facts_json="[]",
                entities=[
                    {"label": "Mystery Thing", "entity_type": "not_a_real_type"},
                ],
                model="haiku", enriched_at="2026-05-08T10:00:02+00:00",
                processed=None,
                embedder=stub_embedder, vector_store=vector_store,
            )
            async with session_scope() as session:
                rows = await EntityRepository(session).list_all()
            return rows

        rows = asyncio.run(go())
        assert len(rows) == 1
        assert rows[0].slug == "mystery-thing"
        assert rows[0].entity_type == "concept"

    def test_duplicate_topics_collapse_via_slug(self, vector_store, stub_embedder):
        """'Kanban' and 'kanban' and 'Kanban Method' should collapse to
        either 1 or 2 topic rows by slug normalization (Kanban/kanban
        share a slug; Kanban Method is distinct)."""
        async def go():
            await _seed_user_and_init()
            await sync_capture(
                capture_id="cap-dupe-topics",
                url=None, title="t", platform="general",
                content_type="article",
                captured_at="2026-05-08T10:00:00+00:00",
            )
            await sync_enrichment(
                capture_id="cap-dupe-topics",
                summary="x", key_facts_json="[]",
                topics=["Kanban", "kanban", "Kanban Method"],
                model="haiku", enriched_at="2026-05-08T10:00:02+00:00",
                processed=None,
                embedder=stub_embedder, vector_store=vector_store,
            )
            async with session_scope() as session:
                rows = await TopicRepository(session).list_all()
            return rows

        rows = asyncio.run(go())
        slugs = {r.slug for r in rows}
        # "Kanban" and "kanban" share slug "kanban"; "Kanban Method"
        # is a separate slug "kanban-method".
        assert slugs == {"kanban", "kanban-method"}


# ---- sync_enrichment with processed (full path) ----------------------

class TestSyncEnrichmentFullPath:
    def test_chunks_generated_and_mirrored_to_chroma(
        self, vector_store, stub_embedder,
    ):
        async def go():
            await _seed_user_and_init()
            await sync_capture(
                capture_id="cap-full",
                url="https://example.com/article",
                title="Test Article",
                platform="general",
                content_type="article",
                captured_at="2026-05-08T10:00:00+00:00",
            )
            processed = _ProcessedStub(
                clean_text=(
                    "First paragraph about kanban.\n\n"
                    "Second paragraph about WIP limits.\n\n"
                    "Third paragraph wrapping up."
                ),
                transcript="",
                image_text="",
                timestamp="2026-05-08T10:00:00+00:00",
            )
            await sync_enrichment(
                capture_id="cap-full",
                summary="Three paragraphs about kanban.",
                key_facts_json="[]",
                topics=["Kanban"],
                entities=[],
                model="haiku",
                enriched_at="2026-05-08T10:00:02+00:00",
                processed=processed,
                embedder=stub_embedder,
                vector_store=vector_store,
            )
            # Pull SQL chunks
            async with session_scope() as session:
                sql_chunks = await ChunkRepository(session).list_by_capture(
                    "cap-full", user_id=DEFAULT_USER_ID,
                )
            chroma_count = await vector_store.count(COLLECTION_CHUNKS)
            return sql_chunks, chroma_count

        sql_chunks, chroma_count = asyncio.run(go())
        # 3 article paragraphs + 1 summary = 4 chunks
        assert len(sql_chunks) == 4
        # Source kinds match what we expect
        kinds = {c.source_kind for c in sql_chunks}
        assert "article_paragraph" in kinds
        assert "summary" in kinds
        # Chroma has the same number of vectors
        assert chroma_count == 4
        # All chunks have embeddings
        for c in sql_chunks:
            assert c.embedding is not None
            assert len(c.embedding) > 0  # non-empty BLOB

    def test_topics_attached_to_every_chunk(self, vector_store, stub_embedder):
        """A capture-level topic gets attached to every chunk on the
        capture (per the design note in _sync_topics_and_entities)."""
        async def go():
            await _seed_user_and_init()
            await sync_capture(
                capture_id="cap-tag",
                url=None, title="t", platform="general",
                content_type="article",
                captured_at="2026-05-08T10:00:00+00:00",
            )
            processed = _ProcessedStub(
                clean_text="Para A.\n\nPara B.",
                transcript="",
                image_text="",
                timestamp="2026-05-08T10:00:00+00:00",
            )
            await sync_enrichment(
                capture_id="cap-tag",
                summary="Two-para test.",
                key_facts_json="[]",
                topics=["Kanban", "Agile"],
                entities=[{"label": "Anthropic", "entity_type": "company"}],
                model="haiku",
                enriched_at="2026-05-08T10:00:02+00:00",
                processed=processed,
                embedder=stub_embedder,
                vector_store=vector_store,
            )
            async with session_scope() as session:
                ct_rows = (await session.execute(select(chunk_topics))).fetchall()
                ce_rows = (await session.execute(select(chunk_entities))).fetchall()
                chunk_rows = (await session.execute(select(chunks))).fetchall()
            return chunk_rows, ct_rows, ce_rows

        chunk_rows, ct_rows, ce_rows = asyncio.run(go())
        # Two clean_text paragraphs + one summary = 3 chunks
        assert len(chunk_rows) == 3
        # Each chunk gets every topic attached: 3 chunks * 2 topics = 6 rows
        assert len(ct_rows) == 6
        # Each chunk gets every entity: 3 * 1 = 3 rows
        assert len(ce_rows) == 3

    def test_re_enrichment_does_not_violate_chunk_uniqueness(
        self, vector_store, stub_embedder,
    ):
        """A second sync_enrichment call for the same capture (Phase 5+
        re-enrichment scenario) MUST NOT crash. The chunks table has
        UNIQUE(capture_id, chunk_index); the v1 behavior is to keep
        the original chunks and only refresh the enrichment row +
        topic/entity attachments. Targeted summary-chunk replacement
        is a Phase 5 follow-up."""
        async def go():
            await _seed_user_and_init()
            await sync_capture(
                capture_id="cap-re-enrich",
                url=None, title="t", platform="general",
                content_type="article",
                captured_at="2026-05-08T10:00:00+00:00",
            )
            processed = _ProcessedStub(
                clean_text="Para A.\n\nPara B.",
                transcript="",
                image_text="",
                timestamp="2026-05-08T10:00:00+00:00",
            )
            # First enrichment.
            ok1 = await sync_enrichment(
                capture_id="cap-re-enrich",
                summary="First summary.",
                key_facts_json="[]",
                topics=["Kanban"],
                entities=[],
                model="haiku",
                enriched_at="2026-05-08T10:00:02+00:00",
                processed=processed,
                embedder=stub_embedder,
                vector_store=vector_store,
            )
            async with session_scope() as session:
                first_chunks = await ChunkRepository(session).list_by_capture(
                    "cap-re-enrich", user_id=DEFAULT_USER_ID,
                )
            first_chunk_count = len(first_chunks)
            first_chunk_texts = [c.text for c in first_chunks]
            first_chroma_count = await vector_store.count(COLLECTION_CHUNKS)

            # Second enrichment with a DIFFERENT summary. Without the
            # fix, this would integrity-error on chunk_index 0 already
            # taken and the whole call would partially fail.
            ok2 = await sync_enrichment(
                capture_id="cap-re-enrich",
                summary="Different summary the second time around.",
                key_facts_json='["new fact"]',
                topics=["Kanban", "Agile"],  # added a topic
                entities=[],
                model="haiku",
                enriched_at="2026-05-08T10:00:05+00:00",  # later
                processed=processed,
                embedder=stub_embedder,
                vector_store=vector_store,
            )
            async with session_scope() as session:
                second_chunks = await ChunkRepository(session).list_by_capture(
                    "cap-re-enrich", user_id=DEFAULT_USER_ID,
                )
                topic_rows = await TopicRepository(session).list_all()
                enr = await EnrichmentRepository(session).get_by_capture(
                    "cap-re-enrich", user_id=DEFAULT_USER_ID,
                )
            second_chroma_count = await vector_store.count(COLLECTION_CHUNKS)

            return (
                ok1, ok2,
                first_chunk_count, first_chunk_texts, first_chroma_count,
                second_chunks, topic_rows, enr, second_chroma_count,
            )

        (
            ok1, ok2,
            first_chunk_count, first_chunk_texts, first_chroma_count,
            second_chunks, topic_rows, enr, second_chroma_count,
        ) = asyncio.run(go())

        # Both calls succeed at the boundary level.
        assert ok1 is True
        assert ok2 is True
        # Original chunks preserved — same count, same texts.
        assert len(second_chunks) == first_chunk_count
        assert [c.text for c in second_chunks] == first_chunk_texts
        # Chroma chunks count unchanged.
        assert second_chroma_count == first_chroma_count
        # New topic from second call DID get added to vocabulary.
        topic_slugs = {t.slug for t in topic_rows}
        assert {"kanban", "agile"}.issubset(topic_slugs)
        # Most recent enrichment is the second one (B.5 / repo
        # docstring says ORDER BY enriched_at DESC LIMIT 1).
        assert enr is not None
        assert enr.summary == "Different summary the second time around."

    def test_topics_and_entities_in_chroma_too(
        self, vector_store, stub_embedder,
    ):
        """Topic and entity vectors should appear in their respective
        Chroma collections (B.7 controlled-vocabulary infrastructure)."""
        async def go():
            await _seed_user_and_init()
            await sync_capture(
                capture_id="cap-vocab",
                url=None, title="t", platform="general",
                content_type="article",
                captured_at="2026-05-08T10:00:00+00:00",
            )
            await sync_enrichment(
                capture_id="cap-vocab",
                summary="s", key_facts_json="[]",
                topics=["Kanban", "Agile"],
                entities=[
                    {"label": "Anthropic", "entity_type": "company"},
                    {"label": "Bengaluru", "entity_type": "place"},
                ],
                model="haiku",
                enriched_at="2026-05-08T10:00:02+00:00",
                processed=None,
                embedder=stub_embedder,
                vector_store=vector_store,
            )
            return (
                await vector_store.count(COLLECTION_TOPICS),
                await vector_store.count(COLLECTION_ENTITIES),
            )

        topic_count, entity_count = asyncio.run(go())
        assert topic_count == 2
        assert entity_count == 2


# ---- Defensive: garbage input doesn't crash --------------------------

class TestDefensive:
    def test_empty_topics_and_entities(self, vector_store, stub_embedder):
        async def go():
            await _seed_user_and_init()
            await sync_capture(
                capture_id="cap-empty",
                url=None, title="t", platform="general",
                content_type="article",
                captured_at="2026-05-08T10:00:00+00:00",
            )
            return await sync_enrichment(
                capture_id="cap-empty",
                summary="x", key_facts_json="[]",
                topics=[], entities=[],
                model="haiku",
                enriched_at="2026-05-08T10:00:02+00:00",
                processed=None,
                embedder=stub_embedder, vector_store=vector_store,
            )
        assert asyncio.run(go()) is True

    def test_garbage_topic_strings_skipped(self, vector_store, stub_embedder):
        async def go():
            await _seed_user_and_init()
            await sync_capture(
                capture_id="cap-garbage",
                url=None, title="t", platform="general",
                content_type="article",
                captured_at="2026-05-08T10:00:00+00:00",
            )
            await sync_enrichment(
                capture_id="cap-garbage",
                summary="x", key_facts_json="[]",
                topics=["", "   ", "Real Topic"],  # 2 garbage + 1 real
                entities=[
                    {"label": "Real Entity", "entity_type": "concept"},
                    {"label": "", "entity_type": "concept"},   # skipped
                    {"not_a_dict": True},                       # skipped (wrong shape)
                ],
                model="haiku",
                enriched_at="2026-05-08T10:00:02+00:00",
                processed=None,
                embedder=stub_embedder, vector_store=vector_store,
            )
            async with session_scope() as session:
                tr = await TopicRepository(session).list_all()
                er = await EntityRepository(session).list_all()
            return tr, er

        topic_rows, entity_rows = asyncio.run(go())
        assert len(topic_rows) == 1
        assert topic_rows[0].slug == "real-topic"
        assert len(entity_rows) == 1
        assert entity_rows[0].slug == "real-entity"
