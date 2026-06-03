"""Tests for Phase 4 M.3 — Recaller.

Run with: pytest tests/test_recaller.py -v

Covers (per docs/phase4-vague-recall-design.md M.3 + the multi-turn
flow fix):

  - Empty / whitespace query short-circuits to no_match without
    calling retrieval or the LLM.
  - First-turn flow: fresh retrieval + Sonnet rerank → ranked response.
  - Conversation_id is minted when missing and preserved when given.
  - Refinement turn (turn 2 with a conversation_id): uses
    prior.last_candidates and does NOT call retrieval again.
  - Anchor query stays stable across refinement turns.
  - When a refinement turn empties the filtered pool, falls back to
    fresh retrieval and resets the anchor (pivot detection).
  - Empty retrieval result → no_match response with no LLM call.
  - Low confidence from the LLM → no_match + closest-miss courtesy
    response (which uses a second LLM call).
  - LLM transient / permanent / malformed failures degrade gracefully
    to a rule-based rerank, still returning a usable response.
  - Conversation TTL eviction is honoured.
  - Summaries fetched in one bulk call are threaded through to both
    the rerank prompt and the result blocks.
  - Sonnet response shape validation rejects malformed JSON.

Stubs both RetrievalService and LLMClient so tests run offline without
SQL, Chroma, or the Anthropic SDK loaded. Summary hydration is also
monkey-patched by default; the dedicated summary tests opt back in to
exercise the real path through EnrichmentRepository (in-memory SQLite).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from backend.agent.recaller import (  # noqa: E402
    DEFAULT_CONFIDENCE_THRESHOLD,
    ConversationStore,
    RankedResult,
    RecallResponse,
    Recaller,
    _ConversationEntry,
)
from backend.agent.retrieval import (  # noqa: E402
    CandidateCapture,
    CandidateChunk,
    RetrievalResult,
)
from backend.knowledge.llm_client import (  # noqa: E402
    MalformedResponseError,
    PermanentLLMError,
    TransientLLMError,
)
from backend.storage import (  # noqa: E402
    CaptureRepository,
    EnrichmentRepository,
    UserRepository,
    init_db,
    session_scope,
)
from backend.storage import db as db_module  # noqa: E402
from backend.storage.models import Capture, Chunk  # noqa: E402


# ---- Stubs -----------------------------------------------------------

class _StubRetrievalService:
    """Mimics RetrievalService.recall — returns canned candidates per
    query, records every call so tests can assert "retrieval was/was
    not called" without standing up Chroma + SQL."""

    def __init__(
        self,
        candidates_by_query: dict[str, list[CandidateCapture]] | None = None,
    ):
        self._by_query = candidates_by_query or {}
        self.calls: list[dict] = []

    async def recall(
        self,
        *,
        query: str,
        user_id: int,
        top_captures: int = 6,
        **_: Any,
    ) -> RetrievalResult:
        self.calls.append({
            "query": query,
            "user_id": user_id,
            "top_captures": top_captures,
        })
        return RetrievalResult(
            query=query,
            candidates=list(self._by_query.get(query, [])),
        )


class _StubLLMClient:
    """Mimics LLMClient.complete_json — returns or raises preconfigured
    responses on successive calls. Each item in `responses` is either
    a dict (returned) or an Exception (raised)."""

    def __init__(self, responses: list[Any]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_tokens: int = 1024,
    ) -> dict:
        self.calls.append({
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "model": model,
            "max_tokens": max_tokens,
        })
        if not self._responses:
            raise AssertionError(
                "StubLLMClient ran out of responses — test under-supplied them"
            )
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


# ---- Helpers ---------------------------------------------------------

