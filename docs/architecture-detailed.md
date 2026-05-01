# BrainTwin — Knowledge Twin Agent

## Overview

A personal knowledge agent that mirrors everything you consume digitally. It reads what you read, sees what you see, and builds a living knowledge base from your daily content consumption. Then you compete against it — a third person asks questions, you both answer, and you see who knows more.

---

## System Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    CAPTURE LAYER                         │
│                                                          │
│   Chrome Extension          Mobile (Telegram Bot)        │
│   (dwell time > 30s)        (share anything to bot)      │
│        │                           │                     │
└────────┼───────────────────────────┼─────────────────────┘
         │                           │
         ▼                           ▼
┌──────────────────────────────────────────────────────────┐
│               PROCESSING PIPELINE                        │
│                  (Python + FastAPI)                       │
│                                                          │
│   ┌─────────────┐  ┌──────────────┐  ┌───────────────┐  │
│   │   Text      │  │   Vision     │  │   YouTube     │  │
│   │  Extractor  │  │   Model      │  │  Transcript   │  │
│   │(readability)│  │(Claude/GPT4V)│  │    (API)      │  │
│   └──────┬──────┘  └──────┬───────┘  └──────┬────────┘  │
│          │                │                  │           │
│          ▼                ▼                  ▼           │
│   ┌─────────────────────────────────────────────────┐   │
│   │           LLM ENRICHMENT                        │   │
│   │   Summarize → Extract Entities → Tag Topics     │   │
│   │   → Identify Key Facts → Find Connections       │   │
│   └─────────────────────┬───────────────────────────┘   │
│                         │                                │
└─────────────────────────┼────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────┐
│                  KNOWLEDGE STORE                         │
│                                                          │
│   ChromaDB (Vector Search)    SQLite (Structured Data)   │
│   - Content embeddings        - Raw content              │
│   - Semantic retrieval        - Metadata (date, source)  │
│                               - Entities & tags          │
│                               - Images (file paths)      │
└─────────────────────────┬────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────┐
│                    AGENT LAYER                           │
│                                                          │
│   Query → Semantic Search (ChromaDB)                     │
│        → Retrieve top-K relevant knowledge chunks        │
│        → Inject into LLM context (Claude API)            │
│        → Agent reasons and answers                       │
│                                                          │
└─────────────────────────┬────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────┐
│               COMPETITION MODE                           │
│                                                          │
│   Third person asks a question                           │
│        → Agent answers from its knowledge base           │
│        → You answer independently                        │
│        → Compare & score                                 │
│                                                          │
│   Game types: Riddle/clue guessing, open Q&A,            │
│   "what do you know about X", current events recall      │
└──────────────────────────────────────────────────────────┘
```

---

## Component Details

### 1. Capture Layer

#### A. Chrome Extension (Laptop)

The extension runs on every page you visit. It tracks how long you spend on each page. Once you cross 30 seconds, it assumes you're actively reading and captures the content.

**What it captures per platform:**

| Platform    | Content Captured                                         |
|-------------|----------------------------------------------------------|
| News/Blogs  | Full article text (via Readability.js), images           |
| YouTube     | Video title, description, transcript (via YT API)        |
| X / Twitter | Tweet text, images, thread context                       |
| Instagram   | Caption text, image, top comments                        |
| Reddit      | Post body, top comments, linked content, images          |
| WhatsApp Web| Message text, shared links, forwarded images             |
| Memes       | Image captured + surrounding text/caption                |
| General     | Page title, URL, main body text, images                  |

**Technical details:**
- Manifest V3 Chrome extension
- Content script tracks dwell time via `visibilitychange` + timer
- On threshold hit: extracts DOM content, captures screenshots of images
- Sends payload to local FastAPI backend (`localhost:8000/capture`)
- Popup UI shows: capture count today, toggle on/off, recent captures

**Payload format:**
```json
{
  "url": "https://example.com/article",
  "title": "Page Title",
  "platform": "reddit",
  "content_type": "article",
  "text": "Extracted main body text...",
  "images": ["base64_encoded_image_1", "base64_encoded_image_2"],
  "timestamp": "2026-04-21T14:30:00Z",
  "dwell_time_seconds": 45,
  "metadata": {
    "author": "username",
    "subreddit": "r/india",
    "likes": 1200
  }
}
```

#### B. Mobile Capture (Telegram Bot)

For phone content, a Telegram bot is the simplest path. You forward or share anything to the bot — links, images, memes, text — and it captures it.

**How it works:**
- Create a Telegram bot via BotFather
- Python backend listens for messages
- When you share a link: bot fetches and processes the page
- When you share an image: bot saves it for vision processing
- When you forward text: bot stores it directly
- Bot confirms capture with a quick reply

**Why Telegram over a native app:**
- No app development needed (huge time savings)
- Works on iOS and Android
- Share sheet integration is built in
- Can forward WhatsApp messages to it easily

---

### 2. Processing Pipeline

A Python FastAPI server that receives captured content, processes it, and stores it.

#### A. Content Extractors

```python
# Tech choices per content type:

