# Phase 2.5 — Capture Hydration & Enrichment Hygiene

> **Status as of 2026-04-28 — DESIGN SIGNED OFF, NOT YET BUILT.**
>
> Phase 2.5 is the follow-up to Phase 2 that closes a real gap surfaced by Phase 2's smoke test: forwarding Instagram reels and Facebook share links from Telegram landed those captures in `capture_failures.jsonl` with `reason: "empty_content"`. Phase 2 did the right thing — it correctly refused to call Haiku on `""`. But the empty content was a Phase 1 capture-layer gap, not an enrichment bug. Phase 2.5 fixes that gap and quiets the false-failure noise it produced.

---

## What Phase 2.5 does

Four small, ordered, independently shippable fixes:

| Fix | What | Where it lives | Cost | Why it matters long-term |
|---|---|---|---|---|
| **1 — Hygiene** | Re-tag `EmptyContentError` and `ContentTooLongError` as `phase: "enrichment_skipped"` instead of `phase: "enrichment"` failures. | `enrichment_worker.py`, `main.py` (`/stats`, `/failures`), bot `cmd_failures` | $0 | The failure log has to mean "something broke", not "nothing to do". Without this, every empty capture pollutes the metric the daily digest will eventually watch. |
| **2 — Free metadata** | At capture time, hydrate URL-only payloads using (a) Telegram's built-in link preview when present, (b) an Open Graph / Twitter Card / `<title>` fetch as fallback. | `backend/telegram_bot/handlers.py`, new `backend/capture/og_fetcher.py`, hook in `processor.py` | $0 (one HTTP GET per URL miss) | Catches ~80% of all URL-only captures — articles, IG public posts, FB share links, Reddit threads, X cards, Substack — for zero recurring cost. The 80/20 fix. |
| **3 — Local video transcription** | For IG reels, FB videos, TikTok, anything yt-dlp recognises as video, download audio and transcribe locally with `whisper.cpp` `small.en`. Attach as `transcript`. | New `backend/capture/video_transcriber.py`, hook in `processor.py` | $0 (250 MB disk, M-series Mac CPU) | This is the only path that actually captures the **content of a reel** — what was said, not just what the page is about. Aligns Phase 2.5 with BrainTwin's north star: the agent quizzing me on what I *consumed*, not just what I shared. |
| **4 — Verify + doc** | Replay the 4 original failure URLs through the now-fixed pipeline, document the new tiered hydration in `docs/phase2-smoke-test.md` as a Pass 8. | `scripts/replay_failed_urls.py`, `docs/phase2-smoke-test.md` | <$0.01 (4 Haiku enrichments) | Closes the loop on the bug that motivated Phase 2.5 and gives future-me a debugger trace for "why is this capture empty?" |

Total budget: ~6 hours, ~$0 recurring, ~250 MB one-time disk.

---

## The bug Phase 2.5 fixes — debugger trace

Forwarding `https://www.instagram.com/reel/DXqmGydAAFx/...` to the Telegram bot today produces this chain:

1. **Telegram → Bot.** Bot API delivers `update.message.text = "<url>"`. No video bytes — Telegram only renders the preview client-side.
2. **`handle_text`** (`handlers.py:328-343`) builds payload with `text=""`, `images=[]`, `title="Telegram link"`, `platform="instagram"`. The comment claims "backend fetches the page itself" — **the comment is wrong**. The backend has no fetcher.
3. **`processor.process()`** calls `extract(raw_text="", url=<ig_url>, platform="instagram")`.
4. **`extract()`** (`extractors.py:148-164`) only knows YouTube. For everything else it normalizes the empty string and returns `clean_text=""`, `transcript=None`. Vision is skipped because `images=[]`.
5. **Capture row is persisted** to `data/captures.jsonl` with empty content. `/capture` returns 200. BackgroundTasks fires.
6. **Worker calls `enrich(processed)`** → `combined_text` is `""` → `enrichment.py:131` raises `EmptyContentError("no clean_text, transcript, or image_text to enrich")`.
7. **Worker** (`enrichment_worker.py:139`) catches it and writes the row with `phase: "enrichment", reason: "empty_content"`.

`title: "Telegram link"` and `text_preview: ""` in those failure rows are the giveaway — they're the literal placeholder values the bot sets when it has only a URL.