def _make_candidate(
    *,
    capture_id: str,
    title: str | None = None,
    url: str | None = None,
    platform: str = "general",
    fused_score: float = 0.025,
    vector_rank: int | None = 1,
    bm25_rank: int | None = 1,
    chunk_text: str = "matching body text",
) -> CandidateCapture:
    """Build a fully-populated CandidateCapture for tests."""
    capture = Capture(
        id=capture_id,
        user_id=1,
        url=url or f"https://example.com/{capture_id}",
        title=title or f"Article {capture_id}",
        platform=platform,
        content_type="article",
        captured_at="2026-05-08T10:00:00+00:00",
        dwell_seconds=42,
        raw_metadata_json=None,
        clean_text=chunk_text,
    )
    chunk = Chunk(
        id=abs(hash(capture_id)) % 100000,
        capture_id=capture_id,
        chunk_index=0,
        text=chunk_text,
        source_kind="article_paragraph",
        embedding=None,
    )
    cc = CandidateChunk(
        chunk=chunk,
        fused_score=fused_score,
        vector_rank=vector_rank,
        bm25_rank=bm25_rank,
        vector_distance=0.1 if vector_rank else None,
        bm25_score=4.5 if bm25_rank else None,
    )
    return CandidateCapture(capture=capture, best_chunk=cc, fused_score=fused_score)


def _good_rerank(capture_ids: list[str], top_confidence: float = 0.85) -> dict:
    """A well-formed Sonnet rerank response for the given candidates."""
    ranked = []
    for i, cid in enumerate(capture_ids):
        # Top result gets the supplied confidence; the rest fade.
        conf = top_confidence if i == 0 else max(0.1, top_confidence - 0.15 * (i + 1))
        ranked.append({
            "capture_id": cid,
            "confidence": conf,
            "why_this_matches": f"matches because of reason {i + 1}",
        })
    return {
        "ranked_results": ranked,
        "brief_answer": f"I think you're remembering capture {capture_ids[0]}.",
        "no_match": top_confidence < DEFAULT_CONFIDENCE_THRESHOLD,
    }


# ---- Fixtures --------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_engine(monkeypatch):
    """Reset SQL engine singletons between tests so the in-memory DB
    starts fresh each time."""
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_session_factory", None)
    yield


@pytest.fixture
def no_summaries(monkeypatch):
    """Most tests don't care about summaries — stub _fetch_summaries
    to return {} so they don't reach for SQL. Dedicated summary tests
    don't use this fixture."""
    async def _empty(self, candidates, user_id):
        return {}
    monkeypatch.setattr(Recaller, "_fetch_summaries", _empty)


def _make_recaller(
    *,
    retrieval: _StubRetrievalService,
    llm: _StubLLMClient,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    conversation_store: ConversationStore | None = None,
) -> Recaller:
    return Recaller(
        retrieval=retrieval,  # type: ignore[arg-type]
        llm_client=llm,  # type: ignore[arg-type]
        confidence_threshold=confidence_threshold,
        conversation_store=conversation_store or ConversationStore(),
    )


# ---- Empty / whitespace short-circuit --------------------------------

class TestEmptyQuery:
    def test_empty_string_returns_no_match_without_calls(self, no_summaries):
        retrieval = _StubRetrievalService()
        llm = _StubLLMClient([])
        r = _make_recaller(retrieval=retrieval, llm=llm)

        response = asyncio.run(r.recall(query="", user_id=1))
        assert response.no_match is True
        assert response.results == []
        assert response.conversation_id  # uuid was minted
        assert retrieval.calls == []  # never reached retrieval
        assert llm.calls == []  # never reached LLM

    def test_whitespace_returns_no_match_without_calls(self, no_summaries):
        retrieval = _StubRetrievalService()
        llm = _StubLLMClient([])
        r = _make_recaller(retrieval=retrieval, llm=llm)

        response = asyncio.run(r.recall(query="   \t\n ", user_id=1))
        assert response.no_match is True
        assert retrieval.calls == []
        assert llm.calls == []


# ---- First-turn happy path -------------------------------------------

