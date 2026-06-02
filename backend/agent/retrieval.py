"""Phase 4 M.2 — RetrievalService.

The hybrid retrieval engine that powers vague-recall search (use case B
from `docs/phase3-design.md`, design locked in
`docs/phase4-vague-recall-design.md`). One query goes in; up to N
candidate captures come out, ranked by a fusion of vector and BM25
scores, diversified so a single capture can't monopolise the result
list.

This module sits below the agent — no LLM calls, no prompt design, no
conversation state. Just retrieval. The `Recaller` agent in M.3 wraps
this with the Sonnet re-rank and the conversational response layer.

Pipeline (per design doc V.1 → V.5):
  1. Embed the query (one shared embedder, same model that built the
     chunks' embeddings at enrichment time).
  2. Run Chroma vector search and SQLite BM25 search in PARALLEL via
     `asyncio.gather`. Both target the same `chunks` table; they just
     score differently. Top-K each, K defaults to 20.
  3. Fuse the two ranked lists with Reciprocal Rank Fusion at k=60.
     Parameter-free, robust as the corpus grows (see V.2 in the design
     doc for the rationale vs weighted sum).
  4. Diversify by capture — walk the fused-ranked list, keep the first
     chunk per capture_id, drop later ones (V.5). This is what prevents
     a long article from filling the candidate slot list with its own
     chunks.
  5. Take the top-N captures (default 6 — V.4). Fetch their parent
     `Capture` rows from SQL so the caller has metadata (url, title,
     captured_at, etc.) without another round-trip.

What we DON'T do here:
  - LLM re-ranking (M.3)
  - Confidence threshold + "no good match" framing (M.3)
  - Conversation-state filters (M.3)
  - Response formatting (M.3 / M.4)

Tenancy: every read filters by `user_id`. Cross-tenant chunk_ids can
never surface — Chroma's `where` filter excludes them client-side and
the BM25 SQL JOIN to `captures` excludes them server-side.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from backend.storage import (
    Capture,
    CaptureRepository,
    Chunk,
    ChunkRepository,
    session_scope,
)
from backend.storage.embedder import Embedder, get_embedder
from backend.storage.vector_store import (
    COLLECTION_CHUNKS,
    VectorStore,
    get_vector_store,
)


logger = logging.getLogger(__name__)


# RRF dampening constant (V.2). 60 is the published default that has
# stayed robust across many corpora; tuning it isn't a knob we want to
# touch without strong evidence. Exposed as a kwarg on `recall()` for
# tests that want to probe the math.
DEFAULT_RRF_K = 60

# Per-ranker top-K (V.4). 20 gives the LLM re-ranker (M.3) enough
# headroom to surface surprise matches without bloating Sonnet's
# context.
DEFAULT_PER_RANKER_TOP_K = 20

# Final cap on capture candidates handed back to the caller (V.4). The
# LLM re-rank pass in M.3 expects ~6 candidates so it can reason about
# which is the actual answer without scanning a long tail.
DEFAULT_TOP_CAPTURES = 6


# ---- Result types ----------------------------------------------------


@dataclass(frozen=True)
class CandidateChunk:
    """One chunk surfaced by retrieval, with fusion math + ranker
    provenance attached so the upstream agent can explain its choices.

    Score convention: `fused_score` is HIGHER-is-better (the RRF sum
    over rankers). `vector_distance` is the raw Chroma cosine distance
    where LOWER-is-better; `bm25_score` is the sign-flipped BM25 score
    from `ChunkRepository.search_by_bm25` where HIGHER-is-better.

    Either `vector_rank` or `bm25_rank` (or both) will be set; never
    both None for a chunk that made it into the fused result."""
    chunk: Chunk
    fused_score: float
    vector_rank: Optional[int] = None
    bm25_rank: Optional[int] = None
    vector_distance: Optional[float] = None
    bm25_score: Optional[float] = None


@dataclass(frozen=True)
class CandidateCapture:
    """One capture surfaced by retrieval, anchored to the chunk of
    that capture that ranked highest. The chunk's text is the snippet
    the agent will show; the capture carries title/url/metadata for
    the result block (U.2)."""
    capture: Capture
    best_chunk: CandidateChunk
    fused_score: float


@dataclass(frozen=True)
class RetrievalResult:
    """Top-N capture candidates for a query, ordered by fused score
    descending. Empty `candidates` means either the query was blank
    or neither ranker produced any hits (genuinely empty corpus / no
    matches at all)."""
    query: str
    candidates: list[CandidateCapture] = field(default_factory=list)


# ---- The service -----------------------------------------------------


class RetrievalService:
    """Hybrid retrieval for vague-recall search.

    Stateless after construction — safe to share across requests. The
    embedder and vector_store are lazy singletons; tests inject stubs
    via the constructor kwargs.
    """

    def __init__(
        self,
        *,
        embedder: Optional[Embedder] = None,
        vector_store: Optional[VectorStore] = None,
    ):
        self._embedder = embedder if embedder is not None else get_embedder()
        self._vector_store = (
            vector_store if vector_store is not None else get_vector_store()
        )

    async def recall(
        self,
        *,
        query: str,
        user_id: int,
        per_ranker_top_k: int = DEFAULT_PER_RANKER_TOP_K,
        top_captures: int = DEFAULT_TOP_CAPTURES,
        rrf_k: int = DEFAULT_RRF_K,
    ) -> RetrievalResult:
        """Run hybrid retrieval. Returns the top-`top_captures` captures
        ranked by RRF-fused score, each anchored to its best chunk.

        Empty or whitespace-only queries return an empty result without
        touching the embedder, Chroma, or SQL. Same for non-positive
        top_captures or per_ranker_top_k — defensive guards against
        bad caller input.

        The parallel `asyncio.gather` over Chroma and BM25 is the
        latency win. Chroma's `query` wraps the (sync) chromadb call
        in `asyncio.to_thread`; BM25 is a real async SQL roundtrip
        via SQLAlchemy. Running them concurrently roughly halves
        retrieval latency on warm caches.
        """
        if not query or not query.strip():
            return RetrievalResult(query=query)
        if per_ranker_top_k <= 0 or top_captures <= 0:
            return RetrievalResult(query=query)

        query_clean = query.strip()

        # The embedder is sync (sentence-transformers is sync). Call it
        # before opening the session so we don't hold the SQL connection
        # while the model runs.
        embedding = self._embedder.embed(query_clean)

        async with session_scope() as session:
            chunk_repo = ChunkRepository(session)
            cap_repo = CaptureRepository(session)

            # ---- Step 2: rankers in parallel ----------------------
            vector_hits, bm25_hits = await asyncio.gather(
                self._vector_store.query(
                    COLLECTION_CHUNKS,
                    embedding=embedding,
                    where={"user_id": user_id},
                    top_k=per_ranker_top_k,
                ),
                chunk_repo.search_by_bm25(
                    query_clean, user_id=user_id, limit=per_ranker_top_k,
                ),
            )

            # ---- Step 3: RRF fusion -------------------------------
            (
                fused_scores,
                vector_rank_by_id,
                bm25_rank_by_id,
                vector_distance_by_id,
                bm25_score_by_id,
                bm25_chunk_by_id,
            ) = _fuse(vector_hits, bm25_hits, rrf_k=rrf_k)

            if not fused_scores:
                return RetrievalResult(query=query)

            # Rank chunk ids by fused score (highest first). Stable
            # secondary sort by chunk id keeps ordering deterministic
            # when two chunks tie.
            ranked_chunk_ids = sorted(
                fused_scores.keys(),
                key=lambda cid: (-fused_scores[cid], cid),
            )

            # ---- Hydrate chunks not already in BM25 hits ----------
            # BM25 gives us full Chunk rows; vectors give us just IDs.
            # Round-trip to SQL for the vector-only chunks in one go.
            missing_ids = [
                cid for cid in ranked_chunk_ids if cid not in bm25_chunk_by_id
            ]
            chunk_by_id: dict[int, Chunk] = dict(bm25_chunk_by_id)
            if missing_ids:
                hydrated = await chunk_repo.get_by_ids(
                    missing_ids, user_id=user_id,
                )
                for ch in hydrated:
                    chunk_by_id[ch.id] = ch

            # ---- Step 4: diversify by capture ---------------------
            seen_captures: set[str] = set()
            best_chunk_per_capture: list[CandidateChunk] = []
            for cid in ranked_chunk_ids:
                ch = chunk_by_id.get(cid)
                if ch is None:
                    # Chunk filtered out by tenant check in get_by_ids
                    # (Chroma's `where` should have caught this, but
                    # defense in depth). Skip silently.
                    continue
                if ch.capture_id in seen_captures:
                    continue
                seen_captures.add(ch.capture_id)
                best_chunk_per_capture.append(CandidateChunk(
                    chunk=ch,
                    fused_score=fused_scores[cid],
                    vector_rank=vector_rank_by_id.get(cid),
                    bm25_rank=bm25_rank_by_id.get(cid),
                    vector_distance=vector_distance_by_id.get(cid),
                    bm25_score=bm25_score_by_id.get(cid),
                ))
                if len(best_chunk_per_capture) >= top_captures:
                    break

            # ---- Step 5: hydrate parent Capture rows --------------
            # Done one-by-one because we cap at top_captures (default
            # 6); batching would be premature. If the cap grows large
            # later, swap for a `list_by_ids` on CaptureRepository.
            candidates: list[CandidateCapture] = []
            for cand_chunk in best_chunk_per_capture:
                cap = await cap_repo.get(
                    cand_chunk.chunk.capture_id, user_id=user_id,
                )
                if cap is None:
                    # FK-violated or another tenant — shouldn't happen
                    # given the upstream tenant filters, but be quiet
                    # and skip.
                    logger.debug(
                        "recall: chunk %d points at missing capture %s",
                        cand_chunk.chunk.id, cand_chunk.chunk.capture_id,
                    )
                    continue
                candidates.append(CandidateCapture(
                    capture=cap,
                    best_chunk=cand_chunk,
                    fused_score=cand_chunk.fused_score,
                ))

        return RetrievalResult(query=query, candidates=candidates)


# ---- Pure fusion math (unit-testable in isolation) ------------------


def _fuse(
    vector_hits: list,
    bm25_hits: list,
    *,
    rrf_k: int,
) -> tuple[
    dict[int, float],   # fused_scores by chunk_id
    dict[int, int],     # vector_rank_by_id
    dict[int, int],     # bm25_rank_by_id
    dict[int, float],   # vector_distance_by_id
    dict[int, float],   # bm25_score_by_id
    dict[int, Chunk],   # bm25_chunk_by_id (full Chunk dataclasses we already loaded)
]:
    """Apply Reciprocal Rank Fusion to the two ranker outputs.

    RRF formula (V.2):
        score(chunk) = Σ over rankers of 1 / (rrf_k + rank_in_that_ranker)

    Ranks are 1-based; a chunk appearing in only one ranker contributes
    one term, a chunk appearing in both contributes two and so wins
    over solo appearances at the same rank. The dampening constant
    `rrf_k` (60 by default) flattens the score gap between rank 1 and
    rank 2 — that's what makes the fusion robust to either ranker
    having a confidently-wrong top hit.

    Returns parallel lookups so the caller can attach provenance to
    each result without redoing math.
    """
    fused_scores: dict[int, float] = defaultdict(float)
    vector_rank_by_id: dict[int, int] = {}
    bm25_rank_by_id: dict[int, int] = {}
    vector_distance_by_id: dict[int, float] = {}
    bm25_score_by_id: dict[int, float] = {}
    bm25_chunk_by_id: dict[int, Chunk] = {}

    for rank, hit in enumerate(vector_hits, start=1):
        # VectorHit.id is the SQL chunk.id serialized as a string by
        # the sync_enrichment write path. Parse defensively — a Chroma
        # row that somehow has a non-int id gets skipped rather than
        # crashing retrieval.
        try:
            cid = int(hit.id)
        except (TypeError, ValueError):
            logger.warning(
                "recall: vector hit with non-integer id=%r — skipping",
                hit.id,
            )
            continue
        fused_scores[cid] += 1.0 / (rrf_k + rank)
        vector_rank_by_id[cid] = rank
        vector_distance_by_id[cid] = float(hit.distance)

    for rank, scored in enumerate(bm25_hits, start=1):
        cid = scored.chunk.id
        fused_scores[cid] += 1.0 / (rrf_k + rank)
        bm25_rank_by_id[cid] = rank
        bm25_score_by_id[cid] = float(scored.score)
        bm25_chunk_by_id[cid] = scored.chunk

    return (
        dict(fused_scores),
        vector_rank_by_id,
        bm25_rank_by_id,
        vector_distance_by_id,
        bm25_score_by_id,
        bm25_chunk_by_id,
    )


__all__ = [
    "CandidateChunk",
    "CandidateCapture",
    "RetrievalResult",
    "RetrievalService",
    "DEFAULT_RRF_K",
    "DEFAULT_PER_RANKER_TOP_K",
    "DEFAULT_TOP_CAPTURES",
]
