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
│   │   └── vision.py           # Image/meme understanding (Claude Vision)
│   ├── knowledge/              # Phase 2 — built
│   │   ├── __init__.py
│   │   ├── llm_client.py       # Async Anthropic SDK wrapper, typed errors
│   │   ├── prompts.py          # Enrichment system/user prompts + retry reminder
│   │   ├── enrichment.py       # Pure enrich() — schema validation + 1 retry
│   │   ├── enrichment_worker.py# Async retry policy + sidecar JSONL persistence
│   │   ├── store.py            # Phase 3 — ChromaDB + SQLite (not built)
│   │   └── embeddings.py       # Phase 3 — Embedding generation (not built)
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
│   ├── capture_failures.jsonl  # Failures tagged with phase: capture | enrichment
│   ├── chroma/                 # Phase 3 — ChromaDB storage (auto-created)
│   ├── images/                 # Captured images/memes
│   └── braintwin.db            # Phase 3 — SQLite database (auto-created)
├── scripts/
│   ├── mock_capture.py         # Phase 1 smoke test — POST a synthetic capture
│   ├── mock_phase2_capture.py  # Phase 2 smoke test — POST + poll for enrichment
│   ├── mock_telegram_capture.py# Phase 1 — exercise the Telegram capture path
│   ├── backfill_enrichment.py  # Idempotent backfill over existing captures
│   └── retry_failed_enrichments.py  # On-demand catch-up for unenriched rows
├── tests/
│   ├── test_capture.py
│   ├── test_enrichment.py
│   └── test_agent.py
├── docs/
│   └── architecture.html       # Visual architecture diagram
├── .env.example                # Environment variables template
├── .gitignore
├── requirements.txt
└── README.md
```

## Architecture

Open `docs/architecture.html` in a browser for the full visual diagram.

**Five layers:**
1. **Capture** — Chrome extension (laptop) + Telegram bot (phone)
2. **Processing** — Text extraction, vision AI for memes, LLM enrichment
3. **Storage** — ChromaDB (semantic search) + SQLite (structured queries)
4. **Agent** — Claude Sonnet with RAG retrieval from your knowledge base
5. **Competition** — You vs the agent, scored by a third party

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
