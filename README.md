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
│   ├── knowledge/
│   │   ├── __init__.py
│   │   ├── store.py            # ChromaDB + SQLite operations
│   │   ├── enrichment.py       # LLM summarization & tagging
│   │   └── embeddings.py       # Embedding generation
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
│   ├── chroma/                 # ChromaDB storage (auto-created)
│   ├── images/                 # Captured images/memes
│   └── braintwin.db            # SQLite database (auto-created)
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
