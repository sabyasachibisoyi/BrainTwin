"""Phase 4 M.3b — prompts for the Recaller agent.

Two prompt families live here:

  1. The **re-rank** prompt. Sonnet receives the top-N candidate
     captures from RetrievalService along with the user's query.
     It re-orders them by how well each actually answers the query
     (rather than how strongly it matched the rankers), attaches a
     confidence score and a one-sentence "why this matches"
     reasoning to each, and composes the brief-mode conversational
     answer that fronts the result block.

  2. The **closest-miss explainer**. When no candidate clears the
     confidence threshold (V.7), Sonnet is asked to produce a short
     honest framing — "I don't think this is in my corpus, but the
     closest match I have is …" — so the response shape (U.4) reads
     like a memory-prosthetic admitting uncertainty rather than a
     search box returning nothing.

Both prompts ask for **JSON-only output** in a schema the parser can
validate. The system prompt sets the tone and the contract; the user
prompt formats the actual candidates.

Why JSON and not free text:
  - Deterministic parsing — `json.loads` either succeeds or raises;
    free-text parsing would need regex hacks per response.
  - Confidence is a numeric field — needs a typed slot, not "very
    confident" / "somewhat sure" prose.
  - The re-rank pass needs to produce structured per-candidate
    fields (rank, confidence, reasoning); JSON is the obvious fit.

Why Sonnet, not Haiku:
  - Phase 3 design A.6 / Phase 4 design V.6 lock Sonnet for the
    recall reasoning step.
  - Haiku runs every capture enrichment (cost-bound, runs hundreds of
    times per week); Sonnet runs per user query (quality-bound, runs
    at human typing speed).
  - Per-query Sonnet cost is ~$0.003, fine for single-user volumes.
"""

from __future__ import annotations


# ---- Re-rank prompt --------------------------------------------------

RERANK_SYSTEM_PROMPT = """You are BrainTwin's memory recall assistant.

The user is trying to remember something they previously consumed — an article they read, a video they watched, a meme someone forwarded them. You will be given:

- The user's query (often vague, half-remembered, with paraphrased or proper-noun fragments)
- Up to 6 candidate captures from their personal corpus, each with title, source, capture date, an enrichment summary, the matching text snippet, and provenance info (which ranker surfaced it)

Your job is to:

1. Decide which candidate is most likely the one the user is trying to remember.
2. Rank the others by how plausibly they fit the query.
3. Attach a confidence score (0.0-1.0) per candidate — how sure you are that THIS is the user's target.
4. Write a one-sentence "why_this_matches" reasoning per candidate, citing what in the candidate connects to the query.
5. Compose a brief one-sentence conversational answer naming the top candidate in a way that triggers the user's memory ("I think you're remembering …", "That sounds like the X piece from Y").

Be honest about uncertainty. If no candidate clearly answers the query — the query mentions things that don't appear in any candidate, or the matches feel coincidental — keep all confidences below 0.6 and set `no_match: true`. The user trusts an honest "I don't think this is in my corpus" far more than a confidently-wrong guess.

Output rules:
- Respond ONLY with the JSON object described below. No prose before or after, no markdown fences.
- Every candidate you receive must appear exactly once in `ranked_results`, ordered by your judgment from best to worst.
- `confidence` must be a number between 0.0 and 1.0 inclusive.
- `why_this_matches` must be one sentence, plain prose, no bullet points.
- `brief_answer` must be one sentence in second person ("I think you're remembering…", not "The article is about…").
- `no_match` must be `true` if and only if the top candidate's confidence is below 0.6.

JSON schema:
{
  "ranked_results": [
    {
      "capture_id": "<string — exact id from the candidate>",
      "confidence": <number 0.0-1.0>,
      "why_this_matches": "<one short sentence>"
    },
    ...
  ],
  "brief_answer": "<one sentence reminder of the top candidate>",
  "no_match": <boolean>
}"""


RERANK_USER_PROMPT_TEMPLATE = """The user typed: "{query}"

Here are {n_candidates} candidate captures from their corpus, in rank order from the retrieval layer (which is NOT the final ranking — your judgment overrides this).

{candidates_block}

Return the JSON object as instructed."""


# ---- Candidate formatting --------------------------------------------

CANDIDATE_TEMPLATE = """[Candidate {n}]
capture_id: {capture_id}
title: {title}
source: {source_domain}
captured: {captured_at} via {client} ({dwell_seconds}s dwell)
summary: {summary}
matching snippet:
{snippet}
provenance: {provenance}
"""


def format_candidate_block(
    *,
    n: int,
    capture_id: str,
    title: str | None,
    source_domain: str | None,
    captured_at: str,
    client: str | None,
    dwell_seconds: int,
    summary: str | None,
    snippet: str,
    provenance: str,
) -> str:
    """Render one candidate as a labelled block. Helper kept here so
    the prompt structure stays in one file — Recaller just feeds the
    fields in."""
    return CANDIDATE_TEMPLATE.format(
        n=n,
        capture_id=capture_id,
        title=title or "(untitled)",
        source_domain=source_domain or "(unknown)",
        captured_at=captured_at,
        client=client or "unknown",
        dwell_seconds=dwell_seconds,
        summary=summary or "(no summary available)",
        snippet=snippet.strip(),
        provenance=provenance,
    )


def build_rerank_user_prompt(
    *,
    query: str,
    candidates_block: str,
    n_candidates: int,
) -> str:
    """Compose the user-prompt portion of the re-rank call."""
    return RERANK_USER_PROMPT_TEMPLATE.format(
        query=query,
        n_candidates=n_candidates,
        candidates_block=candidates_block.strip(),
    )


# ---- Closest-miss prompt (U.4) ---------------------------------------

CLOSEST_MISS_SYSTEM_PROMPT = """You are BrainTwin's memory recall assistant.

The user asked for something that is NOT confidently in their corpus. You will be given the query and the closest-matching capture we have, with the same fields as a regular re-rank candidate.

Your job is to compose a short honest framing — two sentences max — that:

1. Acknowledges this is probably not the thing the user was remembering.
2. Offers the closest match as a courtesy, so the user can confirm or look elsewhere.

Tone: a librarian admitting they couldn't find the exact book, but mentioning a related one in case it helps. Not a salesperson pushing a substitute.

Output rules:
- Respond ONLY with the JSON object below. No prose before or after, no markdown.
- The `answer` is a single field, plain text, two sentences max, second person.

JSON schema:
{
  "answer": "<two sentences max, second person, honest framing>"
}"""


CLOSEST_MISS_USER_PROMPT_TEMPLATE = """The user typed: "{query}"

No candidate in their corpus matched confidently. The closest one I have is:

{candidate_block}

Return the JSON object as instructed."""


def build_closest_miss_user_prompt(
    *,
    query: str,
    candidate_block: str,
) -> str:
    """Compose the user prompt for the closest-miss framing call."""
    return CLOSEST_MISS_USER_PROMPT_TEMPLATE.format(
        query=query,
        candidate_block=candidate_block.strip(),
    )


__all__ = [
    "RERANK_SYSTEM_PROMPT",
    "RERANK_USER_PROMPT_TEMPLATE",
    "CANDIDATE_TEMPLATE",
    "format_candidate_block",
    "build_rerank_user_prompt",
    "CLOSEST_MISS_SYSTEM_PROMPT",
    "CLOSEST_MISS_USER_PROMPT_TEMPLATE",
    "build_closest_miss_user_prompt",
]
