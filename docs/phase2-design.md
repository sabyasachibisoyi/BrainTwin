# Phase 2 Design — Processing Pipeline (LLM Enrichment)

> **Status as of 2026-04-28 — PHASE 2 LIVE. Follow-up Phase 2.5 in design.**
>
> All 10 decisions (A, B, C, D, E, F, G, H, I, J) signed off, all files built, unit tests green, end-to-end smoke test passed on your laptop. Phase 2's enrichment pipeline runs as designed.
>
> **Smoke testing surfaced one real gap that's not an enrichment bug:** forwarding Instagram reels and Facebook share links from Telegram lands those captures in `capture_failures.jsonl` with `reason: "empty_content"`. Phase 2 correctly refused to call Haiku on `""` — but the empty content was a Phase 1 capture-layer hole (the bot sends URL-only payloads, the backend has no fetcher for non-YouTube URLs). Fixing it cleanly requires capture-time hydration plus enrichment-failure-log hygiene, which together are scoped as **[Phase 2.5 — Capture Hydration & Enrichment Hygiene](phase2.5-capture-hydration.md)**.
>
> Cross-check against `docs/architecture.html` (Phase 2 + Phase 2.5 cards) and `docs/architecture-detailed.md` (LLM Enrichment section) for the broader system context.

---

## What Phase 2 does

Take the raw text Phase 1 captures (article body, YouTube transcript, image descriptions) and add a structured `enrichment` block to each row in `data/captures.jsonl`. The block contains a summary, named entities, key facts, and topic tags, produced by Claude Haiku. The enriched data becomes the substrate Phase 3 (storage in ChromaDB + SQLite) and Phase 4 (the quiz agent) will retrieve and reason over.

Phase 2's success criterion: **a captured article in `captures.jsonl` carries a 4-field enrichment block that a downstream agent could meaningfully retrieve from**.

## The seam in code (where Phase 2 plugs in)

```
POST /capture
   → process(capture)              # Phase 1: extract + vision + transcript
   → enrich(processed)             # Phase 2 NEW: Claude Haiku enrichment
   → append to data/captures.jsonl # Phase 1 persistence (unchanged)
```

Important rule: **if enrichment fails, write the row to JSONL without the `enrichment` field anyway.** Raw data is sacred — we never lose a capture because Haiku timed out, returned bad JSON, or hit a rate limit. The failure goes to `data/capture_failures.jsonl` with `phase: "enrichment"` so the existing `/failures` endpoint surfaces it.

There's also an obvious skip case: if `clean_text` is empty AND no transcript AND no image descriptions (the Google News symptom), there's nothing to enrich — log `reason: "empty_content"` and skip the API call. Don't waste a Haiku call on `""`.

## The enrichment schema (LOCKED — 4 fields)

```json
{
  "enrichment": {
    "summary": "1-2 sentence summary of the content.",
    "entities": [
      {"name": "Bengaluru", "type": "place"},
      {"name": "Hindustan Times", "type": "org"}
    ],
    "key_facts": [
      "1BHK rents in HSR start at ~₹25,000",
      "Renter's stated budget was ₹15,000"
    ],
    "topics": ["bengaluru", "rent-crisis", "indian-cities", "real-estate"],
    "model": "claude-haiku-4-5-20251001",
    "enriched_at": "2026-04-28T01:30:00Z"
  }
}
```

Rationale for each field — every field had to earn its place by being something the Phase 4 quiz agent will actually query on:

- `summary` → the agent's grounding context per retrieved chunk.
- `entities` → powers *"what do you know about Deepika Padukone?"* lookups.
- `key_facts` → the literal answer source for quiz questions. The single highest-leverage field.
- `topics` → powers *"show me everything about Indian politics last week"* filtering.

Deliberately deferred from this list:

- `connections` (architecture-detailed lists it) — connections only mean something once *other* captures exist to point at, so it's a Phase 3+ problem after we have ChromaDB.
- `sentiment`, `language`, `content_type_refined` — none are load-bearing for Phase 4. They can be added in Phase 5 by re-running the backfill script with an updated prompt.

---

## Decisions log