The fix has to happen one or two seams earlier: either fill in the content at capture time (Fix 2 + Fix 3), or stop counting "nothing to enrich" as a failure (Fix 1). Phase 2.5 does both.

---

## Fix 1 — Enrichment hygiene

### What

Empty content and over-long content aren't really enrichment failures — they're "not applicable". Re-tag them so they don't pollute the failure metrics.

Today (Phase 2):

```jsonl
{"phase": "enrichment", "reason": "empty_content", ...}
{"phase": "enrichment", "reason": "content_too_long: 52000 chars > 50000 cap", ...}
```

After Phase 2.5:

```jsonl
{"phase": "enrichment_skipped", "reason": "empty_content", ...}
{"phase": "enrichment_skipped", "reason": "content_too_long: 52000 chars > 50000 cap", ...}
```

### Where

- `backend/knowledge/enrichment_worker.py` — branch `EmptyContentError` and `ContentTooLongError` to a new `_log_enrichment_skipped` helper that writes `phase: "enrichment_skipped"`. Network/auth/malformed-JSON failures keep `phase: "enrichment"`.
- `backend/main.py` `/stats` — add `enrichments.skipped: int` separate from the existing `pending` count.
- `backend/main.py` `/failures` — `phase` query param accepts `enrichment_skipped`. Default response excludes skipped from `total` and `by_phase`.
- `backend/telegram_bot/handlers.py` `cmd_failures` — header line ignores `enrichment_skipped` rows. New `/skipped` command (or `/failures skipped`) surfaces them on demand.

### Why

The failure log is the daily digest's input source (Phase 5). If it includes "nothing to enrich" rows, the digest will scream every day and I'll learn to ignore it — exactly the alert-fatigue trap that kills observability systems. Separating "nothing to do" from "something broke" is hygiene work that pays dividends every phase forward.

### Exit criteria

- The 4 IG/FB rows from the bug report move from `failures` to `skipped`.
- `curl /stats` shows `enrichments.skipped: 4` and `failures.by_phase.enrichment: 0` for those rows.
- Bot `/failures` reply no longer mentions them by default.
- Existing real failures (network, malformed JSON) still appear in `phase: "enrichment"`.

---

## Fix 2 — Free metadata layer (Telegram preview + OG fetch)

### What

Two sub-pieces, both running at capture time before the row is persisted, layered cheapest-first:

**2.A — Telegram link preview pickup.**
When the user forwards a URL, Telegram's Bot API often attaches a `link_preview_options` / `web_page` object containing `title`, `description`, and a thumbnail file_id (Telegram's servers crawled the page for the preview). Pull it.

**2.B — Backend OG metadata fetcher.**
For URLs that arrive without a Telegram preview, or directly from the Chrome extension when text extraction returned empty, do a single HTTP GET with a short timeout and parse:

- `og:title`, `og:description`, `og:image` (Open Graph)
- `twitter:title`, `twitter:description`, `twitter:image` (Twitter Cards — fallback)
- `<title>`, `<meta name="description">` (HTML — last-resort fallback)

Image URL goes through the existing Vision pipeline. Description becomes `clean_text`. Title overwrites the `"Telegram link"` placeholder.

### Tiering rule (locked)

```
1. Use raw_text if non-empty (Chrome extension already extracted it).
2. Else use Telegram preview if present (free — Telegram already crawled).
3. Else fetch OG metadata from URL.
4. Else fall through to Fix 3 (yt-dlp + Whisper) if it's a video URL.
5. Else mark phase: "enrichment_skipped" with reason "no_extractable_content".
```

Each tier is independently useful. Tier 4 is Fix 3 (separate ship). Tier 5 is Fix 1.

### Where

- `backend/telegram_bot/handlers.py` — `handle_text` reads `msg.link_preview_options` / `msg.web_page` and attaches `title`, `text` (description), and `images` (downloaded thumbnail) to the payload.
- `backend/capture/og_fetcher.py` — **new file**. Single function `fetch_og_metadata(url: str) -> OGMetadata | None`. Uses `httpx.AsyncClient` with 5s timeout, 2 redirects max, browser User-Agent. Parses with `selectolax` (fast HTML parser, ~150 KB).
- `backend/capture/processor.py` — when `extracted.clean_text == ""` AND `transcript is None`, call `fetch_og_metadata(url)`. If it returns content, set `clean_text` to the description, `text_source = "og_metadata"`, append the OG image to `image_descriptions` after Vision.