class TestFirstTurn:
    def test_fresh_retrieval_and_rerank(self, no_summaries):
        cands = [
            _make_candidate(capture_id="cap-a", title="The kanban article"),
            _make_candidate(capture_id="cap-b", title="Spotify squads"),
        ]
        retrieval = _StubRetrievalService({"kanban article": cands})
        llm = _StubLLMClient([_good_rerank(["cap-a", "cap-b"])])
        r = _make_recaller(retrieval=retrieval, llm=llm)

        response = asyncio.run(r.recall(query="kanban article", user_id=1))

        # Retrieval ran exactly once with the right query + user.
        assert len(retrieval.calls) == 1
        assert retrieval.calls[0]["query"] == "kanban article"
        assert retrieval.calls[0]["user_id"] == 1

        # LLM rerank ran exactly once.
        assert len(llm.calls) == 1
        assert "kanban article" in llm.calls[0]["user_prompt"]

        # Response shape.
        assert response.no_match is False
        assert response.confidence == pytest.approx(0.85)
        assert len(response.results) == 2
        assert response.results[0].capture_id == "cap-a"
        assert response.results[0].title == "The kanban article"
        assert response.answer.startswith("I think you're remembering")

    def test_conversation_id_minted_when_missing(self, no_summaries):
        cands = [_make_candidate(capture_id="cap-a")]
        retrieval = _StubRetrievalService({"q": cands})
        llm = _StubLLMClient([_good_rerank(["cap-a"])])
        r = _make_recaller(retrieval=retrieval, llm=llm)

        response = asyncio.run(r.recall(query="q", user_id=1))
        assert response.conversation_id  # uuid

    def test_conversation_id_preserved_when_given(self, no_summaries):
        cands = [_make_candidate(capture_id="cap-a")]
        retrieval = _StubRetrievalService({"q": cands})
        llm = _StubLLMClient([_good_rerank(["cap-a"])])
        r = _make_recaller(retrieval=retrieval, llm=llm)

        response = asyncio.run(r.recall(
            query="q", user_id=1, conversation_id="given-id"
        ))
        assert response.conversation_id == "given-id"

    def test_result_block_carries_capture_metadata(self, no_summaries):
        cands = [_make_candidate(
            capture_id="cap-a",
            url="https://hindustantimes.com/x",
            title="HSR Layout rents",
            platform="chrome",
        )]
        retrieval = _StubRetrievalService({"rents": cands})
        llm = _StubLLMClient([_good_rerank(["cap-a"], top_confidence=0.9)])
        r = _make_recaller(retrieval=retrieval, llm=llm)

        response = asyncio.run(r.recall(query="rents", user_id=1))
        block = response.results[0]
        assert isinstance(block, RankedResult)
        assert block.original_url == "https://hindustantimes.com/x"
        assert block.source_domain == "hindustantimes.com"
        assert block.client == "chrome"
        assert block.dwell_time_seconds == 42
        assert 0.0 <= block.confidence <= 1.0


# ---- Multi-turn refinement flow --------------------------------------