| # | Decision | Locked? | Choice | Why |
|---|---|---|---|---|
| A | Schema scope | ✅ | 4 fields: `summary`, `entities`, `key_facts`, `topics` | Each earns Phase 4 query value. Add more later via re-enrichment if Phase 5 needs them. |
| F | Model choice | ✅ | Haiku for enrichment, Sonnet for the agent. Both behind a model-agnostic `LLMClient` interface so a local Llama can be swapped in later. No fine-tuning, no training from scratch. | Fine-tuning teaches *style* not *changing facts*. RAG is the right tool when your knowledge base updates daily. Local-Llama A/B test is a post-Phase-5 experiment. |
| G | Article extraction strategy | ✅ | Send full text up to ~50k tokens. No pre-filtering / keyword extraction / chunking at MVP. Map-reduce only if a single capture exceeds 50k tokens (rare — basically only multi-hour podcasts and books). | Haiku context is 200k. A 1000-word article is 0.6% of that. Pre-filtering saves no money and loses nuance. |
| E | Hallucination control in Phase 4 agent | ✅ | **v1: inline citations in the same Sonnet call.** System prompt forces the agent to cite a snippet ID for every claim or omit the claim. Adds ~10% output tokens. **v2: two-pass verification** is deferred — only build it if v1 leaks visibly during real quiz play. | "Doubles cost" framing was wrong (corrected: verification adds ~50%, not 100%). But cheaper-and-good-enough beats expensive-and-pre-emptive. Measure first, decide second. |
| D | Language fidelity (multi-language consumption: English / Hindi / Odia / Telugu / German) | ✅ | **One unified rule for all languages.** Enrichment output in English with Latin script throughout. Names transliterated (Romanized), not translated — `दीपिका पादुकोण` becomes `Deepika Padukone`, `ఎన్టీఆర్` becomes `NTR`. Cultural keywords, idioms, untranslatable phrases preserved verbatim in their Romanized form within summaries and key_facts (`jugaad`, `kal mein soya tha`, `Schadenfreude`). System prompt explicitly tells Haiku that source content can be in any of these five languages, often code-switched. | Translation loses meaning; transliteration loses only script. Latin-script-everywhere is the lowest-common-denominator that you can read across all five languages and that the agent can retrieve over consistently. Per-language exceptions are brittle. Native-script storage (`name_native`) deferred to Phase 5 if a real use case demands it (e.g., quizzing on Hindi spelling literacy). |
| B | Backfill existing JSONL rows | ✅ | **Backfill but skip test rows.** `scripts/backfill_enrichment.py` reads `data/captures.jsonl`, skips rows that look like test fixtures (URL contains `example.com`, `metadata.source` missing, `clean_text` empty, title is `"Telegram link"` with no body), enriches the rest. Idempotent — re-running skips already-enriched rows. | Preserves real captures (HSR rent article, etc.) without polluting the agent's knowledge base with mock-script fixtures. ~$0.05 in Haiku calls total. The test-row classifier is ~10 lines of conditional checks. |
| H | Retry policy on enrichment failure + sync vs async | ✅ | **Enrichment runs ASYNC via FastAPI `BackgroundTasks`, not in the `/capture` request path.** `/capture` writes the raw row to `data/captures.jsonl` (with a generated `capture_id`) and returns 200 immediately. Background task calls Haiku with internal retry logic: 3 attempts on transient errors (network, 5xx, rate limit) with 0.5s/1s/2s backoff, 1 retry on malformed JSON with stricter prompt, no retry on permanent errors (4xx auth, content-too-long). On success, appends to sibling file `data/enrichments.jsonl` keyed by `capture_id`. On all-retries-exhausted, logs to `capture_failures.jsonl` with `phase: "enrichment"` and the row stays unenriched. **Crash recovery**: on FastAPI startup, scan for `capture_id`s in `captures.jsonl` that have no matching `enrichments.jsonl` row and re-queue them. **Manual catch-up**: `scripts/retry_failed_enrichments.py` does the same scan on demand. | Async is the right architectural answer — enrichment latency must not block capture. Two-file append-only design avoids JSONL update locking, matches Phase 3's natural SQLite shape (INSERT then UPDATE). BackgroundTasks is fine for single-worker FastAPI; multi-worker would need a real queue (Redis+RQ) but that's a Phase 5+ cloud-move concern. |
| I | Cross-language duplicates of the same content | ✅ | **Phase 2: don't try to detect. Store both as separate captures.** Reserve `related_captures: []` empty field in the enrichment schema so Phase 3 can populate it without migration. Phase 3 adds embedding-based linking when ChromaDB lands: when a new capture is enriched, query ChromaDB for anything within last 7 days >0.85 cosine similarity, store mutual `related_captures` IDs. | Detecting duplicates pre-Chroma requires building parallel embedding infra; Chroma is Phase 3 anyway. Storing both loses no data — agent will surface both via topic/entity overlap even without explicit link. Merging would lose framing differences (Hindi version vs English version emphasize different things). |
| J | Knowledge freshness when facts change over time | ✅ | **Phase 2: just ensure every row has timestamps (`enriched_at` + source `timestamp`) — already happens.** Phase 4 agent prompt does the actual freshness work: instruct the agent to cite the timestamp of every retrieved snippet and to flag potential staleness ("most recent capture on this is from April 22 — may have changed"). Schema reserves room for a future `superseded_by` field. | Temporal RAG is a known hard problem we're not solving in Phase 2. What we CAN do is preserve the data needed to do better later. Active contradiction detection (~$0.005/capture extra Haiku call to compare against existing facts on the same entity) is a Phase 5+ feature — only build it after real quiz sessions reveal stale-answer pain. |
| C | Telegram inline reply on enrichment failure | ✅ | **Silent on enrichment failure.** Capture-time failures (fetch, parse, process) still get inline `⚠️ Couldn't process: <reason>` per Phase 1 Decision 1 — unchanged. Enrichment-time failures are logged to `capture_failures.jsonl` with `phase: "enrichment"` and surface via the bot's `/failures` command (extended to group by phase: "3 capture failures, 5 enrichment failures"). Future Phase 5 daily digest agent surfaces sustained failures. | The capture itself succeeded — raw data is preserved. User can't act on enrichment failure (only the system can retry). And inline reply would require the backend to call back into the bot, breaking Phase 1's "backend never initiates Telegram messages" rule. The `/failures` command + future digest cover discovery without architectural cost. |

