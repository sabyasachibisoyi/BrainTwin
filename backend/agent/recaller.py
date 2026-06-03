"""Phase 4 M.3 — Recaller agent.

The user-facing orchestrator for vague-recall search. Composes
RetrievalService (M.2) with Sonnet (via LLMClient.complete_json) to
turn a raw query into a structured RecallResponse — the shape that
`POST /recall` returns (S.3) and the shape the Chrome extension /
Telegram bot render against (U.2).

Pipeline:
  1. If the query is empty / whitespace, return a no-match response
     without touching the embedder or any store. Same for negative
     top_k.
  2. Apply any conversation-state filters (U.3) — when a user adds a
     refinement turn ("not that one, more recent"), past filters
     narrow the candidate pool instead of running a fresh search.
  3. Call RetrievalService.recall() to get up to top_captures
     candidates.
  4. If retrieval returns zero candidates → respond with no_match,
     skip Sonnet entirely (no candidates = nothing to re-rank).
  5. If retrieval returns exactly one candidate → fast path. Still
     call Sonnet to compose the brief answer and confidence, but skip
     the multi-candidate reasoning step.
  6. Otherwise: call Sonnet via complete_json with the rerank prompt
     to re-rank candidates by actual relevance to the query, attach
     confidence, and generate the brief answer.
  7. Apply the confidence threshold (V.7). Top score < 0.6 → no_match
     with the closest-miss framing (U.4).
  8. Build the RecallResponse and update conversation state.

Failure handling:
  - RetrievalService failure: shouldn't happen — its rankers are
    already fault-isolated. If it does, return a no-match response
    with an empty result list (no Sonnet call).
  - LLMClient transient failure: surface as a 503 in the upstream
    endpoint; the user can retry. We deliberately do NOT auto-retry
    Sonnet — that would burn cost on flaky-network responses and
    delay the user.
  - LLMClient permanent failure: same as above — the operator needs
    to see this.
  - MalformedResponseError: fall back to a rule-based response built
    from the retrieval candidates (top RRF score wins, snippets are
    the why_this_matches, confidence is a rough mapping from the
    fused score). The user gets a degraded but still usable answer
    rather than a 500.

Conversation state (U.3):
  Stored in-memory keyed by conversation_id (uuid4). Each entry holds
  the last query, the last candidate pool, and any text filters the
  LLM has interpreted from refinement turns. TTL is enforced lazily
  on access — entries older than 30 minutes are dropped on read.
  Process restart clears all state (acceptable: refinement turns are
  intentionally short-lived).
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional
from urllib.parse import urlparse

from backend.agent.prompts import (
    CLOSEST_MISS_SYSTEM_PROMPT,
    RERANK_SYSTEM_PROMPT,
    build_closest_miss_user_prompt,
    build_rerank_user_prompt,
    format_candidate_block,
)
from backend.agent.retrieval import (
    CandidateCapture,
    DEFAULT_TOP_CAPTURES,
    RetrievalResult,
    RetrievalService,
)
from backend.knowledge.llm_client import (
    LLMClient,
    MalformedResponseError,
    PermanentLLMError,
    TransientLLMError,
)
from backend.storage import EnrichmentRepository, session_scope


logger = logging.getLogger(__name__)


# Confidence threshold (V.7). Top score < this → no_match + closest-miss
# framing. Starting estimate; tune against the eval set when Phase 4.0.5
# stands up.
DEFAULT_CONFIDENCE_THRESHOLD = 0.6

# Conversation-state idle TTL (U.3). 30 minutes is the design lock.
CONVERSATION_TTL_SECONDS = 30 * 60


# ---- Public response dataclasses (S.3 shape) -------------------------

@dataclass(frozen=True)
class RankedResult:
    """One result block as it ships to the Chrome extension / Telegram
    bot. Field set is fixed by U.2 so renderers can be one-pass."""
    capture_id: str
    title: Optional[str]
    source_domain: Optional[str]
    captured_at: str
    client: Optional[str]
    dwell_time_seconds: int
    why_this_matches: str
    snippet: str
    original_url: Optional[str]
    summary: Optional[str]
    confidence: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RecallResponse:
    """The shape `POST /recall` returns (S.3). Same shape on success,
    closest-miss, and empty-retrieval — only `no_match` and the size
    of `results` change."""
    answer: str
    confidence: float
    results: list[RankedResult]
    conversation_id: str
    no_match: bool

    def to_dict(self) -> dict:
        return {
            "answer": self.answer,
            "confidence": self.confidence,
            "results": [r.to_dict() for r in self.results],
            "conversation_id": self.conversation_id,
            "no_match": self.no_match,
        }


# ---- Conversation state ----------------------------------------------

@dataclass
class _ConversationEntry:
    """One row in the in-memory conversation store. Mutable so a
    follow-up turn can layer filters on without re-creating the entry."""
    conversation_id: str
    last_query: str
    last_candidates: list[CandidateCapture]
    accumulated_filters: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.monotonic)


class ConversationStore:
    """In-memory conversation store with lazy TTL eviction. Not thread-
    safe per-key — fine for asyncio (single-threaded event loop) but
    would need a lock if we ever go multi-threaded.

    Tests should construct their own instance so they don't share state
    with the production Recaller singleton."""

    def __init__(self, ttl_seconds: int = CONVERSATION_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._entries: dict[str, _ConversationEntry] = {}

    def get(self, conversation_id: str) -> Optional[_ConversationEntry]:
        entry = self._entries.get(conversation_id)
        if entry is None:
            return None
        if time.monotonic() - entry.updated_at > self._ttl:
            del self._entries[conversation_id]
            return None
        return entry

    def save(self, entry: _ConversationEntry) -> None:
        entry.updated_at = time.monotonic()
        self._entries[entry.conversation_id] = entry

    def new_id(self) -> str:
        return str(uuid.uuid4())

    def size(self) -> int:
        return len(self._entries)


# ---- The Recaller ----------------------------------------------------

class Recaller:
    """Orchestrates RetrievalService + Sonnet into a RecallResponse.

    Stateless aside from the conversation store, which is keyed by
    conversation_id and intentionally short-lived. Construct once at
    process startup and share across requests.
    """

    def __init__(
        self,
        *,
        retrieval: RetrievalService,
        llm_client: LLMClient,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        conversation_store: Optional[ConversationStore] = None,
    ):
        self._retrieval = retrieval
        self._llm = llm_client
        self._threshold = confidence_threshold
        self._conversations = conversation_store or ConversationStore()

    # ---- Public entry point --------------------------------------

    async def recall(
        self,
        *,
        query: str,
        user_id: int,
        conversation_id: Optional[str] = None,
        top_captures: int = DEFAULT_TOP_CAPTURES,
    ) -> RecallResponse:
        """Run the full recall pipeline. Always returns a RecallResponse
        — failures degrade to no-match rather than raising.

        Multi-turn flow (per design doc U.3): the FIRST turn in a
        conversation hits retrieval normally. Subsequent turns treat
        the query as a *filter* layered on the previous turn's
        candidate pool — no fresh retrieval, no embedding cost, no
        Chroma round-trip. The Sonnet rerank still runs on the
        filtered pool. If the filter narrows the pool to zero (the
        user pivoted to a genuinely new topic), we fall back to a
        fresh retrieval with the new query as the anchor.
        """
        conv_id = conversation_id or self._conversations.new_id()

        if not query or not query.strip():
            return self._empty_query_response(conv_id)

        # ---- Step 2: pull conversation state (U.3) ----------------
        prior = (
            self._conversations.get(conversation_id)
            if conversation_id else None
        )

        # ---- Step 3: candidate pool — refinement OR fresh retrieval
        # On a follow-up turn, the candidate pool is the PRIOR turn's
        # results filtered by the new query — not a fresh search. This
        # matches U.3's "filter on the previous candidate pool" semantic
        # and keeps refinement turns to a single LLM call (the rerank)
        # instead of two (rerank + retrieval).
        anchor_query = query
        candidates: list[CandidateCapture] = []
        refinement_turn = (
            prior is not None and bool(prior.last_candidates)
        )

        if refinement_turn:
            candidates = self._apply_filters(prior.last_candidates, [query])
            # If the filter wiped the pool, the user has pivoted to a
            # new topic. Re-run retrieval with the new query so the
            # interaction doesn't silently fail.
            if not candidates:
                logger.info(
                    "Recaller: refinement filter emptied the pool; "
                    "treating as new topic and re-retrieving"
                )
                candidates = await self._fresh_retrieval(
                    query=query,
                    user_id=user_id,
                    top_captures=top_captures,
                )
                # New topic → new anchor, prior filters reset on save.
                refinement_turn = False
                anchor_query = query
            else:
                # True refinement turn — keep the original anchor so
                # downstream turns layer on the same lineage.
                anchor_query = prior.last_query
        else:
            candidates = await self._fresh_retrieval(
                query=query,
                user_id=user_id,
                top_captures=top_captures,
            )
            anchor_query = query

        # ---- Step 4: empty retrieval → no-match ------------------
        if not candidates:
            return self._no_match_response(
                query=query,
                conv_id=conv_id,
                closest=None,
                summaries={},
            )

        # Hydrate enrichment summaries in one bulk SQL call. Used by
        # both the Sonnet prompt (more context = better re-ranking)
        # and the response shape (U.2's `summary` field). Failure
        # here is non-fatal — candidates without a summary just fall
        # back to the prompt template's "(no summary available)" line.
        summaries = await self._fetch_summaries(candidates, user_id)

        # ---- Steps 5-7: Sonnet re-rank ---------------------------
        try:
            rerank = await self._call_rerank(
                query=query, candidates=candidates, summaries=summaries,
            )
        except (TransientLLMError, PermanentLLMError) as e:
            logger.warning("Recaller: LLM call failed (%s) — degrading", e)
            rerank = None
        except MalformedResponseError as e:
            logger.warning("Recaller: LLM returned malformed JSON: %s", e)
            rerank = None

        if rerank is None:
            # LLM degraded — fall back to retrieval order.
            rerank = self._fallback_rerank(candidates)

        # ---- Step 7: confidence threshold (V.7) ------------------
        top_confidence = max(
            (r["confidence"] for r in rerank["ranked_results"]),
            default=0.0,
        )
        no_match = rerank.get("no_match", False) or top_confidence < self._threshold

        if no_match:
            return await self._closest_miss_response(
                query=query,
                conv_id=conv_id,
                candidates=candidates,
                rerank=rerank,
                summaries=summaries,
            )

        # ---- Step 8: build response + save conversation ----------
        results = self._compose_results(candidates, rerank, summaries)
        answer = rerank.get("brief_answer") or self._fallback_brief_answer(results)

        response = RecallResponse(
            answer=answer,
            confidence=top_confidence,
            results=results,
            conversation_id=conv_id,
            no_match=False,
        )
        self._save_conversation(
            conv_id=conv_id,
            anchor_query=anchor_query,
            this_turn_query=query,
            candidates=candidates,
            prior=prior if refinement_turn else None,
        )
        return response

    # ---- Retrieval helper ----------------------------------------

    async def _fresh_retrieval(
        self,
        *,
        query: str,
        user_id: int,
        top_captures: int,
    ) -> list[CandidateCapture]:
        """Run RetrievalService.recall and surface its candidates,
        swallowing any unexpected failure into an empty list. The
        retrieval layer is already fault-isolated per ranker; this
        outer try/except just guards against truly unexpected errors
        so the Recaller can degrade to a no-match response rather
        than raising out of `recall()`."""
        try:
            result = await self._retrieval.recall(
                query=query, user_id=user_id, top_captures=top_captures,
            )
            return list(result.candidates)
        except Exception as e:  # noqa: BLE001
            logger.exception("Recaller: retrieval raised: %s", e)
            return []

    # ---- Summary hydration (Phase 4 M.3 — Q3 fix) ----------------

    async def _fetch_summaries(
        self,
        candidates: list[CandidateCapture],
        user_id: int,
    ) -> dict[str, Optional[str]]:
        """Bulk-fetch enrichment summaries for the candidate set in
        one SQL call. Best-effort: a SQL hiccup returns an empty dict
        and downstream code falls back gracefully on missing summaries."""
        if not candidates:
            return {}
        capture_ids = [c.capture.id for c in candidates]
        try:
            async with session_scope() as session:
                return await EnrichmentRepository(session).get_summaries_by_capture_ids(
                    capture_ids, user_id=user_id,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Recaller: summary hydration failed (%s) — continuing without", e,
            )
            return {}

    # ---- LLM calls -----------------------------------------------

    async def _call_rerank(
        self,
        *,
        query: str,
        candidates: list[CandidateCapture],
        summaries: dict[str, Optional[str]],
    ) -> dict:
        """Run the Sonnet re-rank pass and return the parsed JSON."""
        block = "\n".join(
            format_candidate_block(
                n=i + 1,
                capture_id=cand.capture.id,
                title=cand.capture.title,
                source_domain=_extract_domain(cand.capture.url),
                captured_at=cand.capture.captured_at,
                client=cand.capture.platform,
                dwell_seconds=cand.capture.dwell_seconds,
                summary=summaries.get(cand.capture.id),
                snippet=cand.best_chunk.chunk.text,
                provenance=_provenance_line(cand),
            )
            for i, cand in enumerate(candidates)
        )
        user_prompt = build_rerank_user_prompt(
            query=query,
            candidates_block=block,
            n_candidates=len(candidates),
        )
        parsed = await self._llm.complete_json(
            system_prompt=RERANK_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
        self._validate_rerank_shape(parsed, n_candidates=len(candidates))
        return parsed

    async def _call_closest_miss(
        self,
        *,
        query: str,
        closest: CandidateCapture,
        summary: Optional[str],
    ) -> str:
        """Run the closest-miss framing call. Falls back to a static
        sentence if the LLM declines or errors."""
        block = format_candidate_block(
            n=1,
            capture_id=closest.capture.id,
            title=closest.capture.title,
            source_domain=_extract_domain(closest.capture.url),
            captured_at=closest.capture.captured_at,
            client=closest.capture.platform,
            dwell_seconds=closest.capture.dwell_seconds,
            summary=summary,
            snippet=closest.best_chunk.chunk.text,
            provenance=_provenance_line(closest),
        )
        user_prompt = build_closest_miss_user_prompt(
            query=query, candidate_block=block,
        )
        try:
            parsed = await self._llm.complete_json(
                system_prompt=CLOSEST_MISS_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                max_tokens=200,
            )
        except (TransientLLMError, PermanentLLMError, MalformedResponseError):
            return _STATIC_CLOSEST_MISS

        answer = parsed.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            return _STATIC_CLOSEST_MISS
        return answer.strip()

    # ---- Response builders ---------------------------------------

    def _compose_results(
        self,
        candidates: list[CandidateCapture],
        rerank: dict,
        summaries: dict[str, Optional[str]],
    ) -> list[RankedResult]:
        """Build the ordered list of RankedResult from the Sonnet
        ranking. Candidates not present in the LLM's ranked_results
        (shouldn't happen if the LLM followed instructions) are
        appended at the end with their fused score as confidence."""
        by_id = {c.capture.id: c for c in candidates}
        used: set[str] = set()
        results: list[RankedResult] = []

        for entry in rerank.get("ranked_results", []):
            cid = entry.get("capture_id")
            if cid not in by_id or cid in used:
                continue
            used.add(cid)
            cand = by_id[cid]
            results.append(self._candidate_to_result(
                cand,
                confidence=float(entry.get("confidence", 0.0)),
                why=str(entry.get("why_this_matches") or _fallback_why(cand)),
                summary=summaries.get(cid),
            ))

        # Append any candidates the LLM forgot.
        for cand in candidates:
            if cand.capture.id in used:
                continue
            results.append(self._candidate_to_result(
                cand,
                confidence=_confidence_from_fused(cand.fused_score),
                why=_fallback_why(cand),
                summary=summaries.get(cand.capture.id),
            ))

        return results

    @staticmethod
    def _candidate_to_result(
        cand: CandidateCapture,
        *,
        confidence: float,
        why: str,
        summary: Optional[str],
    ) -> RankedResult:
        return RankedResult(
            capture_id=cand.capture.id,
            title=cand.capture.title,
            source_domain=_extract_domain(cand.capture.url),
            captured_at=cand.capture.captured_at,
            client=cand.capture.platform,
            dwell_time_seconds=cand.capture.dwell_seconds,
            why_this_matches=why,
            snippet=cand.best_chunk.chunk.text.strip(),
            original_url=cand.capture.url,
            summary=summary,
            confidence=max(0.0, min(1.0, confidence)),
        )

    def _empty_query_response(self, conv_id: str) -> RecallResponse:
        return RecallResponse(
            answer="Please type something to remember.",
            confidence=0.0,
            results=[],
            conversation_id=conv_id,
            no_match=True,
        )

    def _no_match_response(
        self,
        *,
        query: str,
        conv_id: str,
        closest: Optional[CandidateCapture],
        summaries: dict[str, Optional[str]],
    ) -> RecallResponse:
        results: list[RankedResult] = []
        if closest is not None:
            results.append(self._candidate_to_result(
                closest,
                confidence=_confidence_from_fused(closest.fused_score),
                why=_fallback_why(closest),
                summary=summaries.get(closest.capture.id),
            ))
        return RecallResponse(
            answer=_STATIC_NO_MATCH,
            confidence=0.0,
            results=results,
            conversation_id=conv_id,
            no_match=True,
        )

    async def _closest_miss_response(
        self,
        *,
        query: str,
        conv_id: str,
        candidates: list[CandidateCapture],
        rerank: dict,
        summaries: dict[str, Optional[str]],
    ) -> RecallResponse:
        """Build the no-match response with the closest-miss courtesy.
        Picks the LLM's top-ranked candidate as the closest miss; falls
        back to the fused-score winner if the rerank shape is degraded.
        """
        closest = None
        for entry in rerank.get("ranked_results", []):
            cid = entry.get("capture_id")
            for c in candidates:
                if c.capture.id == cid:
                    closest = c
                    break
            if closest is not None:
                break
        if closest is None:
            closest = candidates[0]

        closest_summary = summaries.get(closest.capture.id)
        answer = await self._call_closest_miss(
            query=query, closest=closest, summary=closest_summary,
        )
        results = [self._candidate_to_result(
            closest,
            confidence=_confidence_from_fused(closest.fused_score),
            why=_fallback_why(closest),
            summary=closest_summary,
        )]
        return RecallResponse(
            answer=answer,
            confidence=0.0,
            results=results,
            conversation_id=conv_id,
            no_match=True,
        )

    # ---- Conversation state helpers ------------------------------

    def _save_conversation(
        self,
        *,
        conv_id: str,
        anchor_query: str,
        this_turn_query: str,
        candidates: list[CandidateCapture],
        prior: Optional[_ConversationEntry],
    ) -> None:
        """Persist the conversation state after a successful turn.

        - `anchor_query` is the ORIGINAL query that opened this
          conversation. It stays stable across refinement turns and
          is what we'd re-run as a fresh retrieval if state got lost.
        - `this_turn_query` is what the user typed this turn. On
          refinement turns it's a filter text; on first turns and on
          "new topic" pivots it equals `anchor_query`.
        - `prior` is non-None only when this was a real refinement
          turn — caller passes None on first turns and on pivot
          (fresh-retrieval) turns so the filter chain resets.
        - `candidates` is the post-filter, post-rerank candidate pool
          that the next refinement turn will narrow further.
        """
        accumulated = list(prior.accumulated_filters) if prior else []
        if prior is not None:
            # Real refinement turn — append the new filter to the chain.
            accumulated.append(this_turn_query)
        entry = _ConversationEntry(
            conversation_id=conv_id,
            last_query=anchor_query,
            last_candidates=candidates,
            accumulated_filters=accumulated,
        )
        self._conversations.save(entry)

    @staticmethod
    def _apply_filters(
        candidates: list[CandidateCapture],
        filters: list[str],
    ) -> list[CandidateCapture]:
        """Substring filter pass — placeholder for v1.

        Treats each accumulated refinement turn as a positive "must
        appear somewhere" filter, matched against `title + snippet`.
        This is intentionally dumb and gets the polarity wrong for
        negative refinements ("not wikipedia") — see the design doc's
        4.0.5 eval section. The replacement is an LLM call that parses
        each refinement turn into a structured filter spec
        ({include_terms, exclude_terms, captured_after, …}). For now
        we ship the placeholder so the end-to-end flow works and tune
        in 4.0.5 with real refinement data.
        """
        if not filters:
            return candidates
        kept: list[CandidateCapture] = []
        for cand in candidates:
            haystack = " ".join([
                cand.capture.title or "",
                cand.best_chunk.chunk.text or "",
            ]).lower()
            if all(_contains_any_word(haystack, f) for f in filters):
                kept.append(cand)
        return kept

    # ---- Degraded paths ------------------------------------------

    @staticmethod
    def _fallback_rerank(candidates: list[CandidateCapture]) -> dict:
        """When Sonnet errors or returns malformed JSON, build a
        rerank shape from the retrieval layer's ordering directly.
        Confidence is a rough mapping from fused_score so the
        threshold logic still applies sensibly."""
        ranked = []
        for cand in candidates:
            ranked.append({
                "capture_id": cand.capture.id,
                "confidence": _confidence_from_fused(cand.fused_score),
                "why_this_matches": _fallback_why(cand),
            })
        top_conf = ranked[0]["confidence"] if ranked else 0.0
        return {
            "ranked_results": ranked,
            "brief_answer": "",  # filled in by _fallback_brief_answer
            "no_match": top_conf < DEFAULT_CONFIDENCE_THRESHOLD,
        }

    @staticmethod
    def _fallback_brief_answer(results: list[RankedResult]) -> str:
        if not results:
            return _STATIC_NO_MATCH
        top = results[0]
        title = top.title or "an untitled capture"
        domain = top.source_domain or "your corpus"
        return f"This looks like {title} from {domain}."

    # ---- Validation ---------------------------------------------

    @staticmethod
    def _validate_rerank_shape(parsed: dict, *, n_candidates: int) -> None:
        if not isinstance(parsed.get("ranked_results"), list):
            raise MalformedResponseError(
                "missing or non-list 'ranked_results' in Sonnet response"
            )
        for entry in parsed["ranked_results"]:
            if not isinstance(entry, dict):
                raise MalformedResponseError(
                    "ranked_results entries must be objects"
                )
            if "capture_id" not in entry or not isinstance(entry["capture_id"], str):
                raise MalformedResponseError("entry missing 'capture_id'")
            try:
                conf = float(entry.get("confidence", -1))
            except (TypeError, ValueError):
                raise MalformedResponseError("entry 'confidence' is not a number")
            if not (0.0 <= conf <= 1.0):
                raise MalformedResponseError(
                    f"entry 'confidence' out of range [0,1]: got {conf}"
                )


# ---- Static fallbacks ------------------------------------------------

_STATIC_NO_MATCH = (
    "I don't think this is in my corpus. Try rephrasing, or add a date "
    "or platform hint and I'll search again."
)

_STATIC_CLOSEST_MISS = (
    "I'm not confident this is what you're remembering, but here's the "
    "closest match I have in your corpus."
)


# ---- Helpers ---------------------------------------------------------

def _extract_domain(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        host = urlparse(url).hostname or url
    except Exception:  # noqa: BLE001
        return url
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _provenance_line(cand: CandidateCapture) -> str:
    """One-line description of which rankers surfaced this candidate.
    Goes into the Sonnet prompt so the LLM has signal about whether
    this was a semantic or lexical match."""
    parts = []
    if cand.best_chunk.vector_rank is not None:
        parts.append(f"vector rank {cand.best_chunk.vector_rank}")
    if cand.best_chunk.bm25_rank is not None:
        parts.append(f"BM25 rank {cand.best_chunk.bm25_rank}")
    if not parts:
        return "no ranker provenance"
    return ", ".join(parts)


def _fallback_why(cand: CandidateCapture) -> str:
    """One-line reasoning when the LLM didn't supply one — used in
    the closest-miss path and the degraded-rerank path."""
    bits = []
    if cand.best_chunk.bm25_rank is not None:
        bits.append("matched on word search")
    if cand.best_chunk.vector_rank is not None:
        bits.append("semantically related")
    if not bits:
        bits.append("surfaced by retrieval")
    return f"This capture {' and '.join(bits)}."


def _confidence_from_fused(fused_score: float) -> float:
    """Map an RRF fused score to a rough confidence in [0, 1]. The
    RRF score scale depends on the dampening constant (60 by default);
    a chunk topping both rankers maxes around 2/(60+1) ≈ 0.033. Scale
    that into a useful confidence range so the threshold + UI still
    behave sensibly when Sonnet is unavailable."""
    # 0.033 → 0.9, 0.0 → 0.0; saturating tanh-style mapping.
    import math
    return float(max(0.0, min(1.0, math.tanh(fused_score * 50))))


def _contains_any_word(haystack: str, filter_text: str) -> bool:
    """Returns True if ANY word in the filter appears in haystack
    (lowercased). Cheap v1 — replace with a real LLM-filter parse
    once we have signal on how users actually phrase refinement
    turns."""
    haystack = haystack.lower()
    tokens = [t for t in filter_text.lower().split() if len(t) > 2]
    if not tokens:
        return True
    return any(t in haystack for t in tokens)


__all__ = [
    "RankedResult",
    "RecallResponse",
    "Recaller",
    "ConversationStore",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "CONVERSATION_TTL_SECONDS",
]