class TestRefinementFlow:
    def test_second_turn_does_not_call_retrieval(self, no_summaries):
        """Crux of the U.3 fix: a follow-up turn uses prior candidates
        and never re-runs RetrievalService."""
        cands = [
            _make_candidate(capture_id="cap-a", chunk_text="kanban WIP limits"),
            _make_candidate(capture_id="cap-b", chunk_text="kanban team size"),
        ]
        retrieval = _StubRetrievalService({"kanban": cands})
        llm = _StubLLMClient([
            _good_rerank(["cap-a", "cap-b"]),  # turn 1
            _good_rerank(["cap-b"]),            # turn 2
        ])
        r = _make_recaller(retrieval=retrieval, llm=llm)

        # Turn 1.
        t1 = asyncio.run(r.recall(query="kanban", user_id=1))
        assert len(retrieval.calls) == 1
        conv_id = t1.conversation_id

        # Turn 2 — refinement.
        t2 = asyncio.run(r.recall(
            query="team size", user_id=1, conversation_id=conv_id,
        ))
        # Retrieval count unchanged — refinement reused the prior pool.
        assert len(retrieval.calls) == 1
        # LLM called twice (once per turn).
        assert len(llm.calls) == 2
        # Turn 2's rerank received the refinement query.
        assert "team size" in llm.calls[1]["user_prompt"]
        assert t2.no_match is False

    def test_anchor_query_preserved_across_refinement(self, no_summaries):
        """`last_query` in the conversation state must stay equal to
        the FIRST turn's query throughout a refinement chain."""
        cands = [_make_candidate(capture_id="cap-a", chunk_text="kanban team size")]
        retrieval = _StubRetrievalService({"kanban": cands})
        llm = _StubLLMClient([
            _good_rerank(["cap-a"]),  # turn 1
            _good_rerank(["cap-a"]),  # turn 2
        ])
        store = ConversationStore()
        r = _make_recaller(retrieval=retrieval, llm=llm, conversation_store=store)

        t1 = asyncio.run(r.recall(query="kanban", user_id=1))
        asyncio.run(r.recall(
            query="team size", user_id=1, conversation_id=t1.conversation_id,
        ))

        entry = store.get(t1.conversation_id)
        assert entry is not None
        assert entry.last_query == "kanban"  # anchor unchanged
        assert entry.accumulated_filters == ["team size"]

    def test_empty_filter_pool_falls_back_to_fresh_retrieval(self, no_summaries):
        """When the refinement filter eliminates every prior candidate
        (the user pivoted to a new topic), we re-run retrieval with
        the new query as the anchor."""
        kanban_cands = [_make_candidate(
            capture_id="cap-k", chunk_text="kanban WIP limits",
        )]
        new_topic_cands = [_make_candidate(
            capture_id="cap-h", chunk_text="HSR Layout rent",
        )]
        retrieval = _StubRetrievalService({
            "kanban": kanban_cands,
            "HSR Layout": new_topic_cands,
        })
        llm = _StubLLMClient([
            _good_rerank(["cap-k"]),  # turn 1 (kanban)
            _good_rerank(["cap-h"]),  # turn 2 (pivot to HSR Layout)
        ])
        store = ConversationStore()
        r = _make_recaller(retrieval=retrieval, llm=llm, conversation_store=store)

        t1 = asyncio.run(r.recall(query="kanban", user_id=1))
        # "HSR Layout" filter against [cap-k with "kanban WIP limits"] → empty
        t2 = asyncio.run(r.recall(
            query="HSR Layout", user_id=1, conversation_id=t1.conversation_id,
        ))

        # Retrieval called TWICE — first turn + the pivot.
        assert len(retrieval.calls) == 2
        assert retrieval.calls[1]["query"] == "HSR Layout"
        # Anchor reset to the new topic.
        entry = store.get(t1.conversation_id)
        assert entry.last_query == "HSR Layout"
        # Pivot resets the filter chain.
        assert entry.accumulated_filters == []
        assert t2.results[0].capture_id == "cap-h"


# ---- No-match cases --------------------------------------------------

class TestNoMatch:
    def test_empty_retrieval_short_circuits_to_no_match(self, no_summaries):
        retrieval = _StubRetrievalService({"anything": []})
        llm = _StubLLMClient([])  # no LLM should be called
        r = _make_recaller(retrieval=retrieval, llm=llm)

        response = asyncio.run(r.recall(query="anything", user_id=1))
        assert response.no_match is True
        assert response.results == []
        assert llm.calls == []  # never reached the LLM

    def test_low_confidence_triggers_closest_miss(self, no_summaries):
        """Top candidate confidence < threshold → no_match=True + the
        closest candidate as a courtesy. Triggers a second LLM call
        for the framing."""
        cands = [_make_candidate(capture_id="cap-a")]
        retrieval = _StubRetrievalService({"x": cands})
        llm = _StubLLMClient([
            # Rerank says low confidence
            _good_rerank(["cap-a"], top_confidence=0.3),
            # Closest-miss framing
            {"answer": "I don't think this is in your corpus, but here's the closest."},
        ])
        r = _make_recaller(retrieval=retrieval, llm=llm)

        response = asyncio.run(r.recall(query="x", user_id=1))
        assert response.no_match is True
        assert len(response.results) == 1
        assert response.results[0].capture_id == "cap-a"
        assert "closest" in response.answer.lower()
        # Two LLM calls — rerank + closest-miss framing.
        assert len(llm.calls) == 2

    def test_closest_miss_llm_failure_uses_static_fallback(self, no_summaries):
        """The closest-miss LLM call is allowed to fail — we have a
        canned sentence ready."""
        cands = [_make_candidate(capture_id="cap-a")]
        retrieval = _StubRetrievalService({"x": cands})
        llm = _StubLLMClient([
            _good_rerank(["cap-a"], top_confidence=0.3),
            TransientLLMError("blip"),
        ])
        r = _make_recaller(retrieval=retrieval, llm=llm)

        response = asyncio.run(r.recall(query="x", user_id=1))
        assert response.no_match is True
        # Static fallback fired (canned message in recaller.py).
        assert "closest match" in response.answer.lower()