---

## Cost forecast (locked numbers)

| Operation | Tokens | Cost per call | At your projected usage |
|---|---|---|---|
| Enrichment per capture (Haiku) | ~2k total | ~$0.003 | 50 captures/day → ~$5/mo |
| Quiz answer with inline citations (Sonnet) | ~5k in, 550 out | ~$0.024 | 20 questions/wk → ~$2/mo |
| **Optional verification pass v2 (Sonnet)** | ~3k in, 200 out | ~$0.012 | If we add v2 → +$1/mo |

**Total realistic monthly: $7–10/mo at projected usage. Even 5× usage stays under $50/mo.** Earlier "doubles the cost" claim was wrong; verification is a $1/mo decision, not a $50/mo decision.

---

## File map (planned, not built)

| File | Role |
|---|---|
| `backend/knowledge/enrichment.py` | `enrich(processed) -> dict` async function. Builds prompt, calls Claude Haiku via `LLMClient`, validates JSON response against Pydantic schema, returns dict (or raises typed `EnrichmentError`). |
| `backend/knowledge/prompts.py` | Enrichment system prompt + 1–2 worked examples. Separated so we can iterate on prompt without touching call code. |
| `backend/knowledge/llm_client.py` | Model-agnostic LLMClient wrapping the `anthropic` SDK. Methods: `enrich(text) -> dict`, `answer(question, snippets) -> str`. Future-proofs swap to local Llama. |
| `backend/knowledge/enrichment_worker.py` | Async background task. Wraps `enrich()` with retry logic (3 transient + 1 JSON), writes successful results to `data/enrichments.jsonl`, writes failures to `data/capture_failures.jsonl`. Called via FastAPI `BackgroundTasks` from `/capture`. |
| `scripts/backfill_enrichment.py` | Idempotent backfill over existing `data/captures.jsonl`. Skips rows that already have a matching `capture_id` in `enrichments.jsonl`, plus the test-row classifier from Decision B. Atomic appends, respects 800ms rate-limit pattern from Telegram client. |
| `scripts/retry_failed_enrichments.py` | On-demand scan: finds `capture_id`s in `captures.jsonl` with no matching row in `enrichments.jsonl`, re-runs enrichment on them. Same logic as the FastAPI startup recovery scan, exposed as a CLI for manual use. |
| `tests/test_enrichment.py` | Mock Anthropic client; verify schema validation, retry logic, "empty content" skip case, sidecar-write behavior. |

