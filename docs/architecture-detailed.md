# SabyaBrain — Knowledge Twin Agent

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

#### B. LLM Enrichment

Every piece of captured content goes through an enrichment step:

```python
enrichment_prompt = """
Analyze this content and return:
1. SUMMARY: 2-3 sentence summary of key information
2. ENTITIES: People, companies, places, events mentioned
3. KEY_FACTS: Specific claims, facts, numbers, dates
4. TOPICS: 3-5 topic tags
5. CONNECTIONS: How this relates to broader themes
   (politics, tech, sports, entertainment, etc.)

Content:
{captured_content}
"""
```

**Output example:**
```json
{
  "summary": "Poonam Pandey faked her death in Feb 2024 to raise cervical cancer awareness. The stunt was widely criticized despite its stated purpose.",
  "entities": ["Poonam Pandey", "cervical cancer"],
  "key_facts": [
    "Faked death in February 2024",
    "Purpose was cervical cancer awareness",
    "Received major public backlash",
    "Was active on OnlyFans"
  ],
  "topics": ["bollywood", "controversy", "health-awareness", "social-media"],
  "connections": ["celebrity stunts", "cancer awareness campaigns", "OnlyFans creators"]
}
```

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
    └──── asks same question ► Sabya (answers from memory)
    
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
sabya-brain/
├── backend/
│   ├── main.py                 # FastAPI app
│   ├── capture/
│   │   ├── processor.py        # Content processing pipeline
│   │   ├── extractors.py       # Platform-specific extractors
│   │   └── vision.py           # Image/meme understanding
│   ├── knowledge/
│   │   ├── store.py            # ChromaDB + SQLite operations
│   │   ├── enrichment.py       # LLM tagging/summarization
│   │   └── embeddings.py       # Embedding generation
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
│   ├── sabya.db                # SQLite database
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