# ---- LLM failure degradation -----------------------------------------

class TestLLMFailure:
    def test_transient_error_uses_rule_based_rerank(self, no_summaries):
        cands = [
            _make_candidate(capture_id="cap-a", fused_score=0.05),
            _make_candidate(capture_id="cap-b", fused_score=0.03),
        ]
        retrieval = _StubRetrievalService({"q": cands})
        llm = _StubLLMClient([TransientLLMError("connection")])
        r = _make_recaller(retrieval=retrieval, llm=llm)

        response = asyncio.run(r.recall(query="q", user_id=1))
        # No raise — still a usable response.
        assert isinstance(response, RecallResponse)
        # The rule-based fallback preserves the order from retrieval.
        # Whether it ends up no_match depends on the fused→confidence
        # mapping; in this fixture cap-a's score is high enough to
        # clear the threshold.
        assert any(r.capture_id == "cap-a" for r in response.results)

    def test_permanent_error_uses_rule_based_rerank(self, no_summaries):
        cands = [_make_candidate(capture_id="cap-a", fused_score=0.05)]
        retrieval = _StubRetrievalService({"q": cands})
        llm = _StubLLMClient([PermanentLLMError("auth")])
        r = _make_recaller(retrieval=retrieval, llm=llm)

        response = asyncio.run(r.recall(query="q", user_id=1))
        assert isinstance(response, RecallResponse)
        assert len(response.results) == 1

    def test_malformed_json_uses_rule_based_rerank(self, no_summaries):
        cands = [_make_candidate(capture_id="cap-a", fused_score=0.05)]
        retrieval = _StubRetrievalService({"q": cands})
        llm = _StubLLMClient([MalformedResponseError("not json")])
        r = _make_recaller(retrieval=retrieval, llm=llm)

        response = asyncio.run(r.recall(query="q", user_id=1))
        assert isinstance(response, RecallResponse)
        assert len(response.results) == 1


# ---- Sonnet response shape validation --------------------------------

class TestRerankValidation:
    """Internal _validate_rerank_shape — bad LLM output must trigger
    the malformed-json fallback (not corrupt results)."""

    def test_missing_ranked_results_treated_as_malformed(self, no_summaries):
        cands = [_make_candidate(capture_id="cap-a", fused_score=0.05)]
        retrieval = _StubRetrievalService({"q": cands})
        # No "ranked_results" → validation fails → fallback path.
        llm = _StubLLMClient([{"brief_answer": "hi", "no_match": False}])
        r = _make_recaller(retrieval=retrieval, llm=llm)

        response = asyncio.run(r.recall(query="q", user_id=1))
        # Falls through the rerank exception → rule-based fallback.
        assert isinstance(response, RecallResponse)
        assert len(response.results) == 1

    def test_confidence_out_of_range_treated_as_malformed(self, no_summaries):
        cands = [_make_candidate(capture_id="cap-a", fused_score=0.05)]
        retrieval = _StubRetrievalService({"q": cands})
        llm = _StubLLMClient([{
            "ranked_results": [{
                "capture_id": "cap-a",
                "confidence": 1.5,  # out of range
                "why_this_matches": "x",
            }],
            "brief_answer": "x",
            "no_match": False,
        }])
        r = _make_recaller(retrieval=retrieval, llm=llm)

        response = asyncio.run(r.recall(query="q", user_id=1))
        assert isinstance(response, RecallResponse)


