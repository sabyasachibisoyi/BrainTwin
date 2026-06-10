# BrainTwin

Your Knowledge Twin Agent — captures everything you consume digitally, builds a living knowledge base, and competes against you in knowledge battles.

## Quick Start

### Prerequisites
- Python 3.11+
- Cursor IDE (recommended)
- Chrome browser
- Claude API key from [console.anthropic.com](https://console.anthropic.com)
- Telegram account (for mobile capture)

### Setup

```bash
# 1. Clone/navigate to the project
cd ~/Desktop/LLM/BrainTwin

# 2. Create virtual environment
python3 -m venv venv
source venv/bin/activate   # Mac/Linux
# venv\Scripts\activate    # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy env template and add your API keys
cp .env.example .env
# Edit .env with your Claude API key and Telegram bot token

# 5. Run the backend
uvicorn backend.main:app --reload --port 8000

# 6. Load the Chrome extension
# Go to chrome://extensions → Enable Developer Mode → Load Unpacked → select /extension folder
```

### Inspecting captures & enrichment

Handy commands for verifying that a capture made it through the full Phase 1 + Phase 2 pipeline (raw text → Haiku enrichment → SQL + Chroma). Run these from the repo root with the backend running.

Post-Phase-3.5 the three knowledge JSONLs (`captures.jsonl`, `enrichments.jsonl`, `hydrations.jsonl`) are gone. SQL (`data/braintwin.db`) and Chroma (`data/chroma/`) are the sole stores. The only JSONL anything still reads is `data/capture_failures.jsonl`, kept as an operational log (see [docs/phase3.5-cutover.md](docs/phase3.5-cutover.md), decision 2).

**1. High-level counts:**

```bash
curl -s http://127.0.0.1:8000/stats | python -m json.tool
```

You want `total_captures` and `enrichments.total` to both go up by 1 after each capture, and `enrichments.pending` to settle to `0` once the background worker finishes (usually within a few seconds).

**2. Drill into a specific capture across SQL + Chroma:**

```bash
python scripts/inspect_storage.py --capture-id <uuid>
```

This is the workhorse command for debugging post-3.5. It shows the SQL row, hydration, enrichment, every chunk + source_kind, and the matching Chroma vectors. Replaces the old "tail captures.jsonl + grep enrichments.jsonl" combo.

**3. Confirm no enrichment failure was logged for that capture:**

```bash
grep "$CID" data/capture_failures.jsonl 2>/dev/null || echo "(no failures for $CID — good)"
```

**4. Inspect failures by phase (capture vs enrichment):**

```bash
curl -s 'http://127.0.0.1:8000/failures?phase=enrichment&limit=5' | python -m json.tool
curl -s 'http://127.0.0.1:8000/failures?phase=capture&limit=5'    | python -m json.tool
```

**5. If `enrichments.pending` won't go to 0**, the worker either failed or is still running. Check the uvicorn log for an `enrich[<first-8-of-CID>]` line, then manually retry. Retry walks SQL (no JSONL needed):

```bash
python scripts/retry_failed_enrichments.py --dry-run    # show what's missing
python scripts/retry_failed_enrichments.py              # actually retry
```

**6. Backfill enrichment over older captures (idempotent — skips test rows and already-enriched captures via SQL):**

```bash
python scripts/backfill_enrichment.py --dry-run         # preview
python scripts/backfill_enrichment.py --limit 5         # cap cost
python scripts/backfill_enrichment.py                   # full run
```

For the full numbered Phase 2 verification flow (unit tests, crash recovery, bot `/failures`, etc.) see [docs/phase2-smoke-test.md](docs/phase2-smoke-test.md).

### Phase 3 — SQL + Vector storage (post-cutover)

Every `/capture` POST and every successful enrichment writes to SQL (`data/braintwin.db`) and ChromaDB (`data/chroma/`). Phase 3.5 retired the JSONL writers; SQL is now the sole authoritative store.

**Quick commands:**

```bash
# Run the full test suite from one entry point
python scripts/run_tests.py

# Inspect what's in SQL + Chroma right now (read-only)
python scripts/inspect_storage.py

# Drill into one capture across both stores
python scripts/inspect_storage.py --capture-id <uuid>

# Migrate historical JSONLs into SQL + Chroma (one-shot, frozen tool
# kept for backfilling archived pre-cutover data)
python scripts/migrate_jsonl_to_sql.py --dry-run
python scripts/migrate_jsonl_to_sql.py
python scripts/migrate_jsonl_to_sql.py --verify
```

Full numbered verification flow: see [docs/phase3-smoke-test.md](docs/phase3-smoke-test.md). Design decisions: [docs/phase3-design.md](docs/phase3-design.md). Phase 3.5 cutover decisions and what changed: [docs/phase3.5-cutover.md](docs/phase3.5-cutover.md).

### Phase 4.0 — Vague-recall search

The agent layer that turns BrainTwin into a usable memory prosthetic. One query in, ranked candidate captures out, all from the same browser button you use to capture.

**Backend endpoint:** `POST /recall`

```bash
# First-turn query
curl -s -X POST http://127.0.0.1:8000/recall \
  -H "Content-Type: application/json" \
  -d '{"query": "the kanban article about team size"}' | python -m json.tool
```

Response shape (per `docs/phase4-vague-recall-design.md` S.3):

```json
{
  "answer": "I think you're remembering the Atlassian piece from Apr 18...",
  "confidence": 0.82,
  "results": [
    {
      "capture_id": "abc-123",
      "title": "Atlassian: WIP limits and team size",
      "source_domain": "atlassian.com",
      "captured_at": "2026-04-18T...",
      "client": "chrome",
      "dwell_time_seconds": 187,
      "why_this_matches": "...",
      "snippet": "...",
      "original_url": "https://...",
      "summary": "...",
      "confidence": 0.82
    }
  ],
  "conversation_id": "uuid",
  "no_match": false
}
```

**Multi-turn refinement** — pass `conversation_id` back to layer a filter on the previous candidate pool (no fresh retrieval needed):

```bash
curl -s -X POST http://127.0.0.1:8000/recall \
  -H "Content-Type: application/json" \
  -d '{"query": "the more recent one, not wikipedia", "conversation_id": "<from-previous>"}' | python -m json.tool
```

**Chrome extension Remember tab** — same backend, ergonomic surface. Load the `extension/` folder via `chrome://extensions` → Developer Mode → Load Unpacked, click the BrainTwin toolbar icon, switch to the **Remember** tab, type a query, hit Find.

The Remember tab handles:
- First-turn search and result cards
- Multi-turn refinement (the active conversation is indicated below the search box; **start over** clears it)
- Error states (backend down → "Couldn't reach the backend…"; missing API key → "The recall agent isn't running…"; timeout → suggests shortening the query)
- Confidence color coding (green ≥60%, amber below)

**Algorithm at a glance** (full rationale in [`docs/phase4-vague-recall-design.md`](docs/phase4-vague-recall-design.md)):

```
query ──┬──► Chroma vector search (top-K=20)  ──┐
        │                                        ├──► RRF (k=60) ──► group-by-capture ──► top-6 ──► Sonnet re-rank ──► RecallResponse
        └──► SQLite FTS5 (BM25, top-K=20)     ──┘
```

The hybrid catches both paraphrased queries (vector) and proper-noun queries like "HSR Layout" or "Tamasha" (BM25), which pure vector retrieval misses on the Indian-context content this corpus captures.

### Project Structure

```
BrainTwin/
├── backend/
│   ├── main.py                 # FastAPI app entry point
│   ├── config.py               # Settings and API keys
│   ├── capture/
│   │   ├── __init__.py
│   │   ├── processor.py        # Content processing pipeline
│   │   ├── extractors.py       # Platform-specific text extractors
│   │   ├── vision.py           # Image/meme understanding (Claude Vision)
│   │   ├── og_fetcher.py       # Phase 2.5 — Open Graph / Twitter Card fallback (planned)
│   │   └── video_transcriber.py# Phase 2.5 — yt-dlp + whisper.cpp local transcription (planned)
│   ├── knowledge/              # Phase 2 — built
│   │   ├── __init__.py
│   │   ├── llm_client.py       # Async Anthropic SDK wrapper, typed errors
│   │   ├── prompts.py          # Enrichment system/user prompts + retry reminder
│   │   ├── enrichment.py       # Pure enrich() — schema validation + 1 retry
│   │   └── enrichment_worker.py# Async retry policy + sidecar JSONL persistence + dual-write to SQL
│   ├── storage/                # Phase 3 — SQL + Vector storage (built)
│   │   ├── __init__.py
│   │   ├── db.py               # Async SQLAlchemy engine + session_scope + init_db
│   │   ├── schema.py           # 9 SQLAlchemy Core tables (users, captures, hydrations, enrichments, chunks, topics, entities, chunk_topics, chunk_entities)
│   │   ├── models.py           # Frozen dataclass models (User, Capture, Chunk, …)
│   │   ├── repositories/       # 7 repository classes (one per table family)
│   │   ├── embedder.py         # Lazy sentence-transformers wrapper (all-MiniLM-L6-v2)
│   │   ├── vector_store.py     # ChromaVectorStore — 3 collections (chunks, topics, entities)
│   │   ├── chunking.py         # Paragraph / chapter-aware / token-window chunking
│   │   └── sync.py             # Dual-write seam (sync_capture / sync_hydration / sync_enrichment)
│   ├── agent/                  # Phase 4 — vague-recall agent (built)
│   │   ├── __init__.py
│   │   ├── retrieval.py        # RetrievalService — hybrid Chroma + FTS5 + RRF fusion + per-capture diversification
│   │   ├── recaller.py         # Recaller — Sonnet re-rank, confidence gate, closest-miss, conversation state (M.3)
│   │   └── prompts.py          # System + user prompts for the Sonnet re-rank pass and the closest-miss explainer
│   ├── competition/            # Phase 5 — planned
│   │   ├── __init__.py
│   │   ├── game.py             # Competition logic
│   │   └── scoring.py          # Score tracking
│   └── telegram_bot/
│       ├── __init__.py
│       └── bot.py              # Telegram bot for mobile capture
├── extension/                  # Chrome extension (Manifest V3, vanilla HTML/CSS/JS, no build step)
│   ├── manifest.json           # Version 0.4.0 — Capture + Remember tabs
│   ├── background.js           # Service worker
│   ├── content.js              # Dwell time tracking + extraction (Capture path)
│   ├── popup.html              # Popup UI — tab switcher + Capture view + Remember view
│   ├── popup.js                # Tab switcher + Capture tab logic
│   ├── recall.js               # Phase 4 M.5 — Remember tab: POST /recall, render result cards, conversation_id continuation
│   └── icons/                  # Extension icons
├── data/
│   ├── braintwin.db            # Phase 3 — SQLite database (auto-created, sole capture store post-3.5)
│   ├── chroma/                 # Phase 3 — ChromaDB persistent storage (auto-created)
│   ├── capture_failures.jsonl  # Operational failures log — retained through Phase 3.5 cutover. Phases: capture | enrichment | enrichment_skipped
│   ├── migration_failures.jsonl# Phase 3 — per-row validation failures from migrate_jsonl_to_sql.py
│   ├── captures.jsonl          # ☠ Retired in Phase 3.5 — historical archive only (if present)
│   ├── enrichments.jsonl       # ☠ Retired in Phase 3.5 — historical archive only (if present)
│   ├── hydrations.jsonl        # ☠ Retired in Phase 3.5 — historical archive only (if present)
│   ├── images/                 # Captured images/memes
│   └── models/                 # Phase 2.5 — whisper.cpp models (gitignored, ~250 MB)
├── bin/
│   └── whisper-cli             # Phase 2.5 — local whisper.cpp binary (gitignored)
├── scripts/
│   ├── mock_capture.py         # Phase 1 smoke test — POST a synthetic capture
│   ├── mock_phase2_capture.py  # Phase 2 smoke test — POST + poll for enrichment
│   ├── mock_telegram_capture.py# Phase 1 — exercise the Telegram capture path
│   ├── backfill_enrichment.py  # Phase 2 — idempotent backfill over existing captures
│   ├── retry_failed_enrichments.py  # Phase 2 — on-demand catch-up for unenriched rows
│   ├── replay_failed_urls.py   # Phase 2.5 — re-POST URLs from capture_failures.jsonl
│   ├── migrate_jsonl_to_sql.py # Phase 3 — backfill historical JSONLs into SQL + Chroma
│   ├── inspect_storage.py      # Phase 3 — read-only inspector (SQL row counts + Chroma collections)
│   ├── run_tests.py            # Phase 3 — single-command pytest runner
│   └── setup_whisper.sh        # Phase 2.5 — install whisper.cpp + model
├── tests/
│   ├── conftest.py             # Pins DATABASE_URL to in-memory SQLite for all tests
│   ├── test_capture.py
│   ├── test_enrichment.py
│   ├── test_og_fetcher.py
│   ├── test_replay_failed_urls.py
│   ├── test_storage_schema.py
│   ├── test_storage_repos.py
│   ├── test_embedder.py
│   ├── test_vector_store.py
│   ├── test_chunking.py
│   ├── test_storage_sync.py
│   ├── test_main_wiring.py
│   └── test_migrate_jsonl_to_sql.py
├── docs/
│   ├── architecture.html       # Visual architecture diagram
│   ├── architecture-detailed.md
│   ├── phase1-design.md        # Phase 1 — locked decisions
│   ├── phase1-smoke-test.md
│   ├── phase2-design.md        # Phase 2 — enrichment design
│   ├── phase2-smoke-test.md
│   ├── phase2.5-capture-hydration.md  # Phase 2.5 — IG/FB hydration + hygiene fixes
│   ├── phase3-design.md        # Phase 3 — SQL + Vector storage decisions
│   └── phase3-smoke-test.md    # Phase 3 — run + verify locally
├── .env.example                # Environment variables template
├── .gitignore
├── requirements.txt
└── README.md
```

## Architecture

Open `docs/architecture.html` in a browser for the full visual diagram.

**Phases (build order):**
1. **Phase 1 — Capture** — Chrome extension (dwell-time-gated) + Telegram bot. ✅ built
2. **Phase 2 — Enrichment** — Async Claude Haiku enrichment (summary, entities, key facts, topics). ✅ built
3. **Phase 2.5 — Hydration** — OG metadata + video transcription to fill empty captures before enrichment. ✅ built
4. **Phase 3 — Storage** — SQLAlchemy on SQLite (Postgres-ready) + ChromaDB. Chunking, embeddings (`all-MiniLM-L6-v2`), 9-table schema, multi-tenant from day one. Dual-write window alongside the JSONLs. ✅ built
5. **Phase 3.5 — Cutover** — JSONL writers (captures, enrichments, hydrations) removed; SQL + Chroma are the sole path. `capture_failures.jsonl` survives as an ops log. ✅ built
6. **Phase 4.0 — Agent (Vague Recall)** — Hybrid retrieval (Chroma vector + SQLite FTS5 BM25) with RRF fusion, Sonnet re-rank, multi-turn conversation refinement, Chrome extension Remember tab. Use case B. ✅ built (M.1-M.5)
7. **Phase 4.0.5 — Eval discipline** — Golden query dataset, Recall@1 / Recall@6 / MRR metrics, LLM-as-judge graders, Langfuse self-hosted for production traces. ⏳ next (after dogfooding 4.0)
8. **Phase 4.1 — Synthesis Quizzes (Use case A)** — Generate or answer multi-concept quizzes from the corpus. 🛠️ planned
9. **Phase 4.2 — Clue Inference Game (Use case C)** — Indirect-clue reasoning, per-chunk entity tagging. 🛠️ planned
10. **Phase 5 — Competition** — Third party quizzes you vs the agent, scoring. 🛠️ planned

## Tech Stack

### Backend

| Component             | Technology                                       | Why                                                    |
|-----------------------|--------------------------------------------------|--------------------------------------------------------|
| Language              | Python 3.11+                                     | Async-first, large LLM/ML ecosystem                    |
| HTTP server           | FastAPI + Uvicorn                                | Async, Pydantic-typed bodies, low ceremony             |
| Structured DB         | SQLite (Postgres-ready via SQLAlchemy)           | Single-file, zero setup; Postgres-compatible schema    |
| Vector DB             | ChromaDB                                         | File-backed, HNSW index, cosine similarity             |
| Full-text search      | SQLite FTS5 (BM25)                               | Built into SQLite; pairs with vectors for hybrid recall |
| Embeddings            | `sentence-transformers` (`all-MiniLM-L6-v2`)     | 384 dims, normalized, fast on CPU                      |
| LLM (enrichment)      | Claude Haiku 4.5                                 | Cost-bound (runs on every capture)                     |
| LLM (recall reasoning)| Claude Sonnet 4.6                                | Quality-bound (runs per user query)                    |
| Vision                | Claude Vision API                                | Meme + image-in-page captioning                        |
| Vague-recall retrieval| Hybrid: Chroma + FTS5 with Reciprocal Rank Fusion (k=60) | Catches paraphrase AND proper nouns                   |
| Audio transcription   | `yt-dlp` + `whisper.cpp` (local)                 | No API cost, runs offline                              |
| Mobile capture        | `python-telegram-bot`                            | Forward anything to the bot, ingest into corpus        |
| Testing               | `pytest` + `pytest-asyncio`                      | Same patterns across all backend tests                 |

### Frontend (Chrome extension)

| Component         | Technology                                         | Why                                              |
|-------------------|----------------------------------------------------|--------------------------------------------------|
| Extension platform| Chrome Extension Manifest V3                       | Required for new Chrome Web Store submissions    |
| HTML / CSS / JS   | **Vanilla, no framework, no build step**           | Popup is < 600 LOC; edit-reload-see-change loop  |
| State persistence | `chrome.storage.local`                             | Survives popup close; cross-popup live updates   |
| HTTP client       | `fetch()` + `AbortController`                      | Standard, supports timeouts                      |
| Cross-browser     | Chromium-only today (Chrome, Edge, Brave, Arc, etc.) | `chrome.*` API namespace                       |
| Firefox port      | Not yet — would need `webextension-polyfill` shim  | ~half a day of work when needed                  |
| Safari            | Not yet — needs native macOS wrapper via Xcode     | Weekend of work when needed                      |

Vanilla over React / Svelte / Vue was a deliberate v1 choice. The popup is small, the iteration loop benefits hugely from having no build step, and there's no framework upgrade treadmill to maintain. When/if a desktop dashboard web app gets built (Phase 5+), that surface gets a real framework; the extension popup stays vanilla.

## Estimated Cost

~$5-15/month (Claude API calls only). Everything else runs locally for free.