### Tech additions

| Package | Why | Size |
|---|---|---|
| `httpx` | Already in deps for the bot client. | (existing) |
| `selectolax` | Fast HTML parser (~5× faster than BeautifulSoup, no lxml dependency). | ~150 KB |

Add `selectolax` to `requirements.txt`.

### Why

Most URLs you forward have proper OG tags. News sites, Reddit, IG public posts, FB share links, Substack, Medium, blogs — all of them serve `og:title` + `og:description` to be social-shareable, which is exactly the structured summary BrainTwin needs to enrich. Skipping this is leaving ~80% of the easy wins on the table.

The Telegram-preview tier specifically saves you from making the HTTP request at all when Telegram has already done the work — your bot doesn't get rate-limited by Instagram, FB, etc. for re-fetching links.

### Exit criteria

- Re-forward one of the 4 IG URLs → capture row has real `title` (post caption or "Instagram") and non-empty `clean_text` (the OG description).
- Same URL goes through enrichment successfully and produces a meaningful `summary`, `entities`, `key_facts`, `topics`.
- A news article URL forwarded as text picks up its title + lede via OG fetch.
- A YouTube URL still uses the existing transcript path (OG fallback never triggers because YouTube extraction succeeds first).

---

## Fix 3 — Local video transcription (yt-dlp + whisper.cpp)

### What