# ---- Conversation store TTL ------------------------------------------

class TestConversationStore:
    def test_get_returns_None_after_ttl(self, monkeypatch):
        store = ConversationStore(ttl_seconds=10)
        entry = _ConversationEntry(
            conversation_id="abc",
            last_query="x",
            last_candidates=[],
        )
        store.save(entry)

        # Fast-forward the monotonic clock by 11 seconds.
        original = time.monotonic
        fake_now = original() + 11
        monkeypatch.setattr(time, "monotonic", lambda: fake_now)

        assert store.get("abc") is None
        # And the stale entry was evicted from the dict.
        assert store.size() == 0

    def test_get_returns_entry_inside_ttl(self):
        store = ConversationStore(ttl_seconds=10)
        entry = _ConversationEntry(
            conversation_id="abc",
            last_query="x",
            last_candidates=[],
        )
        store.save(entry)

        assert store.get("abc") is entry

    def test_new_id_is_unique_per_call(self):
        store = ConversationStore()
        ids = {store.new_id() for _ in range(20)}
        assert len(ids) == 20


# ---- Summary join (uses real EnrichmentRepository) -------------------

class TestSummaryHydration:
    """Exercises the real `_fetch_summaries` path through SQL. Uses
    the autouse `clean_engine` fixture so SQL starts fresh."""

    def test_summary_threaded_through_to_result_block(self):
        async def go():
            await init_db()
            async with session_scope() as session:
                await UserRepository(session).create(
                    email="sabya@example.com",
                    display_name="Sabya",
                    user_id=1,
                )
                await CaptureRepository(session).create(Capture(
                    id="cap-with-summary",
                    user_id=1,
                    url="https://example.com/x",
                    title="Article X",
                    platform="general",
                    content_type="article",
                    captured_at="2026-05-08T10:00:00+00:00",
                    dwell_seconds=42,
                    raw_metadata_json=None,
                    clean_text="body",
                ))
                await EnrichmentRepository(session).create(
                    capture_id="cap-with-summary",
                    summary="A two-sentence enrichment summary about kanban.",
                    key_facts_json="[]",
                    model="stub",
                    enriched_at="2026-05-08T10:01:00+00:00",
                )

            cands = [_make_candidate(capture_id="cap-with-summary")]
            retrieval = _StubRetrievalService({"q": cands})
            llm = _StubLLMClient([_good_rerank(["cap-with-summary"])])
            recaller = _make_recaller(retrieval=retrieval, llm=llm)
            return await recaller.recall(query="q", user_id=1), llm

        response, llm = asyncio.run(go())
        # Result block carries the summary.
        assert response.results[0].summary == (
            "A two-sentence enrichment summary about kanban."
        )
        # Sonnet prompt got it too.
        assert "enrichment summary about kanban" in llm.calls[0]["user_prompt"]

    def test_missing_summary_falls_back_to_none_in_block(self):
        """Capture has no enrichment row → summary=None in the result
        block, prompt template uses the '(no summary available)' line."""
        async def go():
            await init_db()
            async with session_scope() as session:
                await UserRepository(session).create(
                    email="x@example.com", display_name="X", user_id=1,
                )
                await CaptureRepository(session).create(Capture(
                    id="cap-no-summary",
                    user_id=1,
                    url="https://example.com/x",
                    title="Article",
                    platform="general",
                    content_type="article",
                    captured_at="2026-05-08T10:00:00+00:00",
                    dwell_seconds=42,
                    raw_metadata_json=None,
                    clean_text="body",
                ))

            cands = [_make_candidate(capture_id="cap-no-summary")]
            retrieval = _StubRetrievalService({"q": cands})
            llm = _StubLLMClient([_good_rerank(["cap-no-summary"])])
            recaller = _make_recaller(retrieval=retrieval, llm=llm)
            return await recaller.recall(query="q", user_id=1), llm

        response, llm = asyncio.run(go())
        assert response.results[0].summary is None
        # Sonnet got the fallback placeholder in the prompt.
        assert "(no summary available)" in llm.calls[0]["user_prompt"]
