# Phase 4 — Agent Layer (Vague-Recall Search + Retrieval Foundations)

> **Status as of 2026-06-02 — DESIGN LOCKED, AWAITING IMPLEMENTATION.**
>
> Phase 4 is where BrainTwin stops being plumbing and becomes a thing
> you actually open. This document scopes the **first slice of the agent
> layer**: use case B from the Phase 3 design — vague-recall search ("I
> read something about X a few weeks ago, find it for me") — plus the
> retrieval foundations that use cases A (synthesis quizzes) and C
> (clue inference) will later sit on top of.
>
> A and C are explicitly **out of scope** here. They get their own
> design docs (`phase4.1-quizzes-design.md`, `phase4.2-clue-game-design.md`)
> once vague recall is shipped and we know how the retrieval layer
> actually behaves on real queries.
>
> **Locked decisions at a glance:**
> - **V.1** Hybrid retrieval — Chroma (vector) + SQLite FTS5 (BM25), both querying the same `chunks` table
> - **V.2** Reciprocal Rank Fusion for combining the two rankers (parameter-free, constant `k=60`)
> - **V.3** Cosine similarity for vector ranking (equivalent to dot product over the normalized vectors we already produce)
> - **V.4** `K=20` per ranker, top-6 captures handed to the LLM after fusion + diversification
> - **V.5** Group-by-capture diversification — one chunk per capture max in the candidate pool
> - **V.6** Sonnet 4.6 as the re-ranker / answer composer (~$0.003/query)
> - **V.7** Confidence threshold ~0.6 for "no good match"; <0.1 spread between top 2 → ask for disambiguation
> - **V.8** Per-chunk entity tagging — deferred to Phase 4.2 (clue game)
> - **U.1** Brief mode is the default response shape (one-sentence answer + source block)
> - **U.2** Standardized result block — title, source, capture metadata, snippet, original URL, why-this-matches
> - **U.3** Multi-turn refinement via `conversation_id` — follow-ups are filters on the previous query, not fresh searches
> - **U.4** "Closest miss" courtesy result when nothing meets the confidence threshold
> - **S.1** Chrome extension is the first surface (Remember tab in the existing popup)
> - **S.2** Telegram bot is the second surface (`/recall` command + auto-routing of non-URL text)
> - **S.3** `POST /recall` is the single backend endpoint serving both surfaces

---

## What Phase 4 (vague recall) is for

Use case B from `docs/phase3-design.md`: *"a student remembers fragments
('there was something about hash collisions a few weeks ago…'), and
asks BrainTwin to find the source. Single-user feel; depends on strong
semantic retrieval. This is BrainTwin as a memory prosthetic."*

It's the right first build of the agent layer because:

1. **Smallest user surface.** A search box and a result card. No quiz
   generation UI, no multi-turn game state. The product-design budget
   stays on retrieval quality.
2. **Hardest test of the retrieval stack.** A vague-recall query is
   the worst-case shape for any single ranker — the user is half-
   remembering, often with a proper noun in non-English, often after
   weeks. If the retrieval stack survives this, the cleaner queries
   that A and C will throw at it are easier.
3. **Used every day.** Synthesis quizzes are great when you have time
   to sit and study; vague recall is what you reach for in the middle
   of a meeting. The day-to-day utility builds the habit of opening
   BrainTwin.

---

## The retrieval problem

A vague-recall query has two failure modes that any single ranker hits:

**Failure mode 1 — Proper nouns slip through the embedding net.** Pure
vector search struggles with rare tokens that don't sit near a cluster
in the embedding space. "Tamasha" is one example. "HSR Layout" is
another. The Indian-context content captured through the Telegram bot
hits this constantly because all-MiniLM-L6-v2 was trained predominantly
on English Wikipedia + web text — its sense of Bollywood movie names
or Indian neighborhoods is shallow at best.

When the query is "Tamasha meme" and Chroma ranks the actual Tamasha
chunk at position 23, a top-20 pipeline never sees it. The product
silently fails.

**Failure mode 2 — Paraphrased queries miss exact-token search.** The
inverse: a user types "the article about apartment costs in Bangalore"
but the article never uses the words "apartment" or "Bangalore" — it
says "rent," "1BHK," "HSR Layout," and "Bengaluru." Word matching
returns nothing. Vector search, which handles paraphrase natively,
handles this query fine.

The retrieval stack has to handle both, which is why hybrid retrieval
is V.1's central choice.

---

## V.1 — Hybrid retrieval (BM25 + vector)

**Lock:** every recall query runs through Chroma (vector cosine
similarity) **and** SQLite FTS5 (BM25) in parallel against the same
`chunks` table. Both return their own top-K. A fusion step (V.2)
combines them.

**Why both, not one:**

| Query type             | Vector alone | BM25 alone | Hybrid |
|------------------------|--------------|------------|--------|
| Paraphrased            | ✅           | ❌         | ✅     |
| Proper-noun match      | ❌           | ✅         | ✅     |
| Cross-language token   | ❌           | ✅         | ✅     |
| Vague concept          | ✅           | ⚠️         | ✅     |
| Exact-phrase match     | ⚠️           | ✅         | ✅     |

**Where BM25 lives in our stack.** SQLite ships with FTS5, a built-in
full-text-search extension that maintains an inverted index over a
virtual table mirroring `chunks.text`. BM25 ranking is a one-line SQL
function (`ORDER BY bm25(chunks_fts)`). Sub-millisecond on hundreds
of thousands of chunks. No new infrastructure.

**Alternatives considered:**

- **Vector-only + LLM re-rank.** Simpler. Fails on proper-noun queries.
  Rejected — the failure mode is exactly where vague recall must work.
- **BM25-only.** Cheap. Fails on paraphrase. Rejected.
- **Cascaded retrieval** (vector first with big K, then BM25 within).
  Has the same failure mode as sequential — if the proper-noun chunk
  isn't in the vector top-K, BM25 never sees it. Works *most* of the
  time but breaks on pure-proper-noun queries. Rejected — the failure
  mode is too aligned with our content profile.
- **Postgres `tsvector`** instead of FTS5. Identical math. Will become
  relevant when we migrate the SQL layer to Postgres (Phase 3 decision
  A.7), at which point the SQL changes from FTS5 syntax to `tsvector`
  syntax. The retrieval logic is unchanged.

---

## V.2 — Reciprocal Rank Fusion (RRF) for score fusion

**Lock:** combine the two ranked lists with RRF, constant `k=60`:

```
RRF_score(chunk) = Σ (over rankers) 1 / (k + rank_in_that_ranker(chunk))
```

**Why RRF over weighted sum:**

Weighted sum requires calibrating "how much do we trust vector vs
BM25." That weight drifts as the corpus grows — a tiny corpus is
dominated by BM25 (everything looks rare); a large corpus shifts the
balance toward vectors. Re-tuning the weight is a chore.

RRF is parameter-free in this sense — the only constant is `k`, and
60 is the published default that has been robust across many
corpora and retrieval shapes. The constant dampens the gap between
rank 1 and rank 2, so a chunk that lands at rank 3 in both rankers
beats a chunk that aces one ranker and bombs the other. That's the
behavior we want for vague recall: rewards chunks both rankers
agree on.

**Alternatives considered:**

- **Linear-weighted sum** (`α × vector_score + (1-α) × BM25_score`).
  Requires score normalization (the two scales are different), then
  weight calibration. Rejected for v1 — start parameter-free, swap
  to weighted-sum if specific failure modes emerge.
- **Cascaded re-rank** (RRF then BM25 again). Adds no signal we don't
  already have. Rejected.
- **LLM-only ranking** (skip RRF, feed the union of both top-K to
  Sonnet). Too expensive at K=40, plus the LLM still benefits from
  a pre-ordered candidate list. Rejected.

---

## V.3 — Cosine similarity for vector ranking

**Lock:** cosine similarity (which is equivalent to dot product over
unit-normalized vectors) for Chroma's vector scoring.

**Why cosine, not dot product:**

The all-MiniLM-L6-v2 embedder already normalizes its outputs to unit
length, so cosine and dot product give the same answer. Cosine is
locked as the explicit choice because:

1. **Future-proofs against embedder swaps.** If we ever move to
   BAAI/bge-m3 (Phase 3 design noted this as a future-upgrade path
   for multilingual content), some embedders don't normalize. Cosine
   stays correct; raw dot product would silently bias toward longer
   vectors.
2. **Standard semantic.** "Cosine similarity" is what every retrieval
   paper, every textbook, and every Chroma example uses. Encoding the
   convention in code rather than relying on "well, our vectors happen
   to be normalized" prevents accidental regressions.

---

## V.4 — K=20 per ranker, top-6 captures to the LLM

**Lock:** Chroma returns top-20 chunks, FTS5 returns top-20 chunks,
RRF + diversification (V.5) collapses them to top-6 captures, those
6 captures (with their best-matching chunks) go to Sonnet for V.6.

**Why K=20:**

| K   | Behaviour                                                  | Verdict           |
|-----|------------------------------------------------------------|-------------------|
| 5   | Misses the right chunk when either ranker is off; ~1.5kB of context to LLM | Too narrow |
| 20  | Headroom for surprise matches; ~3kB of context to LLM         | **Locked** |
| 50  | LLM re-ranker starts confabulating connections in the long tail; ~8kB of context | Too wide |

K=20 is also the published default in most production hybrid retrieval
papers I've seen.

**Why top-6 captures after fusion:**

After diversification (V.5) we collapse multiple chunks per capture
into one. Six captures is enough for the LLM to genuinely choose
("here are the 6 most-likely answers, pick the best one") without
the LLM having to scan a long tail. Less than 6 → no real selection
happening; more than 6 → LLM picks rank-3 type results too often
because they have more chances to look interesting.

---

## V.5 — Group-by-capture diversification

**Lock:** after RRF, walk the ranked chunk list and keep the
first-seen chunk per `capture_id`, discarding subsequent chunks from
that capture. Then take the top-6 captures.

**Why:**

A long article can have 3-4 chunks all ranking well for a query. If
those four chunks fill the candidate pool, the LLM only ever sees
one source — the diversity that makes vague recall useful disappears.
Diversification ensures the LLM sees up to 6 *different* captures.

**Edge case accepted:**

When a capture has both the best and second-best chunk for a query
(very on-topic capture), we still show it only once. The snippet
shown is whichever chunk ranked highest. This is the right trade —
diversity beats completeness for vague recall, because the user is
already going to be sent to the original capture if they want more.

---

## V.6 — Sonnet 4.6 as re-ranker and answer composer

**Lock:** the top-6 captures from V.5 go to Claude Sonnet 4.6 with
a JSON-output prompt. Sonnet returns:
- A ranked list of the candidates by how well each answers the query
- A `why_this_matches` reasoning per candidate
- A confidence score (0-1) per candidate
- A one-sentence conversational answer for the brief mode (U.1)

**Why a separate model from enrichment:**

The enrichment pipeline uses Haiku 4.5 because per-capture enrichment
is cost-bound — it runs on every single capture, possibly hundreds
per week. Recall is quality-bound and rate-limited by human typing
speed. Sonnet at ~$0.003/query is fine for the volumes a single user
generates.

**Why an LLM re-ranker at all:**

| Approach                       | Quality                     |
|--------------------------------|-----------------------------|
| Trust RRF rank 1 directly      | ~70-80% correct first try   |
| LLM re-rank top-6              | ~90-95% correct first try   |

The 20-30% miss rate without re-ranking is exactly where vague recall
fails worst — "almost the right one" is worse than "no answer" because
the user reads the wrong article thinking it's the one they meant.
The LLM re-ranker catches semantic mismatches that the embedder
missed and weights for relevance rather than just similarity.

**Cost reference:**

- Sonnet prompt: ~3kB context (6 candidates × ~500 tokens of summary
  + matching chunk) + ~200 tokens query / instructions
- Output: ~600 tokens (ranked list + reasoning + answer)
- Per query: ~$0.003 at current Sonnet pricing

For a heavy user running 30 recall queries a day, that's $0.09/day or
~$2.70/month. Within budget.

---

## V.7 — Confidence threshold and disambiguation

**Lock:**
- Top-1 confidence < 0.6 → return a "I don't think this is in my
  corpus" response with the closest miss as a courtesy (U.4)
- Top-1 and top-2 within 0.1 confidence of each other → return both,
  ask the user to disambiguate

**Why these specific thresholds:**

The 0.6 number is a starting estimate based on typical Sonnet
confidence on RAG tasks. It WILL need calibration once we have ~100
real queries to evaluate. Plan: log confidence + user reaction (did
they click through? did they refine?) so we can fit the threshold
empirically.

The 0.1 spread for disambiguation is similarly an opening guess. If
candidates 1 and 2 are this close, the LLM is essentially saying "I
can't tell"; better to ask than to guess.

**Why this matters for UX:**

Confident-wrong is the worst possible product behaviour for a memory
prosthetic. Honest-uncertain ("I have two candidates, which one?")
is better than confident-uncertain. Phase 4's success metric is
*trust*: does the user reach for BrainTwin again after a failed
recall? Failing gracefully is what preserves that.

---

## V.8 — Per-chunk entity tagging — deferred

**Lock:** continue with the current capture-level → all-chunks topic
and entity attachment (`backend/storage/sync.py:_sync_topics_and_entities`)
for Phase 4. Per-chunk tagging gets revisited in Phase 4.2 (clue
inference game).

**Why deferred:**

Per-chunk tagging would let retrieval filter by entity at the chunk
level — much tighter than the current "every chunk of this capture
mentions Anthropic" approximation. But:

1. **Vague recall works fine with capture-level tagging.** Entity
   filtering is a small accuracy lift for vague recall queries; the
   big lifts come from V.1, V.2, V.6.
2. **Per-chunk tagging requires a chunk-level LLM pass at enrichment
   time.** That's roughly 3x the per-chunk Haiku cost and adds latency
   to every capture, not just queries.
3. **The big payoff is on use case C.** Clue inference is the use case
   that genuinely needs to know "which exact chunk mentions Deepika in
   a dance context." Phase 4.2 will lift this when it actually pays off.

---

## U.1 — Brief mode is the default response shape

**Lock:** every recall response is a one-sentence conversational
reminder generated by Sonnet, followed by one or more result blocks.
Synthesis mode (a 3-4 sentence summary in the response itself) ships
later as an opt-in flag.

**Why brief:**

The user (Sabya) is the design partner here and chose brief. The
reasoning that lands:

- Click-through to the source is the natural "give me more" affordance
- Saving the synthesis tokens reduces cost
- A user who actually wants to dive deeper opens the original capture
  — that's already a one-click action

Synthesis mode will exist later as `?mode=deep` (or similar) on the
endpoint for the "I'm driving, read it to me" case.

---

## U.2 — Result block fields

**Lock:** every result block returns exactly these fields:

```json
{
  "title":              "HSR Layout 1BHK rents jump as supply tightens",
  "source_domain":      "hindustantimes.com",
  "captured_at":        "2026-04-27T12:14:00+00:00",
  "client":             "chrome",
  "dwell_time_seconds": 47,
  "why_this_matches":   "you asked about 'Bengaluru rents going up' — this piece covers exactly that…",
  "snippet":            "…HSR rents have climbed roughly 40%…",
  "original_url":       "https://www.hindustantimes.com/cities/bengaluru-news/…",
  "summary":            "(enrichment summary — fallback if snippet is unhelpful)",
  "confidence":         0.81
}
```

**Why each field:**

| Field                 | Why it's there                                          |
|-----------------------|---------------------------------------------------------|
| `title`               | What the user remembers when they see the result        |
| `source_domain`       | "Oh, that was a Hindustan Times piece"                  |
| `captured_at`         | Lets the user reason about recency                      |
| `client`              | "Was this an article I read or a thing I forwarded?"    |
| `dwell_time_seconds`  | Triggers "the article I spent a long time on" recall    |
| `why_this_matches`    | Tells the user why the system chose this result         |
| `snippet`             | Triggers content-level recall without click-through     |
| `original_url`        | The direct answer to "where did I read this?"           |
| `summary`             | Fallback when the matching chunk isn't recall-triggering |
| `confidence`          | Drives client-side rendering of low-confidence results  |

Shape is locked so Chrome and Telegram render once, deterministically.

---

## U.3 — Multi-turn refinement via conversation_id

**Lock:** the `/recall` endpoint accepts an optional `conversation_id`
field. When present, the server treats the current query as a
*filter* layered on the previous query's candidate pool, not as a
fresh search.

**Concrete example:**

```
POST /recall { "query": "the article about hash collisions" }
→ { "conversation_id": "abc-123", "results": [knowledge_graph, data_structures_video] }

POST /recall { "query": "more recent than wikipedia", "conversation_id": "abc-123" }
→ filters previous candidates by date + excludes wikipedia.com source
→ { "results": [data_structures_video] }
```

**Server-side state:** an in-memory dict keyed by `conversation_id`,
storing the last query's candidate pool + the LLM's interpreted
filters. Evicted after 30 minutes of idle. Lost on process restart —
acceptable because conversations are intentionally short.

---

## U.4 — Closest-miss fallback

**Lock:** when no candidate meets the confidence threshold (V.7), the
response is:

```json
{
  "answer":     "I don't think this is in my corpus. The closest match I have is …",
  "confidence": 0.42,
  "results":    [ <best candidate anyway> ],
  "no_match":   true
}
```

**Why not just return empty results:**

Silent "no match" is the worst case for a memory prosthetic — the
user can't tell whether their memory is wrong, the search is wrong,
or the capture never happened. Returning the closest candidate with
explicit "this might not be it" framing preserves value: maybe it
*is* the one and the user just remembered fuzzily, or at minimum
the user knows the search ran.

---

## S.1 — Chrome extension is the first surface

**Lock:** Phase 4 ships the "Remember" tab inside the existing
extension popup. The popup gets a tab switcher (Capture | Remember).
The Remember tab is a single input + a result list rendering U.2's
result blocks as cards.

**Why Chrome first:**

The user is laptop-heavy (Sabya). Capture and recall in the same
surface closes the loop — see a page, capture it, search for it
again later, all from the same browser button. The popup
infrastructure already exists; extension messaging to `/recall` is
a thin add.

**What the UI looks like (rough):**

```
┌──────────────────────────────────────────┐
│ BrainTwin                                │
│ ┌──────────┐ ┌───────────┐               │
│ │ Capture  │ │ Remember  │  ← tab switch │
│ └──────────┘ └───────────┘               │
│                                          │
│ [the kanban article about team size  ]   │
│                              [ Search ]  │
│                                          │
│ ─── 1 result ──────────────────────────  │
│                                          │
│ I think you're remembering the Atlassian │
│ piece from Apr 18 — it argues small      │
│ teams with tight WIP limits outperform.. │
│                                          │
│ ┌────────────────────────────────────┐   │
│ │ Atlassian: WIP limits and team siz │   │
│ │ atlassian.com · Apr 18 · Chrome    │   │
│ │ (3min dwell)                       │   │
│ │                                    │   │
│ │ Why: matches "team size"...        │   │
│ │ "…optimal team size is 5-7..."     │   │
│ │                                    │   │
│ │ Open original →                    │   │
│ └────────────────────────────────────┘   │
└──────────────────────────────────────────┘
```

---

## S.2 — Telegram bot is the second surface

**Lock:** Phase 4.0.1 (separate ship from the core 4.0) adds a
`/recall <query>` command to the bot. Non-URL text messages get
auto-routed through `/recall` so you can just type a query naturally.

**Why second:**

Chrome is the laptop surface. Telegram is the phone surface. The
phone use case is real but secondary for this user. Better to nail
the laptop experience first than split design budget across both.

---

## S.3 — POST /recall endpoint

**Lock:** one backend endpoint serves both surfaces.

```
POST /recall
Content-Type: application/json

{
  "query":           "the kanban article about team size",
  "conversation_id": "abc-123"   // optional
}

Response 200:
{
  "answer":         "I think you're remembering the Atlassian piece...",
  "confidence":     0.74,
  "results":        [ <result block 1>, <result block 2> ],
  "conversation_id": "abc-123",
  "no_match":       false
}
```

The same endpoint handles refinement (via `conversation_id`),
no-match (via `no_match: true`), and the success path. The Chrome
extension and Telegram bot both render against the same response
shape.

---

## Implementation milestones

The Phase 4.0 work breaks into 6 milestones. Each one is a
shippable-and-testable chunk; landing them in order means we can
evaluate retrieval quality before committing to the LLM re-ranker
prompt design.

### M.1 — FTS5 chunks index + sync ✅ built

- `chunks_fts` virtual table declared as raw DDL strings in
  `backend/storage/schema.py` (SQLAlchemy Core has no clean model for
  FTS5 virtual tables). External-content (`content='chunks'`,
  `content_rowid='id'`) so the actual text bytes only live in
  `chunks.text` — no storage duplication.
- Tokenizer: `unicode61 remove_diacritics 2`. Normalizes accents
  (café/cafe both index the same way) without conflating distinct
  proper nouns (Bengaluru ≠ Bangalore — that's vector search's job).
- Three SQLite triggers (`AFTER INSERT`, `AFTER UPDATE OF text`,
  `AFTER DELETE`) keep `chunks_fts` in sync with `chunks`. The
  `INSERT INTO chunks_fts(chunks_fts, …) VALUES('delete', …)` dance
  is the documented external-content FTS5 pattern.
- Migration in `backend/storage/db.py` (`_apply_pending_fts_setup`)
  consults `sqlite_master` for each object and only issues the CREATE
  if absent — idempotent, same shape as the Phase 3.5 ALTER TABLE
  sweep. Dialect-checked: SQLite-only; on Postgres the equivalent
  setup will be a `tsvector` column + maintenance trigger.
- `ChunkRepository.search_by_bm25(query, user_id, limit=20)` runs the
  FTS5 query JOINed back to `captures` for tenant filtering. Returns
  `list[ChunkWithScore]` with the BM25 score sign-flipped so
  higher = better (matches the upstream convention).
- `ChunkRepository.get_by_ids(chunk_ids, user_id)` added for the
  hybrid retrieval pipeline (M.2 vectors return IDs only).
- `scripts/backfill_chunks_fts.py` — one-shot rebuild using FTS5's
  `'rebuild'` command, dry-run flag. For DBs that pre-date M.1 or
  drift out of sync with the index.
- Tests: `tests/test_chunks_fts.py` — init creates the table + 3
  triggers, init is idempotent, INSERT trigger auto-indexes, UPDATE
  re-indexes, DELETE drops from index, BM25 ranks more-specific
  matches higher, empty query returns empty, limit caps results,
  cross-tenant chunks invisible, the Tamasha proper-noun case from
  the design rationale, diacritic normalization works.

### M.2 — RetrievalService ✅ built

- New module `backend/agent/retrieval.py`
- Class `RetrievalService` exposing
  `recall(*, query, user_id, per_ranker_top_k=20, top_captures=6, rrf_k=60) → RetrievalResult`
- Result dataclasses: `CandidateChunk` (chunk + fused score + per-ranker
  provenance), `CandidateCapture` (capture + best chunk), `RetrievalResult`
- Internally:
  - Embed query once via shared embedder (sync, called before opening
    the SQL session so the model run doesn't hold a connection)
  - Chroma `query()` and `ChunkRepository.search_by_bm25` run **in
    parallel via `asyncio.gather`** — roughly halves retrieval latency
    on warm caches vs serial
  - Pure `_fuse()` helper applies RRF math (testable in isolation)
  - Vector hits give us IDs only; we hydrate the missing chunks via
    one batched `ChunkRepository.get_by_ids` (BM25 hits already carry
    the full `Chunk` rows so no double-fetch)
  - Per-capture diversification keeps the first chunk per `capture_id`
    in fused-score order, drops later ones
  - Top-6 capture cap applied after diversification; parent `Capture`
    rows hydrated one-by-one (small N, batching is premature)
- Tenant isolation: defence-in-depth via Chroma's `where={"user_id": …}`
  filter AND the SQL `get_by_ids` tenant check. Tests deliberately
  feed cross-tenant vector hits through and verify they get filtered.
- Tests: `tests/test_retrieval_service.py` — empty/whitespace short-
  circuit, vector-only path, BM25-only path, hybrid RRF (chunk in
  both rankers outranks chunk in one), diversification, top_captures
  cap, tenant isolation, vector_store args plumbing, pure fusion math
  with crafted inputs.

### M.3 — Recaller agent

- New module `backend/agent/recaller.py`
- Class `Recaller` wrapping `RetrievalService` with the Sonnet
  re-rank prompt
- Owns conversation-state dict for U.3
- Manages confidence threshold and closest-miss logic (V.7, U.4)
- Returns a `RecallResponse` dataclass with the exact shape of S.3
- Prompts and JSON schemas live in `backend/agent/prompts.py`

### M.4 — POST /recall endpoint

- Add to `backend/main.py`
- Thin handler over `Recaller`
- Default user (Sabya, `user_id=1`) for now; multi-user auth lands
  with use case A
- Same FastAPI pattern as `/capture`

### M.5 — Chrome extension Remember tab

- Add tab switcher to `extension/popup.html`
- Add Remember view (input + result card template)
- New `extension/recall.js` that posts to `/recall` and renders
- Reuses CORS config that already permits the extension origin
- Conversation state lives in `chrome.storage.local` keyed by tab,
  cleared on tab close

### M.6 — Telegram bot /recall command (Phase 4.0.1)

- Extend `backend/telegram_bot/handlers.py` with a `/recall` command
- Add a fallthrough that routes any non-URL text through `/recall`
- Result formatting tuned for Telegram markdown (bold title,
  blockquote snippet, clickable URL)
- Re-uses the same `Recaller` instance the FastAPI app uses

---

## What we are NOT building in Phase 4.0

Explicitly out of scope, with the rationale for each:

- **Synthesis quizzes (use case A).** Separate design doc when
  vague-recall behaviour is known. Need real query data to calibrate
  the quiz quality bar.
- **Clue inference game (use case C).** Same — separate design doc
  when per-chunk entity tagging is on the table.
- **Per-chunk topic/entity tagging.** Deferred to Phase 4.2 (V.8).
- **Multi-user authentication on /recall.** Phase 4.x when use case A
  goes live and real other users show up.
- **`related_captures` cross-capture relations.** Deferred from Phase 2
  (Decision I); will be revisited when synthesis quizzes need a graph.
- **Negative feedback signal.** Recording "user said no, not that one"
  as training data for retrieval is in scope conceptually but not the
  first ship. Phase 4.1 once we have refinement turns to learn from.
- **Result diversification beyond per-capture.** No diversity rules
  on topic or recency for v1. If retrieval feels monotonous in
  practice, revisit.

---

## Decisions explicitly NOT locked yet

These will calibrate against real usage after the first ship:

- **The confidence threshold value** (0.6 is a guess — tune after
  ~100 queries with logged ground truth)
- **The disambiguation spread** (0.1 between top-1 and top-2 — same
  calibration story)
- **Default visible result count** (top-3 or top-5 in the UI —
  whichever feels right; backend always returns up to 6)
- **Conversation TTL** (30 minutes — likely correct, but no data yet)
- **Brief vs synthesis mode UI affordance** (CLI flag for v1, real
  UI toggle later if synthesis mode catches on)

---

## Success metric

The single number that tells us Phase 4 worked: **first-result hit
rate.** After the user runs a recall query, how often do they:

- Click through on the first result (confirms it was correct), or
- Add a refinement turn (confirms it was close), or
- Bail out (confirms it failed)

Goal for v1: ≥80% click-through-or-refinement on the first response,
≤20% bail-outs.

Measurement: log every `/recall` request and the next action by the
same `conversation_id`. Build a small `/admin/recall-stats` view to
read these.

---

## Next docs after Phase 4.0 ships

- `phase4-vague-recall-smoke-test.md` — the numbered verification
  flow (mirror of `phase2-smoke-test.md`, `phase3-smoke-test.md`)
- `phase4.1-quizzes-design.md` — design notes for use case A once we
  have a month of real recall usage
- `phase4.2-clue-game-design.md` — design notes for use case C,
  including the per-chunk entity tagging story

---

*Document author: Claude, in design conversation with Sabya. Decisions
captured 2026-06-02.*
