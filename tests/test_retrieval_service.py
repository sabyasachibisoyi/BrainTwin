"""Tests for Phase 4 M.2 — RetrievalService.

Covers (per the milestone scope in docs/phase4-vague-recall-design.md M.2):

  - Empty / whitespace query short-circuits to an empty result without
    touching the embedder or any store.
  - Non-positive top_k / top_captures short-circuit similarly.
  - Vector-only hits (BM25 empty) produce a clean ranking with
    `bm25_rank` / `bm25_score` = None on every CandidateChunk.
  - BM25-only hits (vector empty) produce a clean ranking with
    `vector_rank` / `vector_distance` = None.
  - RRF math: a chunk in BOTH rankers at modest ranks beats a chunk in
    only ONE ranker at rank 1 (the central guarantee of RRF).
  - Per-capture diversification: when one capture has two chunks both
    ranked, only the higher one survives into `candidates`.
  - `top_captures` cap applied after diversification.
  - Tenant isolation: cross-tenant chunks never surface even when both
    rankers return them (defense in depth — Chroma's `where` should
    catch this, but we test the SQL layer's behavior too).
  - Pure `_fuse()` math in isolation with crafted ranker outputs.

Stubs the embedder and vector_store so tests don't need
sentence-transformers loaded or a running Chroma — only the SQL +
FTS5 path is exercised end-to-end. The fusion math runs on real
inputs from both halves so the pipeline is genuinely tested.

Run with: pytest tests/test_retrieval_service.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from backend.agent.retrieval import (  # noqa: E402
    CandidateCapture,
    DEFAULT_RRF_K,
    RetrievalResult,
    RetrievalService,
    _fuse,
)
from backend.storage import (  # noqa: E402
    CaptureRepository,
    ChunkInsert,
    ChunkRepository,
    UserRepository,
    init_db,
    session_scope,
)
from backend.storage import db as db_module  # noqa: E402
from backend.storage.embedder import EMBEDDING_DIM  # noqa: E402
from backend.storage.models import Capture  # noqa: E402
from backend.storage.vector_store import VectorHit  # noqa: E402


# ---- Fixtures --------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_engine(monkeypatch):
    """Fresh in-memory DB per test. Same pattern as
    tests/test_chunks_fts.py."""
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_session_factory", None)
    yield


# ---- Stubs -----------------------------------------------------------

class _StubEmbedder:
    """Deterministic, no model download. The actual vector content
    doesn't matter for any test here — the stub vector_store ignores
    it. We just need `embed()` to return a 384-d ndarray of the right
    dtype so the contract stays honest."""

    @property
    def model_name(self) -> str:
        return "stub"

    @property
    def dim(self) -> int:
        return EMBEDDING_DIM

    def embed(self, text: str) -> np.ndarray:
        seed = abs(hash(text)) % (2**32)
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(EMBEDDING_DIM, dtype=np.float32)
        return (v / max(float(np.linalg.norm(v)), 1e-9)).astype(np.float32)


class _StubVectorStore:
    """Returns whatever list of VectorHit it was constructed with,
    truncated to the requested top_k. Records all calls so tests can
    assert on tenancy filters being passed correctly."""

    def __init__(self, hits: list[VectorHit] | None = None):
        self._hits = list(hits or [])
        self.calls: list[dict] = []

    async def query(
        self,
        collection: str,
        *,
        embedding,
        where=None,
        top_k: int = 20,
    ):
        self.calls.append({
            "collection": collection, "where": where, "top_k": top_k,
        })
        return list(self._hits[:top_k])


# ---- Helpers ---------------------------------------------------------

def _vh(chunk_id: int, distance: float, *, user_id: int = 1) -> VectorHit:
    """Tiny VectorHit factory — Chroma stores chunk_id as a string."""
    return VectorHit(
        id=str(chunk_id),
        distance=distance,
        metadata={"user_id": user_id, "capture_id": "cap"},
        document=None,
    )


async def _seed_user_and_captures(captures: list[tuple[str, int, str]]) -> None:
    """Seed users 1 + 2 and the given captures. Each tuple is
    (capture_id, user_id, title)."""
    await init_db()
    async with session_scope() as session:
        users = UserRepository(session)
        await users.create(email="sabya@example.com", display_name="Sabya", user_id=1)
        await users.create(email="other@example.com", display_name="Other", user_id=2)
        cap_repo = CaptureRepository(session)
        for cid, uid, title in captures:
            await cap_repo.create(Capture(
                id=cid,
                user_id=uid,
                url=f"https://example.com/{cid}",
                title=title,
                platform="general",
                content_type="article",
                captured_at="2026-05-08T10:00:00+00:00",
                dwell_seconds=10,
                raw_metadata_json=None,
                clean_text="body",
            ))


async def _seed_chunks(
    rows: list[tuple[str, int, str]],
) -> list[int]:
    """Seed chunks (capture_id, chunk_index, text). Returns the assigned
    chunk ids in order."""
    async with session_scope() as session:
        repo = ChunkRepository(session)
        ids = await repo.create_many([
            ChunkInsert(
                capture_id=cid,
                chunk_index=idx,
                text=text,
                source_kind="article_paragraph",
                embedding=None,
            ) for cid, idx, text in rows
        ])
    return ids


# ---- Pure fusion math ------------------------------------------------

class TestFuseMath:
    """Direct unit tests for `_fuse` — no DB, no stubs."""

    def test_chunk_in_both_rankers_beats_chunk_in_one(self):
        # Chunk 99 is at rank 3 in BOTH rankers.
        # Chunk 11 is at rank 1 in vector only.
        # Chunk 22 is at rank 1 in BM25 only.
        # RRF guarantee: the chunk in both should win.
        class _StubChunk:
            def __init__(self, cid): self.id = cid

        class _StubScored:
            def __init__(self, cid, score): self.chunk = _StubChunk(cid); self.score = score

        v_hits = [_vh(11, 0.05), _vh(55, 0.10), _vh(99, 0.20)]
        b_hits = [_StubScored(22, 5.0), _StubScored(77, 4.0), _StubScored(99, 3.0)]
        (
            scores,
            v_ranks,
            b_ranks,
            v_dists,
            b_scores,
            b_chunks,
        ) = _fuse(v_hits, b_hits, rrf_k=DEFAULT_RRF_K)

        # 99 in both at rank 3 → score = 2 × 1/63 ≈ 0.0317
        # 11, 22 each in one ranker at rank 1 → score = 1/61 ≈ 0.0164
        assert scores[99] > scores[11]
        assert scores[99] > scores[22]
        assert v_ranks[99] == 3 and b_ranks[99] == 3
        assert v_ranks.get(22) is None
        assert b_ranks.get(11) is None
        # Provenance attached for the ones we DO see in each ranker.
        assert v_dists[11] == pytest.approx(0.05)
        assert b_scores[22] == pytest.approx(5.0)
        # The BM25-loaded chunks come through for downstream hydration.
        assert set(b_chunks.keys()) == {22, 77, 99}

    def test_single_ranker_preserves_order(self):
        class _StubChunk:
            def __init__(self, cid): self.id = cid

        class _StubScored:
            def __init__(self, cid, score): self.chunk = _StubChunk(cid); self.score = score

        b_hits = [_StubScored(1, 5.0), _StubScored(2, 4.0), _StubScored(3, 3.0)]
        scores, _, b_ranks, _, _, _ = _fuse([], b_hits, rrf_k=DEFAULT_RRF_K)
        # Higher BM25 rank → higher fused score (lower 1/(k+rank))
        assert scores[1] > scores[2] > scores[3]
        assert b_ranks == {1: 1, 2: 2, 3: 3}

    def test_non_integer_vector_id_skipped(self):
        # Defense in depth: a Chroma row with a malformed id shouldn't
        # take down retrieval.
        bad_hit = VectorHit(id="not-an-int", distance=0.1, metadata={}, document=None)
        good_hit = _vh(42, 0.2)
        scores, _, _, _, _, _ = _fuse([bad_hit, good_hit], [], rrf_k=DEFAULT_RRF_K)
        assert 42 in scores
        assert "not-an-int" not in scores  # didn't crash, didn't pollute


# ---- Empty-query short circuit ---------------------------------------

class TestEmptyQuery:
    @pytest.fixture
    def service(self):
        return RetrievalService(
            embedder=_StubEmbedder(),
            vector_store=_StubVectorStore(),
        )

    def test_empty_string_returns_empty(self, service):
        r = asyncio.run(service.recall(query="", user_id=1))
        assert isinstance(r, RetrievalResult)
        assert r.candidates == []
        assert r.query == ""

    def test_whitespace_returns_empty(self, service):
        r = asyncio.run(service.recall(query="   \t\n  ", user_id=1))
        assert r.candidates == []

    def test_zero_top_k_returns_empty(self, service):
        r = asyncio.run(service.recall(
            query="anything", user_id=1, per_ranker_top_k=0,
        ))
        assert r.candidates == []

    def test_zero_top_captures_returns_empty(self, service):
        r = asyncio.run(service.recall(
            query="anything", user_id=1, top_captures=0,
        ))
        assert r.candidates == []


# ---- Real pipeline end-to-end (stub vector + real BM25) --------------

class TestRecallPipeline:
    """End-to-end recall against real SQL + FTS5, with the vector
    side stubbed so we control which IDs Chroma "returns"."""

    def test_vector_only_path(self):
        """No BM25 hits (query doesn't match any chunk text). Vector
        results carry the result alone."""
        async def go():
            await _seed_user_and_captures([("cap-a", 1, "Article A")])
            chunk_ids = await _seed_chunks([
                ("cap-a", 0, "alpha bravo charlie"),
                ("cap-a", 1, "delta echo foxtrot"),
            ])
            # Vector ranks chunk 1 (delta…) first.
            vs = _StubVectorStore([_vh(chunk_ids[1], 0.05),
                                   _vh(chunk_ids[0], 0.30)])
            svc = RetrievalService(embedder=_StubEmbedder(), vector_store=vs)
            # Query that won't match any chunk text via BM25.
            return await svc.recall(query="nonexistenttoken", user_id=1)

        r = asyncio.run(go())
        # Both chunks belong to one capture — diversification collapses
        # to one CandidateCapture.
        assert len(r.candidates) == 1
        cand = r.candidates[0]
        assert cand.capture.id == "cap-a"
        assert cand.best_chunk.vector_rank == 1
        assert cand.best_chunk.bm25_rank is None
        assert cand.best_chunk.bm25_score is None
        assert cand.best_chunk.vector_distance == pytest.approx(0.05)
        # delta-echo chunk wins because vector ranked it first
        assert cand.best_chunk.chunk.text == "delta echo foxtrot"

    def test_bm25_only_path(self):
        """No vector hits (stub returns nothing). BM25 alone surfaces
        the matching chunk."""
        async def go():
            await _seed_user_and_captures([("cap-b", 1, "Article B")])
            chunk_ids = await _seed_chunks([
                ("cap-b", 0, "Tamasha is a Bollywood film"),
                ("cap-b", 1, "Generic content with no rare words"),
            ])
            vs = _StubVectorStore([])  # vector returns nothing
            svc = RetrievalService(embedder=_StubEmbedder(), vector_store=vs)
            return await svc.recall(query="Tamasha", user_id=1)

        r = asyncio.run(go())
        assert len(r.candidates) == 1
        cand = r.candidates[0]
        assert cand.best_chunk.bm25_rank == 1
        assert cand.best_chunk.vector_rank is None
        assert cand.best_chunk.vector_distance is None
        assert "Tamasha" in cand.best_chunk.chunk.text

    def test_hybrid_fusion_lifts_chunk_in_both_rankers(self):
        """The central RRF guarantee: a chunk surfaced by BOTH rankers
        outranks chunks surfaced by only one, even when their solo
        ranks were higher."""
        async def go():
            await _seed_user_and_captures([
                ("cap-x", 1, "X"),
                ("cap-y", 1, "Y"),
                ("cap-z", 1, "Z"),
            ])
            chunk_ids = await _seed_chunks([
                ("cap-x", 0, "kanban WIP limits"),       # ID[0]
                ("cap-y", 0, "team size considerations"), # ID[1]
                ("cap-z", 0, "unrelated noise"),          # ID[2]
            ])
            # Vector ranking: cap-z chunk first, cap-x chunk third.
            vs = _StubVectorStore([
                _vh(chunk_ids[2], 0.05),  # cap-z rank 1
                _vh(chunk_ids[1], 0.20),  # cap-y rank 2
                _vh(chunk_ids[0], 0.30),  # cap-x rank 3
            ])
            svc = RetrievalService(embedder=_StubEmbedder(), vector_store=vs)
            # BM25 with "kanban" matches cap-x chunk strongly.
            return await svc.recall(query="kanban WIP", user_id=1)

        r = asyncio.run(go())
        # cap-x appears in both rankers → wins fusion despite cap-z
        # being vector rank 1.
        assert r.candidates[0].capture.id == "cap-x"
        assert r.candidates[0].best_chunk.vector_rank == 3
        assert r.candidates[0].best_chunk.bm25_rank == 1

    def test_diversification_one_chunk_per_capture(self):
        """A capture with multiple matching chunks contributes one
        CandidateCapture — the chunk with the highest fused score."""
        async def go():
            await _seed_user_and_captures([("cap-long", 1, "Long article")])
            chunk_ids = await _seed_chunks([
                ("cap-long", 0, "para one talks about kanban"),
                ("cap-long", 1, "para two talks about kanban too"),
                ("cap-long", 2, "para three is unrelated"),
            ])
            # Vector returns all three chunks from the same capture
            # in rank order.
            vs = _StubVectorStore([
                _vh(chunk_ids[1], 0.05),  # rank 1
                _vh(chunk_ids[0], 0.10),  # rank 2
                _vh(chunk_ids[2], 0.20),  # rank 3
            ])
            svc = RetrievalService(embedder=_StubEmbedder(), vector_store=vs)
            return await svc.recall(query="kanban", user_id=1)

        r = asyncio.run(go())
        # One capture in candidates.
        assert len(r.candidates) == 1
        # And the chunk attached is the highest-ranked one.
        assert r.candidates[0].best_chunk.chunk.chunk_index == 1

    def test_top_captures_cap_applied(self):
        async def go():
            caps = [(f"cap-{i}", 1, f"Article {i}") for i in range(10)]
            await _seed_user_and_captures(caps)
            chunk_ids = await _seed_chunks([
                (f"cap-{i}", 0, f"capture {i} body") for i in range(10)
            ])
            # Vector returns all 10 chunks in order.
            vs = _StubVectorStore([
                _vh(chunk_ids[i], 0.05 + i * 0.01) for i in range(10)
            ])
            svc = RetrievalService(embedder=_StubEmbedder(), vector_store=vs)
            return await svc.recall(query="body", user_id=1, top_captures=3)

        r = asyncio.run(go())
        assert len(r.candidates) == 3

    def test_tenant_isolation(self):
        """Even when the stub vector_store returns a chunk owned by
        user 2, user 1's recall must not surface it. The SQL-side
        tenant filter in get_by_ids is the safety net here."""
        async def go():
            await _seed_user_and_captures([
                ("cap-mine", 1, "Mine"),
                ("cap-theirs", 2, "Theirs"),
            ])
            chunk_ids = await _seed_chunks([
                ("cap-mine", 0, "my secret note"),
                ("cap-theirs", 0, "their secret note"),
            ])
            # Vector returns BOTH chunks (simulating a bug or a bypass
            # of Chroma's where filter).
            vs = _StubVectorStore([
                _vh(chunk_ids[1], 0.05, user_id=2),  # theirs first
                _vh(chunk_ids[0], 0.10, user_id=1),
            ])
            svc = RetrievalService(embedder=_StubEmbedder(), vector_store=vs)
            r = await svc.recall(query="secret note", user_id=1)
            return r, vs

        r, vs = asyncio.run(go())
        # Only my capture made it through.
        capture_ids = {c.capture.id for c in r.candidates}
        assert "cap-theirs" not in capture_ids
        assert "cap-mine" in capture_ids
        # And the vector store was queried with the right tenant filter.
        assert vs.calls[0]["where"] == {"user_id": 1}

    def test_vector_store_is_called_with_correct_args(self):
        """Plumbing check — top_k forwarded, collection name correct,
        tenant filter set."""
        async def go():
            await _seed_user_and_captures([("cap-x", 1, "X")])
            chunk_ids = await _seed_chunks([("cap-x", 0, "body")])
            vs = _StubVectorStore([_vh(chunk_ids[0], 0.05)])
            svc = RetrievalService(embedder=_StubEmbedder(), vector_store=vs)
            await svc.recall(
                query="body", user_id=1, per_ranker_top_k=5,
            )
            return vs.calls

        calls = asyncio.run(go())
        assert len(calls) == 1
        assert calls[0]["collection"] == "chunks"
        assert calls[0]["top_k"] == 5
        assert calls[0]["where"] == {"user_id": 1}
