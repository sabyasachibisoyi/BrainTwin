# Phase 1 Design — Capture Layer

> **Status as of 2026-04-27 — PHASE 1 COMPLETE ✅**
>
> Both capture clients are built, deployed, and verified end-to-end.
>
> | Client | Status | Verified by |
> |---|---|---|
> | Chrome extension | ✅ Live | Real page-view → JSONL row, badge increments, pause/resume work |
> | Telegram bot | ✅ Live | Real phone-forward (chat_id 5052520402, msg_id 3) → JSONL row with `metadata.source: "telegram"` |
>
> Phase 1's success criterion — *a real piece of content you consume becomes a row in `data/captures.jsonl`* — is met from both the laptop and the phone.
>
> **Pick up next session at [Part 6 — Where to start next session](#part-6--where-to-start-next-session).**

The HTML architecture diagram (`docs/architecture.html`) defines Phase 1 as the **Capture Layer**: the things that watch what you consume and feed it into the FastAPI backend. Two capture clients, both POSTing to the same `POST /capture` endpoint:

```
Laptop  →  Chrome Extension  ──┐
                                ├──►  POST /capture  →  data/captures.jsonl
Mobile  →  Telegram Bot      ──┘
```

Phase 1's success criterion is narrow: **a real piece of content you consume becomes a row in `data/captures.jsonl`**. Enrichment (summary, entities, tags), embeddings, ChromaDB, SQLite, and the agent all live in later phases.

---

## Part 1 — Chrome Extension (built)

### What it does

The extension runs as a content script on every page (subject to a skip list). It starts a 30-second dwell timer when the tab becomes visible. If you stay on the tab for the full 30 seconds without switching away, the script extracts the page's main text plus any reasonably-sized images, packages it into the same JSON shape the backend expects, and POSTs it to `http://127.0.0.1:8000/capture`. The backend processes it (text normalization, optional vision on images, YouTube transcript fetch when applicable) and appends the result to `data/captures.jsonl`.

The extension also has a popup with a live capture count and a Pause/Resume toggle. Both are backed by `chrome.storage.local`, so they survive the MV3 service worker dying after ~30 seconds idle. Pausing cancels in-flight dwell timers on already-open tabs and prevents new tabs from starting capture.

### File map

| File | Role |
|---|---|
| `extension/manifest.json` | MV3 manifest — declares `activeTab` + `storage` permissions and `<all_urls>` host access |
| `extension/content.js` | Runs on every page. Owns the dwell timer, content extraction, POST to backend. Reads `enabled` from storage; cancels in-flight timers when toggled off. |
| `extension/background.js` | Service worker. Owns the badge, atomically increments the daily capture count when `CAPTURE_SUCCESS` arrives from a content script. |
| `extension/popup.html` / `popup.js` | The toolbar popup. Reads count + enabled state from storage, lets user toggle, live-updates via `storage.onChanged`. |
| `extension/icons/icon{16,48,128}.png` | Brand-gradient twin-circles glyph — required for Chrome to load the unpacked extension. |

### Storage schema (`chrome.storage.local`)

Single source of truth. All three scripts read/write here; no in-memory state outlives the service worker.

```json
{
  "enabled": true,
  "captures": { "count": 7, "date": "2026-04-24" }
}
```

When the date in `captures` doesn't match today's date, the count is reset to zero on the next read. This avoids depending on `chrome.alarms` (which would require an extra permission and isn't reliable in MV3 anyway).

### Capture payload (the contract with the backend)

This is the JSON the extension POSTs. The Telegram bot will produce the same shape so the backend doesn't need to care which client sent the capture.

```json
{
  "url": "https://example.com/article",
  "title": "Page Title",
  "platform": "general",
  "content_type": "article",
  "text": "Extracted main body text...",
  "images": ["https://...jpg", "data:image/png;base64,..."],
  "timestamp": "2026-04-24T20:30:00Z",
  "dwell_time_seconds": 47,
  "metadata": {
    "description": "OG description if present",
    "author": "byline if present"
  }
}
```

Platform values currently emitted by the extension: `youtube`, `twitter`, `instagram`, `reddit`, `whatsapp`, `linkedin`, `facebook`, `general`. The backend doesn't validate this — it's a free-text tag.

### Skip list

`extension/content.js` refuses to run on banking, auth, and email domains: Gmail, Citi, Chase, BofA, accounts.google.com, anything starting with `login.`/`signin.`/`auth.`. This is hardcoded today; if it gets annoying we'll move it into `chrome.storage` and expose a UI.

### How to verify (live)

See `docs/phase1-smoke-test.md` — three passes (file presence → backend in isolation → real Chrome capture). The mock script `scripts/mock_capture.py` POSTs the canonical payload and reads `/stats` back; useful for backend-only debugging.

### Known limitations (intentional, defer to later phases)

- **No enrichment.** Captures land in JSONL with raw text and image descriptions only — no summary, entities, or topic tags. That's Phase 2 (per the HTML diagram) or whenever we wire `backend/knowledge/`.
- **No semantic storage.** ChromaDB and SQLite directories exist but the code paths to write them don't. Phase 3 in the HTML diagram.
- **No agent.** `/ask` is a stub. Phase 4.
- **Single user.** No multi-user separation in the JSONL. Fine for personal use.
- **No retries on POST failure.** If the backend is down when the dwell timer fires, the capture is lost. Could add a small in-memory retry queue but probably not worth it pre-Phase-2.

---

## Part 2 — Telegram Bot (proposed, not built)

### Why Telegram, why now

The Chrome extension catches nothing on your phone. You consume substantial content on mobile: WhatsApp forwards (Indian news screenshots, memes, voice notes), Instagram reels and posts, YouTube videos in the YouTube app, X threads, news in Times of India / Safari, occasional PDFs and podcast links. Building a native iOS/Android app is weeks of work. Telegram gives us the share sheet, push notifications, image and album support, and group-chat support (needed for Phase 5 competition mode) for free.

The user-facing flow on mobile: tap Share in any app → pick Telegram → pick the BrainTwin bot's chat → send. Three taps. Within Telegram itself, long-press a message → Forward → bot. Two taps.

### Mental model

```
You forward / share something to the bot
    │
    ▼
Bot acks within ~1s:  "📥 Got it"
    │
    ▼
Bot detects content type and processes:

   URL in text     → fetch page (or YouTube transcript) → POST /capture
   single image    → save bytes → POST /capture (vision runs server-side)
   media group     → wait ~1.5s for siblings → batch as one capture
   plain text      → POST /capture as content_type="thought"  (decision #3)
   forwarded msg   → preserve forward_origin in metadata
   voice note      → ??? (decision #2)
    │
    ▼
Bot replies again when processing finishes:

   "📚 [title]
    [1-line summary from enrichment]"

   or, if it failed:

   "⚠️  Couldn't process: <reason>"
```

### Why a separate process (not embedded in FastAPI)

Two reasonable architectures:

The **embedded** option starts the bot as an async background task inside `backend/main.py`. One `uvicorn` command runs everything. Downside: a bot crash takes the backend down with it; harder to reason about what's serving what.

The **separate-process** option runs the bot as `python -m backend.telegram_bot.bot` in its own terminal. The bot POSTs captures to `http://127.0.0.1:8000/capture` exactly like the Chrome extension does. This matches the architecture's "two clients, one backend" model, isolates failure domains, and lets us debug the bot's logs separately. It also means the same `CapturePayload` contract works for both clients, no special code path.

**Going with separate-process.** Same convention as the extension, easier to reason about.

### Auth

Telegram bots are public — anyone who finds your bot's username can DM it. Solution: a `.env` allowlist.

```
TELEGRAM_BOT_TOKEN=...
ALLOWED_TELEGRAM_USER_IDS=12345678,87654321
```

The bot ignores any update whose `from.id` isn't in the allowlist. On `/start`, the bot replies with the sender's Telegram ID so you know what to put in `.env` the first time.

When Phase 5 (competition) lands, the quizmaster's user ID joins the allowlist and the bot starts handling group-chat messages too. Out of scope for Phase 1.

### Polling vs webhook

**Polling** for Phase 1 — works from localhost without exposing a public URL. The python-telegram-bot library handles this transparently (`Application.run_polling()`). When/if we deploy somewhere with a public URL, we can switch to webhook for lower latency.

### Commands

| Command | What it does |
|---|---|
| `/start` | Replies with your Telegram user ID and a short "send me anything" message. Shown the first time you talk to the bot. |
| `/help` | Lists commands and what content types are supported. |
| `/stats` | Mirrors `GET /stats` from the backend — total captures, breakdown by platform, last capture timestamp. |
| `/last` | Shows the title + URL of the most recent capture. |
| `/delete_last` | Removes the most recent capture from `data/captures.jsonl`. Insurance for accidental forwards (bank screenshots, personal photos). |
| `/pause` | Sets the bot to "paused" — acks messages with "⏸ paused" but doesn't process them. |
| `/resume` | Resumes processing. |

`/pause` and `/resume` deliberately mirror the Chrome extension toggle so behavior is consistent across clients. State lives in a small JSON file (`data/telegram_state.json`) — same idea as `chrome.storage.local`, just on disk.

### Mapping mobile content into the `CapturePayload` shape

The backend's `CapturePayload` schema has `url`, `title`, `text`, `images`, `platform`, `metadata`. Most mobile content doesn't fit cleanly. Mapping rules:

When the message contains a URL, treat it like the Chrome extension would: fetch the page (or YouTube transcript), set `platform` from the URL host, set `title` from `<title>` or `og:title`, set `text` to the extracted article body. The backend's existing `extractors.py` already handles YouTube via `youtube-transcript-api`.

When the message is an image (single or media group), set `url` to `tg://message/<message_id>`, `platform` to `telegram_image`, `title` to the image caption if any (or "Telegram image"), `text` to the caption, and `images` to the downloaded bytes (base64 data URL — same path the extension uses for inline images). The backend's vision pipeline takes it from there.

When the message is plain text without a URL — see decision #3 below for whether we capture it at all. If yes: `url = tg://message/<message_id>`, `platform = telegram_text`, `text = the message body`, `content_type = "thought"`.

When the message is a forward (Telegram's `forward_origin` is set), copy origin info into `metadata`: `{ "forwarded_from": "username or chat name", "forward_date": "..." }`. WhatsApp forwards lose their chain when crossing apps — there's nothing we can do about that — but Telegram-internal forwards keep attribution.

### File map (proposed)

| File | Role |
|---|---|
| `backend/telegram_bot/bot.py` | Main entry. `Application.run_polling()`, command handlers, message handlers. |
| `backend/telegram_bot/handlers.py` | One handler per content type — text/url, photo, media_group, voice (if decision #2 says yes), commands. |
| `backend/telegram_bot/state.py` | Read/write `data/telegram_state.json` for the pause flag. |
| `backend/telegram_bot/client.py` | Thin async HTTP client for POSTing to `http://127.0.0.1:8000/capture`. |
| `data/telegram_state.json` | `{"enabled": true}` — auto-created on first run. |
| `scripts/mock_telegram_capture.py` | Offline simulator: builds the same payloads the bot would produce and POSTs them. Useful when you don't want to spam your real bot. |

The empty `backend/telegram_bot/__init__.py` already exists.

### Smoke test plan (when built)

Mirror `phase1-smoke-test.md`'s structure:

1. **Pass 0.** Files exist, `TELEGRAM_BOT_TOKEN` and `ALLOWED_TELEGRAM_USER_IDS` set in `.env`.
2. **Pass 1.** Run the offline simulator (`mock_telegram_capture.py`) — proves the URL → page-fetch → POST path works without needing a real bot.
3. **Pass 2.** Run the real bot (`python -m backend.telegram_bot.bot`), send `/start`, confirm it replies with your user ID.
4. **Pass 3.** Forward a real Indian news article URL to the bot. Confirm: (a) immediate "📥 Got it" ack, (b) backend log shows `Processing capture`, (c) `data/captures.jsonl` gains a row, (d) follow-up message from bot with title.
5. **Pass 4.** Send a meme image with no caption. Confirm vision runs and the row in JSONL has an `image_descriptions` entry.
6. **Pass 5.** Send 5 photos at once (a media group). Confirm they're batched into one capture row, not five.

---

## Part 3 — Decisions (locked 2026-04-27)

### Decision 1 — Chattiness → **Minimal ack + failure reply**

The bot acks every received message with `📥 Captured` within ~1s. On successful processing it stays silent — no false-confidence summary. On failure it replies with one short line: `⚠️ Couldn't fetch — paywall` (or whatever the reason). Every failure also gets persisted to `data/capture_failures.jsonl` so a future agent can surface them on laptop wake or as a daily Telegram digest.

> *Rationale: Sabya's brain doesn't tell him "I remembered that"; the only true test is the quiz. So the bot mirrors that — silent on success, vocal only when the data didn't make it in.*

### Decision 2 — Voice notes → **Out of scope for Phase 1**

Voice notes (your own and forwarded WhatsApp ones) get a polite `🎙 voice notes not supported yet` reply. Adding transcription is a future-improvement item — see Part 5.

### Decision 3 — Your typed thoughts / interpretive layer → **Don't capture**

Rule of thumb: **capture what arrived in your stream of consumption; ignore your interpretive layer on top.**

| Case | Behavior |
|---|---|
| You type a bare URL | Capture URL. (It's a thing you saw, not a thought.) |
| You forward an image with no caption | Capture image. |
| You forward an image and add "lol cringe" yourself | Capture image. Drop your caption. |
| Image arrives with caption already from original sender | Capture image + caption together. |
| Quote-reply on an earlier capture: "this connects to the Tata thing" | Ignore. (Pure spoonfed connection.) |
| Bare typed text, no URL/image | Ignore. (Your thought.) |

> *Rationale: BrainTwin must develop its own emotional intelligence and connection-sense from observed consumption patterns — forwarding-chat origin, time of day, frequency clusters, dwell time, content's own emotional valence. Hand-feeding labels would corrupt the test of whether the system can think like Sabya on its own.*

---

## Part 4 — Build log (what got done this session, 2026-04-27)

All shipped:

1. ✅ Added `ALLOWED_TELEGRAM_USER_IDS`, `BACKEND_CAPTURE_URL`, `TELEGRAM_POST_MIN_INTERVAL_MS`, `TELEGRAM_CATCHUP_GAP_MINUTES`, `TELEGRAM_STATE_PATH`, `CAPTURE_FAILURES_PATH` to `backend/config.py` and `.env.example`.
2. ✅ Added `_log_failure()` to `backend/main.py`. Every `/capture` failure (processing or persist) now appends a structured row to `data/capture_failures.jsonl` with source/url/reason/preview. Added `GET /failures` endpoint.
3. ✅ Built `backend/telegram_bot/state.py` (pause flag + last-seen timestamp + media-group de-dup, atomic file writes).
4. ✅ Built `backend/telegram_bot/client.py` (`httpx.AsyncClient` + 800ms rate-limited POST to `/capture`, returns short human-readable failure reasons).
5. ✅ Built `backend/telegram_bot/handlers.py`. Commands: `/start /help /whoami /pause /resume /stats /last /failures`. Content: text-with-URL, single photo, photo-album with 2s batching window, voice→polite reject, video/document/sticker→polite reject. Implements Decision 3's "drop your own caption on forwards" rule.
6. ✅ Built `backend/telegram_bot/bot.py` entry point with placeholder-token guard, allowlist startup log, post-init/post-shutdown hooks, error handler. Run via `python -m backend.telegram_bot.bot`.
7. ✅ Wrote `scripts/mock_telegram_capture.py` — offline simulator with 4 scenarios (text/image/album/forward), stdlib-only.
8. ✅ Updated `docs/phase1-smoke-test.md` with Part B — 7 numbered Telegram passes from BotFather setup through laptop-sleep catch-up.
9. ✅ Hardened bot replies — removed `quote=True` (incompatible with python-telegram-bot v21+) and `parse_mode="Markdown"` (fragile with user data) so failures are loud, not silent.
10. ✅ Verified end-to-end: Sabya forwarded a real article from his phone → `📥 Captured` reply → `data/captures.jsonl` gained a row with `metadata.source: "telegram"`.

### Known open polish items (small, optional, not blocking Phase 2)

- **Google News URL extraction** — forwarding a Google News link results in `clean_text: ""` because Google News wraps everything in encrypted click-through redirects. Workaround: forward the article URL directly. Real fix: have `backend/capture/extractors.py` follow Google News' redirect chain (or detect+reject with a clearer error). Low priority — most consumption isn't via Google News.
- **Bot terminal log** — currently goes to stdout. Adding file logging (e.g. `data/bot.log`) would help when the bot runs unattended for a long stretch.

## Part 5 — Future improvements (deferred from Phase 1)

These were considered and explicitly deferred. Each is a self-contained later-session scope.

| Item | What | Why deferred |
|---|---|---|
| **Cloud / always-on bot** | Move the bot (and eventually backend) to a free Render / Railway / fly.io instance and switch from polling to webhook. Telegram POSTs to the public URL → bot processes → POSTs into your backend over a tunnel or hosted backend. Eliminates the ~24h Telegram getUpdates retention limit and the "laptop was off" gap entirely. | Phase 1 only needs to prove the capture pipeline works. ~24h queue is more than enough for a laptop on most of the day. Cloud move adds deployment + tunneling complexity that isn't needed yet. |
| **Voice transcription** | Local Whisper (free, slow on CPU) or OpenAI Whisper API (~$0.006/min, fast). Adds a `voice` content type and runs the audio through transcription before posting. | Adds a dependency, latency budget, and (for cloud Whisper) a second API key. Most of mobile consumption is text/image. Revisit when WhatsApp voice forwards become a real chunk of what Sabya consumes. |
| **Failure-summary agent** | Small daemon (or extension popup hook) that reads `data/capture_failures.jsonl` and on laptop wake sends Sabya a Telegram digest: *"Yesterday: 23 captures, 2 failures (paywall ×1, timeout ×1)."* Maybe also surfaces in the Chrome popup. | Phase 1 already persists every failure to `capture_failures.jsonl` and replies inline on the failing message; the digest is a quality-of-life add, not load-bearing. |
| **Style / voice corpus** | Capture Sabya's typed text into a separate `data/style_corpus.jsonl` (firewalled from the knowledge graph) so the Phase 5 quiz bot can mirror his actual texting style — emoji choices, sparseness, "lol" sarcasm vs. sincerity. | Phase 5 problem. Phase 1's "don't capture thoughts" rule preserves test integrity; style mirroring sits at generation time, not knowledge time. |
| **Emoji-reaction feedback** | Treat 👍 / 👎 reactions on bot quiz answers as labeled feedback ("the twin got this right / wrong"). | Phase 5 problem — no quiz exists yet. Worth knowing the data path is reserved. |
| **Webhook + zero-loss buffering** | Even before full cloud move, switching to webhook mode behind ngrok / Cloudflare Tunnel removes the polling delay and the 24h Telegram retention cap. | Same logic as cloud move — overkill for Phase 1 personal-laptop use. |

---

## Part 6 — Where to start next session

**Phase 1 is closed.** Both capture clients are live, the contract (`CapturePayload`) is stable, failure logging is in place, and a real piece of phone-consumed content has made it into `data/captures.jsonl` end-to-end. Don't reopen Phase 1 unless something regresses.

### Next phase per `docs/architecture.html` → Phase 2: Processing Pipeline (LLM enrichment)

Right now `/capture` does only what Phase 1 needs: text normalization, optional vision-on-images, YouTube transcript fetch, then append the row to JSONL. **The row has no summary, no entities, no topics, no sentiment.** Phase 2 adds those.

**Concrete kickoff steps** (in order):

1. **Re-read the phasing.** Open `docs/architecture.html` and confirm the Processing Pipeline card matches your current intent — it's the source of truth for what fields enrichment must produce.
2. **Audit the stub.** `backend/knowledge/` exists but is largely empty. List what's there, decide what stays, what's renamed, what's new.
3. **Design the enrichment schema.** A new pydantic model (e.g. `EnrichedCapture`) on top of `CapturePayload` with: `summary` (1-2 sentence), `entities` (people / orgs / places), `topics` (free-text tags), `sentiment` (the consumed content's emotional valence — *not* Sabya's), `content_type_refined` (e.g. "news/politics/india", "meme/text-on-image"), and `language`. Keep it small — every field must earn its place by being something the Phase 4 agent will actually query on.
4. **Pick the model.** `.env` already has `ENRICHMENT_MODEL=claude-haiku-4-5-20251001`. Haiku is cheap enough to run on every capture; Sonnet is for the agent. Stick with the split.
5. **Wire it into `/capture`.** The natural seam is *after* JSONL append (so raw capture is never lost if enrichment fails) and *before* any later phase's storage write. Enrichment failure should land its own row in `capture_failures.jsonl` with `phase: "enrichment"` so the existing `/failures` endpoint and Decision-1 telegram digest both surface it.
6. **Write a smoke test.** Add Part C to `docs/phase1-smoke-test.md` (or start `phase2-smoke-test.md`) with: text-only article in → enriched fields out; image-only meme in → vision description fed to enricher → topics out; YouTube link in → transcript-driven topics out.

After Phase 2 is solid, **Phase 3 (storage)** is the natural next step — wire the enriched rows into ChromaDB (semantic search) and SQLite (structured filters like "show me all India-politics captures from last week"). The directories already exist (`data/chroma/`, `data/braintwin.db` paths in `.env`); the code paths to write them don't.

### Optional warm-up work (if you want to ease in before tackling Phase 2)

Either of the open polish items from Part 4 is a small, self-contained 30-60min task:

- **Google News URL extraction** — make `backend/capture/extractors.py` follow the Google News redirect chain so forwarded GN links don't land as `clean_text: ""`. Real fix, not just a "forward direct URL" workaround.
- **Bot file logging** — pipe the `braintwin.telegram` logger to `data/bot.log` (rotating handler, keep last few MB) so unattended bot runs are debuggable after the fact.

Neither blocks Phase 2; both make the system slightly more pleasant to live with.

### What NOT to do next session

- **Don't start the Chrome popup redesign / extension polish.** It works; leave it.
- **Don't add voice-note transcription yet.** It's deferred for a reason (Part 5 — voice forwards aren't a big enough share of mobile consumption to justify the dependency).
- **Don't move the bot to cloud yet.** Same reason — laptop-on-most-of-the-day + 24h Telegram queue covers the gap. Revisit when Phase 3 storage is in place and you actually need 24/7 ingestion for accuracy.
- **Don't capture typed thoughts.** Decision 3 stays locked through Phase 4. If Phase 5 quiz mode needs Sabya's voice, that's a separate `style_corpus.jsonl` (Part 5), not a relaxation of the capture rule.

---

## Appendix — What the architecture docs say vs what's actually built

There's drift between `docs/architecture.html` (the visual) and `docs/architecture-detailed.md` (the markdown):

| Source | Phase 1 contains |
|---|---|
| `architecture.html` | Capture layer only: Chrome extension + Telegram bot + "what gets captured." Processing, storage, agent, competition are Phases 2-5. |
| `architecture-detailed.md` | "Foundation": project structure + FastAPI capture endpoint + ChromaDB+SQLite + processing pipeline. Chrome extension is Phase 2; Telegram is Phase 3. |

**Going with the HTML's phasing** since that's the version you've been pointing at. The markdown phasing is incidentally what the codebase already partially follows (backend pipeline exists; storage/enrichment don't), but for naming consistency we'll call the current capture-layer work "Phase 1."