For IG reels, FB videos, TikTok, YouTube shorts where the transcript API failed — download the audio stream, run `whisper.cpp` `small.en` locally, attach the transcript to `ProcessedContent`. The existing enrichment pipeline already knows what to do with `transcript` (it's part of `combined_text`).

### Pipeline per video URL

```
1. URL recognized as video (instagram /reel/, /p/ video, facebook /share/, tiktok, etc.)
2. yt-dlp extracts audio-only stream → temp .m4a in /tmp
3. whisper.cpp small.en → transcript text
4. Delete temp .m4a
5. Attach transcript to ProcessedContent.transcript, source = "whisper_local"
6. Phase 2 enrichment runs as normal — combined_text now has real content
```

### Tech additions

| Tool | Role | Size | One-time setup |
|---|---|---|---|
| `yt-dlp` (Python lib) | Pull audio from IG/FB/TikTok/YouTube | ~5 MB | `pip install yt-dlp` |
| `whisper.cpp` binary | Local transcription | ~5 MB | Build from source on Mac (~5 min) |
| `ggml-small.en.bin` | Whisper model file | ~250 MB | Download once into `data/models/` |

Both `yt-dlp` and `whisper.cpp` are invoked as subprocesses — no Python wrapper code needed beyond a thin orchestrator.

### Where

- `backend/capture/video_transcriber.py` — **new file**. Functions:
  - `is_video_url(url: str, platform: str) -> bool` — pattern match
  - `transcribe_video(url: str) -> str | None` — runs yt-dlp + whisper.cpp, returns text or None
- `backend/capture/processor.py` — after the YouTube transcript branch, add: if `is_video_url(url, platform)` and no transcript yet, call `transcribe_video(url)`.
- `bin/whisper-cli` — local-only, gitignored.
- `data/models/ggml-small.en.bin` — local-only, gitignored.

### Cost

Hard cost: **$0/month**.

| Resource | Cost |
|---|---|
| Disk one-time | ~250 MB model + ~5 MB binary |
| RAM per transcription | ~1 GB (released after) |
| CPU per 30s reel on M-series Mac | ~5 seconds |
| Maintenance | `pip install -U yt-dlp` every 2-4 weeks when IG/FB shift their endpoints |

If volume ever explodes (>100 reels/day) or the Mac is offline, swap in OpenAI Whisper API ($0.006/min, ~$3/mo at 20 reels/day). Drop-in 5-line change.

### Why

This is the only fix in Phase 2.5 that actually captures **video content**. OG metadata gives you "Reel by @user" + the caption. Whisper gives you what the person actually said in the reel — which is the substrate for any future quiz question that's not just "who posted this".

It's also the most expensive in setup time (~3 hours) and the most fragile long-term (yt-dlp drift). Sequencing it last in Phase 2.5 means Fixes 1 + 2 ship the hygiene win and the 80% win independently of whether Whisper integration goes smoothly.

### Exit criteria

- Forward one of the IG reel URLs → `processed.transcript` is populated with real spoken-text.
- Enrichment row's `summary` and `key_facts` reference content that's only available in the transcript (not in the OG description).
- Transcription completes inside the BackgroundTask in <10 seconds for a 30-60 second reel.
- No orphan `.m4a` files left in `/tmp` or `data/`.

---

## Fix 4 — End-to-end verification

### What

Replay the 4 original failure URLs through the now-fixed pipeline. Confirm each produces a real enrichment row with non-trivial summary + key_facts + topics. Document the new tiered hydration model in `docs/phase2-smoke-test.md` as a new Pass.

### Where

- `scripts/replay_failed_urls.py` — **new**. Reads `data/capture_failures.jsonl`, dedupes URLs, POSTs each back through `/capture` with the bot's payload shape. Polls for the resulting enrichment row.
- `docs/phase2-smoke-test.md` — append a "Pass 8 — capture hydration" section walking through forward-an-IG-reel-and-watch-it-enrich.

### Exit criteria

- All 4 of the original IG/FB URLs end up with real enrichment rows (or are honestly tagged `enrichment_skipped` if the post is private/age-gated).
- `/stats` shows `enrichments.skipped` near zero for newly-forwarded URLs.
- `docs/phase2-smoke-test.md` Pass 8 documents the tier order so future debugging knows where to look first.
- This doc's status flips from "DESIGN SIGNED OFF, NOT YET BUILT" to "PHASE 2.5 LIVE".

---

## Why a 2.5 and not a 3.0

Phase 3 is the storage layer (ChromaDB + SQLite). It's a meaningful architectural step — different infra, different code paths, different testing posture. Conflating "fix the IG/FB capture gap" with "introduce vector storage" would muddle both:

- It would delay Phase 3 by the full ~6 hours of Phase 2.5 work.
- It would risk landing storage on top of an enrichment pipeline still producing junk rows.
- It would make rollback harder if Whisper integration turns out to be flaky — you'd lose the storage progress too.

Phase 2.5 is small, testable in a single afternoon, and naturally splits into 4 commits (one per fix). It deserves its own design doc and its own status.

---

## Sequencing rules

1. **Ship fixes in order.** Each one is independently useful and doesn't depend on the others. If Whisper integration (Fix 3) hits trouble, Fixes 1 + 2 still close 80% of the gap.
2. **Smoke-test between each.** After Fix 1 ships, re-forward the 4 URLs and confirm they move to `enrichment_skipped`. After Fix 2, confirm at least the IG public post hydrates via OG. After Fix 3, confirm the reel transcribes.
3. **Don't add new features inside Phase 2.5.** Tempting additions like TikTok-specific scraping, browser-screenshot fallback, or Anthropic web search tool integration are explicitly Phase 5+. They belong in their own design pass after we've watched Phase 2.5 run for a few weeks.

---

## Open polish carried forward into Phase 2.5

- **Comment correction.** The misleading `"backend fetches the page itself"` comment in `handlers.py:333` gets fixed as part of Fix 2 — it'll be replaced with an accurate description of the new tiered hydration.
- **Bot file logging.** Carried over from Phase 2's deferred list. Not blocking Phase 2.5.

---

## Next: implementation

After this doc is signed off:

1. Start with **Fix 1**. Single PR / commit. Smoke-test by re-forwarding the 4 IG/FB URLs from the bug report; confirm they move to `enrichment_skipped`.
2. Then **Fix 2**. Two-stage commit (Telegram preview pickup, then OG fetcher) so each is reviewable independently.
3. Then **Fix 3**. Single PR but expect two passes — first the orchestrator + yt-dlp wiring, second the whisper.cpp build + first-real-reel test.
4. Then **Fix 4**. Replay script + smoke-test doc update + flip this doc's status.

When all four are green, move on to Phase 3 (storage layer — ChromaDB + SQLite).