Plus:
- `CapturePayload` model in `backend/main.py` gains a `capture_id: str` field (auto-generated UUID4 if not provided) so the raw row and the eventual enrichment row can be joined.
- `/capture` handler schedules enrichment via `BackgroundTasks` after writing the raw row.
- FastAPI startup hook scans for unenriched rows and queues them.
- New file: `data/enrichments.jsonl` — append-only sidecar, one row per successful enrichment, keyed by `capture_id`.

---

## Future improvements (deferred from Phase 2)

| Item | What | Why deferred |
|---|---|---|
| **Per-user language config** | Move the "5 languages" list out of the hardcoded enrichment prompt and into a `.env` setting (`USER_LANGUAGES=english,hindi,odia,telugu,german`). The prompt builds dynamically from this list. Lets a friend who reads Mandarin / Tamil / Spanish fork the repo and configure for their own consumption profile without touching code. | BrainTwin-the-product is currently a single-user system tuned for one operator. Open-sourcing for friends is a Phase 5+ concern. The hardcoded prompt for v1 is fine — it lifts cleanly into a configurable prompt later (one variable substitution). |
| **Native-script entity field** | Optional `name_native` per entity — preserves source-script spelling alongside the Romanized form. Backfilled via a re-enrichment pass when needed. | Locked Decision D's "Latin script everywhere" rule covers all current use cases. Add `name_native` only when a real Phase 5 quiz use case demands it (e.g., script-literacy quizzes). |
| **Per-language summary variants** | `summary_native` field for content where preserving cultural tone in the source language is more useful than English-only. | Same logic — solve when a real use case shows up, not pre-emptively. |

---

## Open polish carried forward from Phase 1 (don't block Phase 2)

- **Google News URL extraction** — forwarded GN links result in `clean_text: ""` because GN wraps everything in encrypted click-through redirects. Either follow the redirect chain in `backend/capture/extractors.py`, or detect-and-reject with a clear failure reason. Knockable as warm-up before starting enrichment work.
- **Bot file logging** — pipe `braintwin.telegram` logger to `data/bot.log` (rotating handler) so unattended bot runs are debuggable.

---

## Next: smoke-test on your laptop

All planned files in the file map above are written, compile, and the unit tests are in place. To turn the green-light on Phase 2:

1. Set `ANTHROPIC_API_KEY=sk-ant-...` in `.env`.
2. Follow [docs/phase2-smoke-test.md](phase2-smoke-test.md) — 7 numbered passes from offline unit tests through bot `/failures` integration.
3. When all 7 are green, flip this doc's status from "AWAITING SMOKE TEST" to "PHASE 2 LIVE" and start Phase 3 (storage layer — ChromaDB + SQLite).

---

## Phase 2.5 follow-up (2026-04-28)

Smoke testing passed all 7 numbered passes, but real-world use immediately surfaced an issue that Phase 2 cleanly refused to mask:

**Bug:** Forwarding Instagram reels (`/reel/...`, `/p/...`) and Facebook share links (`/share/...`) to the Telegram bot writes capture rows with `text=""`, `images=[]`, `title="Telegram link"` — and the enrichment worker then logs them as `phase: "enrichment", reason: "empty_content"` failures.

**Diagnosis:** Not an enrichment bug. The bot sends URL-only payloads; the backend's `extract()` only knows YouTube; nothing fetches IG/FB content; the enricher correctly refuses to call Haiku on nothing. The misleading comment `"backend fetches the page itself"` in `handlers.py:333` is a smoking gun — it claims behavior the backend never had.

**Fix scope:** [Phase 2.5 — Capture Hydration & Enrichment Hygiene](phase2.5-capture-hydration.md) — four ordered, independently shippable fixes (~6 hours total, ~$0 recurring):

1. **Hygiene:** re-tag `EmptyContentError` / `ContentTooLongError` as `phase: "enrichment_skipped"` so the failure log only counts real failures.
2. **Free metadata layer:** Telegram link-preview pickup + Open Graph fetcher in `processor.py`. Hydrates ~80% of URL-only captures for $0.
3. **Local video transcription:** `yt-dlp` + `whisper.cpp small.en` for IG reels / FB videos. Captures actual spoken content for $0/mo (250 MB disk).
4. **Verify + doc:** replay the original failure URLs, document the new tier order in `docs/phase2-smoke-test.md`.

Phase 3 (storage layer) starts after Phase 2.5 is green.
