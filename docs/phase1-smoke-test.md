# Phase 1 Smoke Test — Capture Layer End-to-End

Phase 1 has two capture clients. Each has its own end-to-end verification flow:

- **[Part A — Chrome extension](#part-a--chrome-extension)** — webpage (≥30s) → `extension/content.js` → POST `/capture` → `data/captures.jsonl`
- **[Part B — Telegram bot](#part-b--telegram-bot)** — phone forward → `backend.telegram_bot.bot` → POST `/capture` → `data/captures.jsonl`

Each part runs in numbered passes so when something breaks you know exactly where.

---

## Part A — Chrome extension

Goal: prove **Chrome page (≥30s)  →  extension/content.js  →  POST /capture  →  data/captures.jsonl**.

---

## Pass 0 — Confirm the pieces exist

```bash
cd ~/Desktop/LLM/BrainTwin

# Backend code present?
ls backend/main.py backend/capture/processor.py

# Extension assets present (icons were the blocker — should now exist)?
ls extension/manifest.json extension/icons/icon{16,48,128}.png

# Smoke test script present?
ls scripts/mock_capture.py
```

All five `ls` calls should succeed. If any fail, stop here.

---

## Pass 1 — Backend in isolation (no browser)

This proves the server + processing pipeline + JSONL append work, *before* you touch Chrome.

**Terminal A — start the backend:**

```bash
cd ~/Desktop/LLM/BrainTwin
source venv/bin/activate
uvicorn backend.main:app --reload --port 8000
```

You should see Uvicorn log lines ending with `Application startup complete.`

**Terminal B — fire a mock capture:**

```bash
cd ~/Desktop/LLM/BrainTwin
python scripts/mock_capture.py
```

Expected output (last lines):

```
[4/4] GET /stats (after)
  ← {'total_captures': 1, 'total_entities': 0, 'platforms': {'general': 1}, ...}

  total_captures: 0 → 1  (delta +1)
  last row in data/captures.jsonl: url='https://en.wikipedia.org/wiki/Knowledge_graph' text_source='extension'

✓ End-to-end capture path works.
```

If this works, the server side is good. If it fails, the script tells you which step (health / stats / capture) broke and the backend log in Terminal A will have the traceback.

You can also peek at the JSONL directly:

```bash
tail -1 data/captures.jsonl | python -m json.tool
```

---

## Pass 2 — Load the Chrome extension

1. In Chrome, open `chrome://extensions`.
2. Toggle **Developer mode** on (top-right).
3. Click **Load unpacked**.
4. Select the folder: `~/Desktop/LLM/BrainTwin/extension`.
5. The "BrainTwin Capture" card should appear with the gradient twin-circles icon. No errors in red.
6. Pin it to the toolbar (puzzle icon → pin).

If Chrome shows a manifest error, the most common causes are: missing icon PNGs (Pass 0 should have caught this) or bad JSON in `manifest.json`.

---

## Pass 3 — Real capture from a real page

Backend must be running (Terminal A from Pass 1).

1. Open a long-form article in a new tab — e.g. a Wikipedia page or a news article. Avoid login pages, banking sites, and the skip-listed domains in `content.js`.
2. **Stay on the tab and scroll/read for ≥30 seconds.** Don't switch tabs (the dwell timer pauses on `visibilitychange`).
3. After 30s, the extension fires `POST /capture` automatically.

**How to confirm it captured:**

| Check | What you should see |
|---|---|
| Extension toolbar badge | A small number (the daily capture count) increments by 1. |
| Backend log (Terminal A) | A line like `INFO ... Processing capture: platform=general url=...` followed by Uvicorn's `200 OK` access line. |
| Page DevTools console (F12) | `[BrainTwin] Content captured: <page title>` (only visible from the page's own console, not the extension popup). |
| `data/captures.jsonl` | `tail -1 data/captures.jsonl` should now show that page's URL and title. |
| Stats endpoint | `curl http://127.0.0.1:8000/stats` shows `total_captures` increased and the platform breakdown updated. |

---

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Badge never increments | Tab lost focus before 30s elapsed | Stay on the tab; or temporarily lower `DWELL_THRESHOLD_MS` in `content.js` for testing |
| Console shows `Backend not running, skipping capture.` | Uvicorn isn't running, or wrong port | Start uvicorn on port 8000 |
| 422 Unprocessable Entity in backend log | Payload shape mismatch | The mock script uses the canonical shape — diff against `content.js` |
| Capture row is missing `image_descriptions` content | `ANTHROPIC_API_KEY` not set in `.env` → vision skipped | Expected for now; Phase 2 enrichment will add real summaries |
| Chrome refuses to load extension ("Could not load icon") | Icon PNGs missing | Re-run icon generation; check `extension/icons/` |

---

## What this does *not* test (yet)

These are out of scope for Phase 1 verification per the architecture diagram — they belong to later phases:

- LLM enrichment (summary / entities / topics) — Phase 2
- ChromaDB + SQLite storage — Phase 3
- Agent Q&A — Phase 4
- Competition scoring — Phase 5

The current Phase 1 success criterion is just: **a real Chrome page-view becomes a row in `data/captures.jsonl`.**

---

## Part B — Telegram bot

Goal: prove **phone (forward / share)  →  Telegram cloud  →  bot polling on laptop  →  POST `/capture`  →  `data/captures.jsonl`**.

### Pass 0 — One-time bot setup

These steps happen once per machine — you only repeat them if you switch bots.

1. **Create the bot.** In the Telegram app on your phone, open a chat with `@BotFather`. Send `/newbot`, give it a display name (e.g. *"Sabya BrainTwin"*) and a unique username ending in `bot` (e.g. `sabya_braintwin_bot`). BotFather will reply with a token like `123456789:ABCdef...`.
2. **Save the token.** Open `~/Desktop/LLM/BrainTwin/.env` and set:
   ```
   TELEGRAM_BOT_TOKEN=123456789:ABCdef...
   ALLOWED_TELEGRAM_USER_IDS=
   ```
   Leave the allowlist empty for now.
3. **Install the deps if you haven't already.**
   ```bash
   cd ~/Desktop/LLM/BrainTwin
   source venv/bin/activate
   pip install -r requirements.txt
   ```
4. **Get your Telegram user ID.** Start the bot once with the empty allowlist — it'll let `/start` through and reply with your ID.
   ```bash
   python -m backend.telegram_bot.bot
   ```
   On your phone, search Telegram for the bot's username, open the chat, tap **Start**. The bot replies: *"Your Telegram user ID is `12345678`"*. Copy that.
5. **Stop the bot** (`Ctrl-C`), put the ID in `.env`:
   ```
   ALLOWED_TELEGRAM_USER_IDS=12345678
   ```
   Restart the bot. The startup log should now print `Allowlist: [12345678]`.

### Pass 1 — Bot in isolation (offline simulator, no real Telegram)

Verifies the payload shapes the bot will produce work end-to-end, without spamming the real bot. Backend must be running.

```bash
# Terminal A:
uvicorn backend.main:app --reload --port 8000

# Terminal B:
python scripts/mock_telegram_capture.py
```

Expected output ends with:

```
[stats after]  total_captures = N+4  (delta +4)

✓ All Telegram-shaped scenarios round-tripped through /capture.
```

This sends four scenarios — text-with-URL, single image, 3-image album, and a forwarded URL — and checks each one made it into `data/captures.jsonl`.

### Pass 2 — Real bot replies to /start

Backend running (Terminal A). In Terminal B:

```bash
python -m backend.telegram_bot.bot
```

Startup log should show: `Bot online: @your_bot_name (id=...)` and `Allowlist: [12345678]`. From your phone, send `/start` — the bot should reply *"BrainTwin is active for you."* Send `/help` — should list commands.

### Pass 3 — Forward a real URL from your phone

Bot + backend both running. On your phone:

1. Open Safari / Chrome / Times of India / any news app, find an article.
2. Tap **Share** → pick Telegram → pick your BrainTwin bot's chat → send.
3. Within ~1s the bot replies *"📥 Captured"*.

Verify on your laptop:

| Check | What you should see |
|---|---|
| Bot terminal | `200 OK` from POST `/capture` |
| Backend terminal | `Processing capture: platform=... url=...` followed by `200 OK` |
| `data/captures.jsonl` | `tail -1 data/captures.jsonl \| python -m json.tool` shows the article URL, title, and `metadata.source = "telegram"` |
| `/stats` | `curl http://127.0.0.1:8000/stats` shows `total_captures` increased by 1 |

### Pass 4 — Forward an image (single)

On your phone, long-press an image in WhatsApp / Photos / Instagram → Share → Telegram → BrainTwin bot.

| Check | What you should see |
|---|---|
| Bot reply | `📥 Captured` within 1s |
| `data/captures.jsonl` | New row with `platform: "telegram_image"`, `images: ["data:image/jpeg;base64,..."]`, `url: "tg://message/..."` |
| If `ANTHROPIC_API_KEY` set | `image_descriptions` array populated by vision |

### Pass 5 — Send an album (multiple photos)

Pick 3–5 photos in your phone gallery, share them all to the bot in one go.

| Check | What you should see |
|---|---|
| Bot reply | One `📥 Captured` (not five) |
| Backend log | One `Processing capture` (not five) |
| `data/captures.jsonl` | One row, with `images: [<3-5 b64 strings>]` and `metadata.image_count` matching |

### Pass 6 — /pause and /failures behavior

```
/pause            → bot replies "⏸ Paused"
forward something → no ack, no /capture POST
/resume           → bot replies "▶️ Resumed"
forward something → ack + capture as normal
/failures         → "✅ No recent failures." (or shows the most recent ones)
```

### Pass 7 — Catch-up after laptop sleep

1. Stop the bot (`Ctrl-C`) and the backend.
2. From your phone, forward 2–3 things to the bot. They sit in Telegram's queue.
3. Wait a few minutes, then start backend + bot again.
4. As the bot drains the backlog, each message gets `📥 Captured` in order, and `data/captures.jsonl` grows by 2–3 rows.

(For the 12h-gap "caught up on N messages" notice, you'd need to actually leave the bot off for 12+ hours. Not worth simulating in a smoke test — but the code path is exercised by `_maybe_catchup_notice` in `handlers.py`.)

### Common Telegram failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Bot startup logs `Allowlist: EMPTY` and ignores everything | `ALLOWED_TELEGRAM_USER_IDS` not set in `.env` | Send `/start` to the bot and use the ID from the reply |
| `Conflict: terminated by other getUpdates request` | Another instance of this bot is polling somewhere | Kill the other process, or rotate the token via BotFather |
| Bot connects but `/start` reply never arrives | Wrong token, or token belongs to a different bot | Re-check `TELEGRAM_BOT_TOKEN` matches the bot whose chat you opened |
| Forward → no `📥 Captured` reply | You typed plain text without a URL (Decision 3 → ignored), or the bot is paused | Check with `/help` — only URLs / images are captured |
| `⚠️ Couldn't process: backend HTTP 500` | Backend exception | Check the backend terminal traceback; row will also appear in `data/capture_failures.jsonl` |
| `⚠️ Couldn't process: backend unreachable` | Backend not running | Start `uvicorn backend.main:app` |
