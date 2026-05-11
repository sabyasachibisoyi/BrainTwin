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

Handy commands for verifying that a capture made it through the full Phase 1 + Phase 2 pipeline (raw text → Haiku enrichment → sidecar JSONL). Run these from the repo root with the backend running.

**1. High-level counts:**

```bash
curl -s http://127.0.0.1:8000/stats | python -m json.tool
```

You want `total_captures` and `enrichments.total` to both go up by 1 after each capture, and `enrichments.pending` to settle to `0` once the background worker finishes (usually within a few seconds).

**2. Pull the latest capture row + grab its `capture_id`:**

```bash
tail -1 data/captures.jsonl | python -m json.tool | head -20
CID=$(tail -1 data/captures.jsonl | python -c "import json,sys; print(json.loads(sys.stdin.read())['capture_id'])")
echo "capture_id = $CID"
```

**3. Pull the matching enrichment row:**

```bash
grep "$CID" data/enrichments.jsonl | python -m json.tool
```

The enrichment block must have four fields: `summary` (1-2 sentences), `entities` (list of `{name, type}`), `key_facts` (atomic claims), `topics` (3-5 lowercase hyphenated tags). Wrapper keys: `model`, `enriched_at`, `related_captures` (reserved empty for Phase 3).

**4. Confirm no enrichment failure was logged for that capture:**

```bash
grep "$CID" data/capture_failures.jsonl 2>/dev/null || echo "(no failures for $CID — good)"
```

**5. Inspect failures by phase (capture vs enrichment):**

```bash
curl -s 'http://127.0.0.1:8000/failures?phase=enrichment&limit=5' | python -m json.tool
curl -s 'http://127.0.0.1:8000/failures?phase=capture&limit=5'    | python -m json.tool
```

**6. If `enrichments.pending` won't go to 0**, the worker either failed or is still running. Check the uvicorn log for an `enrich[<first-8-of-CID>]` line, then manually retry:

```bash
python scripts/retry_failed_enrichments.py --dry-run    # show what's missing
python scripts/retry_failed_enrichments.py              # actually retry
```

**7. Backfill enrichment over older captures (idempotent — skips test rows and already-enriched IDs):**

```bash
python scripts/backfill_enrichment.py --dry-run         # preview
python scripts/backfill_enrichment.py --limit 5         # cap cost
python scripts/backfill_enrichment.py                   # full run
```

For the full numbered Phase 2 verification flow (unit tests, crash recovery, bot `/failures`, etc.) see [docs/phase2-smoke-test.md](docs/phase2-smoke-test.md).

### Phase 3 — SQL + Vector storage (dual-write mode)

Every `/capture` POST and every successful enrichment now mirrors into SQL (`data/braintwin.db`) and ChromaDB (`data/chroma/`) **alongside** the JSONL writers. Gated by `STORAGE_DUAL_WRITE=true` (default).

**Quick commands:**

```bash
# Run the full test suite from one entry point
python scripts/run_tests.py

# Inspect what's in SQL + Chroma right now (read-only)
python scripts/inspect_storage.py

# Drill into one capture across both stores
python scripts/inspect_storage.py --capture-id <uuid>

# Migrate historical JSONLs into SQL + Chroma
python scripts/migrate_jsonl_to_sql.py --dry-run
python scripts/migrate_jsonl_to_sql.py
python scripts/migrate_jsonl_to_sql.py --verify
```

Full numbered verification flow: see [docs/phase3-smoke-test.md](docs/phase3-smoke-test.md). Design decisions and the build-step log: [docs/phase3-design.md](docs/phase3-design.md).

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
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── retrieval.py        # RAG retrieval logic
│   │   ├── reasoning.py        # Agent LLM reasoning
│   │   └── prompts.py          # System prompts
│   ├── competition/
│   │   ├── __init__.py
│   │   ├── game.py             # Competition logic
│   │   └── scoring.py          # Score tracking
│   └── telegram_bot/
│       ├── __init__.py
│       └── bot.py              # Telegram bot for mobile capture
├── extension/
│   ├── manifest.json           # Chrome extension config
│   ├── background.js           # Service worker
│   ├── content.js              # Dwell time tracking + extraction
│   ├── popup.html              # Extension popup UI
│   ├── popup.js                # Popup logic
│   └── icons/                  # Extension icons
├── data/
│   ├── captures.jsonl          # Phase 1 — raw captures (one per line, with capture_id)
│   ├── enrichments.jsonl       # Phase 2 — Haiku enrichment sidecar (joined by capture_id)
│   ├── hydrations.jsonl        # Phase 2.5 — OG metadata + transcription sidecar
│   ├── capture_failures.jsonl  # Failures tagged with phase: capture | enrichment | enrichment_skipped (Phase 2.5)
│   ├── migration_failures.jsonl# Phase 3 — per-row validation failures from migrate_jsonl_to_sql.py
│   ├── chroma/                 # Phase 3 — ChromaDB persistent storage (auto-created)
│   ├── braintwin.db            # Phase 3 — SQLite database (auto-created)
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
1. **Phase 1 — Capture** — Chrome extension (dwell-time-gated) + Telegram bot. Writes raw JSONL with `capture_id`. ✅ built
2. **Phase 2 — Enrichment** — Async Claude Haiku enrichment (summary, entities, key facts, topics). Sidecar JSONL. ✅ built
3. **Phase 2.5 — Hydration** — OG metadata + video transcription to fill empty captures before enrichment. ✅ built
4. **Phase 3 — Storage** — SQLAlchemy on SQLite (Postgres-ready) + ChromaDB. Chunking, embeddings (`all-MiniLM-L6-v2`), 9-table schema, multi-tenant from day one. Currently in **dual-write mode** alongside the JSONLs. ✅ built
5. **Phase 3.5 — Cutover** — Remove JSONL writers; SQL + Chroma become sole path. ⏳ next
6. **Phase 4 — Agent** — Synthesis quizzes (use case A), vague-recall search (B), indirect-clue inference (C). 🛠️ planned
7. **Phase 5 — Competition** — Third party quizzes you vs the agent, scoring. 🛠️ planned

## Tech Stack

| Component         | Technology                     |
|-------------------|--------------------------------|
| Language          | Python 3.11+                   |
| IDE               | Cursor                         |
| Backend           | FastAPI + Uvicorn              |
| Vector DB         | ChromaDB                       |
| Structured DB     | SQLite                         |
| LLM               | Claude API (Haiku + Sonnet)    |
| Embeddings        | sentence-transformers          |
| Vision            | Claude Vision API              |
| Browser Extension | JavaScript, Manifest V3        |
| Mobile Capture    | python-telegram-bot            |
| Testing           | pytest                         |

## Estimated Cost

~$5-15/month (Claude API calls only). Everything else runs locally for free.