# Articles / web pages
newspaper3k or readability-lxml  →  clean article text

# YouTube transcripts
youtube-transcript-api  →  full video transcript with timestamps

# Images / Memes
Claude Vision API  →  describe image, extract text, identify context
                       (e.g., "This is a Bollywood meme about Deepika
                        Padukone referencing the movie Tamasha")

# Social media
Custom parsers per platform  →  extract structured data from DOM
```

##### A.1. Capture Hydration Tiers (Phase 2.5)

> **In progress (2026-04-29):** Fix 1 + Fix 2.A shipped, Fix 2.B cancelled (Bot API limitation), Fix 3 next. See [phase2.5-capture-hydration.md](phase2.5-capture-hydration.md).

Phase 1 + 2 left a real gap: when the Telegram bot forwards a URL (Instagram reel, Facebook share link, news article), the bot can only send `text=""` because Telegram's Bot API delivers just the URL, not the rendered preview. The backend's `extract()` only special-cases YouTube, so everything else lands at the enricher with empty content and gets refused.

Phase 2.5 closes the gap with a tiered hydration model. Hydration runs **inside the enrichment BackgroundTask, before `enrich()` is called** (revised from the original "at capture time" design — keeps the bot's `📥 Captured` ack fast and keeps `captures.jsonl` immutable):

```
1. Use raw_text if non-empty                                    (Chrome extension already extracted)  ✅ Phase 1
2. Else fetch og:title / og:description from URL  (one HTTP GET)                                       ✅ Fix 2.A shipped
3. Else if video URL → yt-dlp + whisper.cpp local transcription                                        ⏭️ Fix 3 next
4. Else mark `phase: "enrichment_skipped"` — nothing left to try                                       ✅ Fix 1 shipped
```

(Originally a 5-tier plan with a "Telegram preview pickup" tier between 1 and 2. That tier was cancelled when we discovered the Bot API doesn't expose preview content — it only exposes `LinkPreviewOptions` settings. The OG fetcher provides the same content via one HTTP GET.)

When tier 2 or 3 fires, the orchestrator writes a sidecar row to `data/hydrations.jsonl` (one row per capture_id, joined to `captures.jsonl` and `enrichments.jsonl` at read time). The `tier` field tags which layer hydrated each capture so future debugging knows where to look. The original `captures.jsonl` row stays as the immutable record of what arrived from the client.

Tier 1 is zero-cost. Tier 2 is one HTTP request via `httpx` + `selectolax` parser. Tier 3 uses local `whisper.cpp` `small.en` (~5s per 30s reel on M-series Mac, $0/month). Tier 4 is a logging-only sentinel separate from real failures (Phase 2.5 hygiene Fix 1).

#### B. LLM Enrichment

> **Built in Phase 2.** See [phase2-design.md](phase2-design.md) for locked decisions and [phase2-smoke-test.md](phase2-smoke-test.md) for verification steps.

Every successfully captured row goes through an async enrichment step (FastAPI BackgroundTasks → `enrichment_worker.enqueue_enrichment`). The schema dropped `connections` from the original sketch — it only means something once Phase 3 has other captures to point at, so it's deferred.

**Locked v1 schema (4 fields):**

```python
{
    "summary":   str,                                    # 1-2 sentences, English
    "entities":  list[{"name": str, "type": str}],      # type ∈ person|org|place|event
    "key_facts": list[str],                              # atomic claims w/ numbers/dates
    "topics":    list[str],                              # 3-5 lowercase hyphenated tags
}
```

Plus wrapper fields written by `wrap_enrichment_record`: `capture_id`, `enriched_at`, `model`, `related_captures` (reserved empty for Phase 3 cross-language linking — Decision I).

**Language fidelity (Decision D):** All output strings are English with Latin script. Names from non-Latin scripts are **transliterated, not translated** (`दीपिका पादुकोण` → `Deepika Padukone`). Cultural keywords, idioms, and untranslatable phrases are preserved verbatim in their Romanized form (`jugaad`, `Schadenfreude`, movie titles like `Tamasha`). Source content can be in any of English / Hindi / Odia / Telugu / German.

**Output example (HSR Layout rent article):**
```json
{
  "summary": "A viral X post by a 28-year-old in Bengaluru complains that 1BHK rents in HSR Layout start at ₹25,000 vs her ₹15,000 budget; HT covered the post and quoted agents saying HSR rents are up ~40% in two years.",
  "entities": [
    {"name": "Bengaluru", "type": "place"},
    {"name": "HSR Layout", "type": "place"},
    {"name": "Hindustan Times", "type": "org"}
  ],
  "key_facts": [
    "1BHK rents in HSR Layout start at ~₹25,000",
    "Renter's stated budget was ₹15,000",
    "HSR rents up ~40% over the past two years"
  ],
  "topics": ["bengaluru", "rent-crisis", "indian-cities", "real-estate", "viral-post"]
}
```

**Persistence model:** sidecar JSONL. `data/captures.jsonl` holds the raw row (Phase 1) keyed by an added `capture_id` UUID4. `data/enrichments.jsonl` holds one enrichment row per `capture_id`. Append-only — no in-place updates, no JSONL locking. Phase 3 collapses both into ChromaDB + SQLite, but the same `capture_id` will continue to be the join key.

**Failure / retry policy (Decision H):** transient errors retry 3× with 0.5s/1s/2s backoff; permanent errors and post-retry malformed JSON skip immediately. All failures append to `data/capture_failures.jsonl` with `phase: "enrichment"` so the existing `/failures` endpoint and bot command surface them. Crash recovery: on FastAPI startup, scan for `capture_id`s in captures.jsonl with no row in enrichments.jsonl and re-queue them.

**Phase 2.5 update — `enrichment_skipped` vs `enrichment`:** "nothing to enrich" cases (`EmptyContentError`, `ContentTooLongError`) are tagged `phase: "enrichment_skipped"` and excluded from the default failure count. Real failures (network, auth, malformed JSON after retry) keep `phase: "enrichment"`. This separation is the hygiene Fix 1 in [phase2.5-capture-hydration.md](phase2.5-capture-hydration.md).

#### C. Embedding Generation

```python
# Use sentence-transformers for local embeddings (free, fast)
from sentence_transformers import SentenceTransformer

model = SentenceTransformer('all-MiniLM-L6-v2')

# Embed the summary + key facts for semantic search
embedding = model.encode(summary + " ".join(key_facts))
```

---

### 3. Knowledge Store

Two databases working together:

#### ChromaDB (Vector Search)
- Stores embeddings of every processed content piece
- Enables semantic search ("who faked their death for awareness?" → finds Poonam Pandey)
- Runs locally, no cloud needed
- Collections organized by topic/platform if needed

#### SQLite (Structured Data)
- Stores raw content, metadata, entities, tags
- Enables exact lookups ("all content from April 2026")
- Enables entity search ("everything about Deepika Padukone")
- Stores image file paths for vision-processed content

**Schema:**
```sql
CREATE TABLE captures (
    id INTEGER PRIMARY KEY,
    url TEXT,
    title TEXT,
    platform TEXT,
    raw_text TEXT,
    summary TEXT,
    entities TEXT,        -- JSON array
    key_facts TEXT,       -- JSON array
    topics TEXT,          -- JSON array
    timestamp DATETIME,
    dwell_time INTEGER,
    image_paths TEXT      -- JSON array of local file paths
);

CREATE TABLE entities (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE,
    entity_type TEXT,     -- person, company, place, event
    mention_count INTEGER,
    first_seen DATETIME,
    last_seen DATETIME
);

CREATE TABLE entity_captures (
    entity_id INTEGER,
    capture_id INTEGER,
    FOREIGN KEY (entity_id) REFERENCES entities(id),
    FOREIGN KEY (capture_id) REFERENCES captures(id)
);
```

---

### 4. Agent Layer

The brain of the system — an LLM powered by your knowledge base.

#### Retrieval (RAG)

When a question comes in:

```python
def answer_question(question: str) -> str:
    # 1. Semantic search — find relevant knowledge
    results = chromadb_collection.query(
        query_texts=[question],
        n_results=20
    )

    # 2. Entity search — find any named entities in the question
    entities = extract_entities(question)
    entity_results = sqlite_db.search_by_entities(entities)

    # 3. Combine and deduplicate
    context = merge_results(results, entity_results)

    # 4. Build prompt with knowledge context
    prompt = f"""
    You are a knowledge agent. You have been consuming the same
    content as your human counterpart — news, social media, memes,
    videos, articles — everything listed below.

    Answer the question using ONLY the knowledge provided below.
    If you don't have enough information, say so.
    Make connections between different pieces of knowledge.
    Think step by step.

    YOUR KNOWLEDGE BASE:
    {format_context(context)}

    QUESTION: {question}
    """

    # 5. Call Claude API
    response = anthropic.messages.create(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": prompt}]
    )

    return response.content[0].text
```

#### Why Claude over a local LLM:
- Much better at reasoning and making connections (critical for riddle games)
- Better at understanding cultural context (Bollywood, Indian politics, memes)
- Vision capabilities for image-based questions
- Can start with Claude, switch to local later for cost savings

---

### 5. Competition Mode

This is NOT auto-generated quizzes. This is real, organic competition.

#### How it works:

```
Third Person (Quizmaster)
    │
    ├──── asks question ────► Agent (answers from knowledge base)
    │
    └──── asks same question ► You (answer from memory)
    
    Compare answers → Score
```

#### Interface options:

**Option A: Telegram Group**
- Create a group with: You, the Bot, a friend
- Friend asks a question
- Bot responds in the group
- You respond in the group
- Compare side by side

**Option B: Web Interface**
- Simple chat page with two columns
- Question goes to both
- Agent's answer revealed after you submit yours
- Score tracker on the side

**Option C: CLI (simplest MVP)**
- Terminal interface
- Question input → Agent answers → You answer → Compare
- Score tracked in a text file

#### Scoring:
- Correct answer: +1
- Partially correct: +0.5 (judged by quizmaster or LLM)
- Wrong answer: 0
- Running total: You vs Agent, tracked over time

---

## Tech Stack Summary

| Component         | Technology                          | Cost      |
|-------------------|-------------------------------------|-----------|
| Chrome Extension  | JavaScript, Manifest V3             | Free      |
| Mobile Capture    | Telegram Bot (python-telegram-bot)  | Free      |
| Backend API       | Python, FastAPI, Uvicorn            | Free      |
| Text Extraction   | readability-lxml, newspaper3k       | Free      |
| YouTube           | youtube-transcript-api              | Free      |
| Vision (memes)    | Claude Vision API                   | ~$0.01/img|
| LLM Enrichment    | Claude API (Haiku for tagging)      | ~$0.001/capture |
| Embeddings        | sentence-transformers (local)       | Free      |
| Vector DB         | ChromaDB (local)                    | Free      |
| Structured DB     | SQLite                              | Free      |
| Agent LLM         | Claude API (Sonnet for reasoning)   | ~$0.01/query |
| Hosting           | Your laptop                         | Free      |

**Estimated monthly cost:** $5–15 depending on usage (mostly Claude API calls)

---

## Build Order (Suggested Phases)

### Phase 1: Foundation (Week 1-2)
- [ ] Set up Python project structure
- [ ] Build FastAPI backend with capture endpoint
- [ ] Set up ChromaDB + SQLite
- [ ] Build processing pipeline (text extraction + LLM enrichment)
- [ ] Test with manual content input (paste URLs)

### Phase 2: Chrome Extension (Week 2-3)
- [ ] Build Manifest V3 extension
- [ ] Implement dwell time tracking
- [ ] Content extraction per platform (Readability.js + custom)
- [ ] Image capture for memes
- [ ] Connect to local backend API

### Phase 3: Mobile + Vision (Week 3-4)
- [ ] Set up Telegram bot
- [ ] Image processing with Claude Vision
- [ ] YouTube transcript integration
- [ ] Test cross-platform capture (laptop + phone)

### Phase 4: Agent (Week 4-5)
- [ ] Build RAG retrieval pipeline
- [ ] Design agent system prompt
- [ ] Test with knowledge-based questions
- [ ] Iterate on retrieval quality (tune chunk sizes, top-K, etc.)

### Phase 5: Competition Mode (Week 5-6)
- [ ] Build competition interface (start with CLI or Telegram)
- [ ] Implement scoring system
- [ ] Test with friends
- [ ] Iterate on agent accuracy

---

## Folder Structure

```
BrainTwin/
├── backend/
│   ├── main.py                 # FastAPI app
│   ├── capture/
│   │   ├── processor.py        # Content processing pipeline
│   │   ├── extractors.py       # Platform-specific extractors
│   │   └── vision.py           # Image/meme understanding
│   ├── knowledge/              # Built in Phase 2
│   │   ├── llm_client.py       # Model-agnostic async wrapper (Anthropic SDK)
│   │   ├── prompts.py          # System / user prompts + retry reminder
│   │   ├── enrichment.py       # Pure enrich(processed) → 4-field dict
│   │   ├── enrichment_worker.py# Async retry + sidecar JSONL persistence
│   │   ├── store.py            # Phase 3 — ChromaDB + SQLite operations
│   │   └── embeddings.py       # Phase 3 — embedding generation
│   ├── agent/
│   │   ├── retrieval.py        # RAG retrieval logic
│   │   ├── reasoning.py        # Agent LLM calls
│   │   └── prompts.py          # System prompts
│   ├── competition/
│   │   ├── game.py             # Competition logic
│   │   └── scoring.py          # Score tracking
│   ├── telegram_bot/
│   │   └── bot.py              # Telegram bot for mobile capture
│   ├── config.py               # API keys, settings
│   └── requirements.txt
├── extension/
│   ├── manifest.json           # Chrome extension manifest v3
│   ├── background.js           # Service worker
│   ├── content.js              # Content script (dwell time + extraction)
│   ├── popup.html              # Extension popup UI
│   ├── popup.js
│   └── readability.js          # Mozilla Readability library
├── data/
│   ├── chroma/                 # ChromaDB storage
│   ├── braintwin.db            # SQLite database
│   └── images/                 # Captured images/memes
├── tests/
│   ├── test_capture.py
│   ├── test_enrichment.py
│   └── test_agent.py
└── README.md
```

---

## Key Design Decisions & Trade-offs

### Why dwell time (30s) instead of capturing everything?
Capturing every page load would flood the system with noise — accidental clicks, search results pages, loading screens. 30 seconds means you're actually reading/engaging. Can always adjust the threshold.

### Why Telegram for mobile instead of a native app?
Building a native iOS/Android app is weeks of work. Telegram gives you a share sheet, notifications, image support, and group chat (for competition mode) out of the box. The bot takes a day to build.

### Why ChromaDB + SQLite instead of just one database?
ChromaDB is great for "find me content related to this concept" (semantic search). SQLite is great for "show me everything I read about Deepika Padukone last week" (structured queries). You need both for a good agent.

### Why Claude API instead of a local LLM?
The riddle/guessing game requires strong reasoning and cultural knowledge. Local LLMs (Llama, Mistral) are decent at retrieval but much weaker at making creative connections — the kind of lateral thinking needed to go from "my favorite bird migrated from India" to "Kingfisher → Vijay Mallya." Claude is significantly better at this. Cost is ~$5-15/month which is reasonable.

### Why not fine-tune a model on your data?
Fine-tuning makes the model memorize patterns, not reason about new connections. RAG (retrieval + reasoning) is better for this use case because your knowledge base changes daily. Fine-tuning would need retraining every time new content comes in.

---

## Privacy & Security Notes

- All data stays on your local machine (no cloud storage)
- Chrome extension only activates on pages YOU spend time on
- API calls to Claude send content snippets for processing — consider what you're comfortable sending
- SQLite database and ChromaDB are local files — encrypt if needed
- Telegram bot messages go through Telegram's servers — be aware of this for sensitive content
- Add an extension toggle to pause capture on sensitive sites (banking, email, etc.)
