# Phase 2.5 — Capture Hydration & Enrichment Hygiene

> **Status as of 2026-04-30 — PHASE 2.5 LIVE.** Fix 1 (hygiene) shipped. Fix 2.A (OG fetcher) shipped. Fix 2.B (Telegram preview) cancelled — Bot API limitation, see "Fix 2.B — cancelled" below. Fix 3 (video transcription) shipped + verified end-to-end with a real IG reel. Fix 4 (replay script + Pass 8 smoke-test) shipped — see [phase2-smoke-test.md Pass 8](phase2-smoke-test.md#pass-8--capture-hydration-phase-25).
>
> Phase 2.5 is the follow-up to Phase 2 that closes a real gap surfaced by Phase 2's smoke test: forwarding Instagram reels and Facebook share links from Telegram landed those captures in `capture_failures.jsonl` with `reason: "empty_content"`. Phase 2 did the right thing — it correctly refused to call Haiku on `""`. But the empty content was a Phase 1 capture-layer gap, not an enrichment bug. Phase 2.5 fixes that gap and quiets the false-failure noise it produced.

---

## Progress at a glance

| Fix | Status | Smoke-tested? |
|---|---|---|
| **1 — Enrichment hygiene** | ✅ **Shipped** (2026-04-29) | Pending end-to-end re-forward |
| **2.A — OG metadata fetcher** | ✅ **Shipped** (2026-04-29) | Pending: pip install + pytest + 1 fresh IG/FB forward |
| **2.B — Telegram link-preview pickup** | ❌ **Cancelled** (2026-04-29) | Bot API limitation — see "Fix 2.B — cancelled" below |
| **3 — Local video transcription (yt-dlp + whisper.cpp)** | ✅ **Shipped & verified** (2026-04-30) | ✅ Real IG reel forward → `tier="video_transcript"` row in hydrations.jsonl, transcript-derived enrichment confirmed |
| **4 — Replay script + smoke-test doc** | ✅ **Shipped** (2026-04-30) | Pending: run `python -m scripts.replay_failed_urls --dry-run` followed by a real run. See [Pass 8](phase2-smoke-test.md#pass-8--capture-hydration-phase-25). |

Done so far: hygiene split (failures vs skipped) + cheapest hydration tier (HTTP GET + OG parsing) + video transcription (yt-dlp + whisper.cpp local) with a merge model that combines OG caption with the spoken transcript. What's left: a replay script that reruns the original 4 IG/FB failure URLs through the full pipeline, plus a Pass 8 in `phase2-smoke-test.md` documenting the new flow. Fix 2.B was cancelled when we discovered the Bot API doesn't deliver preview content (only the user's MTProto client does); Fix 2.A's OG fetcher already covers everything Fix 2.B was supposed to provide.

---

## What Phase 2.5 does

Four small, ordered, independently shippable fixes:

| Fix | Status | What | Where it lives | Cost | Why it matters long-term |
|---|---|---|---|---|---|
| **1 — Hygiene** | ✅ Shipped | Re-tag `EmptyContentError` and `ContentTooLongError` as `phase: "enrichment_skipped"` instead of `phase: "enrichment"` failures. | `enrichment_worker.py`, `main.py` (`/stats`, `/failures`), bot `cmd_failures`, `tests/test_enrichment.py` | $0 | The failure log has to mean "something broke", not "nothing to do". Without this, every empty capture pollutes the metric the daily digest will eventually watch. |
| **2.A — OG fetcher** | ✅ Shipped | When a capture arrives empty, fetch the URL once and parse `og:` / `twitter:` / `<title>` / `<meta name="description">`. Hydrate the in-memory `ProcessedContent` before enrichment runs. Persist a sidecar row to `data/hydrations.jsonl` (audit trail; `captures.jsonl` stays immutable). | New `backend/capture/og_fetcher.py`, new `backend/capture/hydration.py`, hook in `enrichment_worker.enqueue_enrichment`, new `tests/test_og_fetcher.py` + `TestHydrationHook` cases, `selectolax` dep, 4 new `Settings` fields. | $0 (one HTTP GET per URL miss) | Catches ~80% of all URL-only captures — articles, IG public posts, FB share links, Reddit threads, X cards, Substack — for zero recurring cost. The 80/20 fix. |
| **2.B — Telegram preview** | ❌ Cancelled | Original idea: lift `link_preview_options` / `web_page` from incoming messages so we don't re-fetch what Telegram already has. **Cancelled 2026-04-29 after discovering the Telegram Bot API doesn't expose preview content** (only `LinkPreviewOptions` settings — `is_disabled`, `url`, `prefer_*_media`, `show_above_text`). Bots only receive the URL string; the rendered preview is a client-side MTProto thing. Fix 2.A's OG fetcher already gives us everything 2.B was meant to provide. | (none) | (n/a) | Optimization that turned out to not be possible. User-facing outcome unchanged — Fix 2.A handles the same content. |
| **3 — Local video transcription** | ✅ Shipped | For IG reels, FB videos, TikTok, anything yt-dlp recognises as video, download audio and transcribe locally with `whisper.cpp` `small.en`. On video URLs runs IN ADDITION to OG fetch — caption + transcript both feed the LLM. | New `backend/capture/video_transcriber.py`, extended `backend/capture/hydration.py` with merge model + `tiers_used` field, new `scripts/setup_whisper.sh`, new `tests/test_video_transcriber.py`, new `TestVideoTranscriptMerge` cases, `yt-dlp` dep, 5 new `Settings` fields. | $0 (250 MB disk, M-series Mac CPU) | This is the only path that actually captures the **content of a reel** — what was said, not just what the page is about. Aligns Phase 2.5 with BrainTwin's north star: the agent quizzing me on what I *consumed*, not just what I shared. |
| **4 — Verify + doc** | ✅ Shipped | Replay the historical failure URLs through the now-fixed pipeline; document the tiered hydration in `docs/phase2-smoke-test.md` as Pass 8. | New `scripts/replay_failed_urls.py` + `tests/test_replay_failed_urls.py`; appended Pass 8 (with sub-passes for setup, unit tests, single reel, article URL, YouTube URL, replay) to `docs/phase2-smoke-test.md`. | <$0.01 per replay (Haiku enrichment cost) | Closes the loop on the bug that motivated Phase 2.5 and gives future-me a debugger trace for "why is this capture empty?" |

Total budget: ~6 hours, ~$0 recurring, ~250 MB one-time disk. Shipped so far: ~3 hours.

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

### ✅ Shipped (2026-04-29)

- `enrichment_worker.py` — added `_log_enrichment_skipped` + shared `_build_enrichment_log_row`. `EmptyContentError` and `ContentTooLongError` now route to skipped.
- `find_unenriched_capture_ids` — also takes `failures_path` and excludes IDs already tagged `enrichment_skipped`, so the startup-recovery scan doesn't re-call Haiku on the same hopeless rows after every restart.
- `main.py` — `/stats` now reports `enrichments.skipped` and excludes it from `pending`. `/failures` adds `?include_skipped=true` flag and `?phase=enrichment_skipped` filter; default response hides skipped rows.
- `backend/telegram_bot/handlers.py` — bot's `/failures` reply tags rows `[skipped]` / `[enrich]` / `[<source>]`.
- `tests/test_enrichment.py` — empty / too-long tests assert `phase == "enrichment_skipped"`; new `test_skipped_ids_are_excluded` proves recovery-scan exclusion.

Pending: re-forward the 4 historical IG/FB URLs and confirm the live behaviour. (Code changes above are unit-tested; only the end-to-end re-forward is left.)

---

## Fix 2 — Free metadata layer (OG fetch + Telegram preview)

Two sub-pieces, layered cheapest-first. **The original design proposed Telegram-preview-first (2.A) and OG-fetcher-second (2.B); during implementation we flipped the order to ship OG first because it covers both the bot path and the Chrome empty-text path, while Telegram preview only helps the bot path.** Tier order at runtime is unchanged — Telegram preview will still run before OG once 2.B ships.

### Tiering rule (revised after 2.B cancellation)

```
1. Use raw_text if non-empty (Chrome extension already extracted it).         ← Phase 1
2. Else fetch OG metadata from URL.                                            ← Fix 2.A (shipped)
3. Else if video URL → yt-dlp + whisper.cpp local transcription.               ← Fix 3 (next)
4. Else mark phase: "enrichment_skipped" with reason "no_extractable_content". ← Fix 1
```

Originally a 5-tier model; collapsed to 4 when Fix 2.B was cancelled (see "Fix 2.B — cancelled" below). Each remaining tier is independently useful.

---

## Fix 2.A — Backend OG metadata fetcher ✅ Shipped

### What

For URLs that arrive empty (the bot's "URL-only forward", the Chrome extension when text extraction returned `""`), do a single HTTP GET with a short timeout and parse:

- `og:title`, `og:description`, `og:image` (Open Graph)
- `twitter:title`, `twitter:description`, `twitter:image` (Twitter Cards — fallback)
- `<title>`, `<meta name="description">` (HTML — last-resort fallback)

Description becomes the new `clean_text`. Title replaces the `"Telegram link"` placeholder. Image URL is **captured but not downloaded or fed to Vision in this commit** (deferred to Phase 3 — see "Image deferral" below).

### Where it runs (revised from original design)

The original design said "in `processor.py`, before the row is persisted." User sign-off flipped this to **inside `BackgroundTasks`, before enrichment**. Two reasons:

1. The bot's `📥 Captured` ack stays fast — no 5-second OG fetch on the user-facing path.
2. The captures.jsonl row stays as the immutable record of what arrived from the client. Hydration writes a separate sidecar (`data/hydrations.jsonl`), matching the existing `enrichments.jsonl` pattern.

Trade-off: the captures.jsonl row's `clean_text` field is the empty original. Consumers that want the hydrated text must join `captures.jsonl` ⨝ `hydrations.jsonl` ⨝ `enrichments.jsonl` by `capture_id`. Phase 3 storage layer collapses this into a single row.

### Files shipped

| Path | Role |
|---|---|
| `backend/capture/og_fetcher.py` (new) | Pure async `fetch_og_metadata(url, *, client=None)` using `httpx` + `selectolax`. 5s timeout, 2 redirects, browser User-Agent, 256 KB body cap. Returns `OGMetadata` dataclass with `is_useful()` gating on a non-empty description. Best-effort: any error returns `None`. |
| `backend/capture/hydration.py` (new) | `hydrate_processed(capture_id, processed)` orchestrator. Decides if hydration is needed (URL present + `combined_text` empty), calls the fetcher, returns a `HydrationResult` with the hydrated `ProcessedContent` and the sidecar log row. Replaces `_PLACEHOLDER_TITLES` like `"Telegram link"` with the real OG title. |
| `backend/knowledge/enrichment_worker.py` | `enqueue_enrichment` calls `hydrate_capture` before `enrich()`. Persists the sidecar row via new `_persist_hydration` helper. Defensive try/except so a buggy fetcher can't kill enrichment. |
| `backend/config.py` | Added `hydrations_path`, `og_fetch_timeout_seconds`, `og_fetch_max_redirects`, `og_fetch_enabled` (kill-switch in case a site hangs the worker). |
| `requirements.txt` | Added `selectolax`. |
| `tests/test_og_fetcher.py` (new) | Parser unit tests (og/twitter/html priority, `is_useful()` gating, whitespace, garbage input). HTTP integration tests via `httpx.MockTransport` (happy path, 4xx, network error, non-http URL, oversized body truncation). |
| `tests/test_enrichment.py` | `tmp_jsonl` fixture redirects `hydrations_path` and disables OG fetch by default. New `TestHydrationHook` class with 5 cases (hydration happy path, disabled fall-through, title-only filter, non-empty capture skips fetch, fetcher exception is caught). |

### What `data/hydrations.jsonl` stores

One row per hydrated capture. Same sidecar pattern as `enrichments.jsonl` — joined by `capture_id`. Schema:

```json
{
  "timestamp": "...",
  "capture_id": "uuid",
  "url": "https://...",
  "tier": "og_metadata",
  "source": "og",
  "title_before": "Telegram link",
  "title_after": "Real Article Title",
  "title_replaced": true,
  "clean_text_before_chars": 0,
  "clean_text_after_chars": 247,
  "image_url": "https://cdn.example.com/i.jpg",
  "site_name": "Instagram"
}
```

Fix 2.B will write rows with `tier: "telegram_preview"`. Fix 3 will write rows with `tier: "video_transcript"`. The tier field tells future-you exactly which layer hydrated this capture.

### Image deferral

The original design said image URL goes through Vision. We're not doing that in Fix 2.A — we just record `image_url` in the sidecar row. Reasons:

- Avoids a second HTTP GET per capture (the og:image download).
- Avoids a second Haiku Vision call per capture (~$0.001 each).
- Telegram thumbnails would also need a `getFile` API call to download. Better to handle all image-side logic in one place once Fix 2.B ships.

Image vision integration becomes a Phase 3 nice-to-have.

### Tech additions

| Package | Why | Size |
|---|---|---|
| `httpx` | Already in deps for the bot client. | (existing) |
| `selectolax` | Fast HTML parser (~5× faster than BeautifulSoup, no lxml dependency). | ~150 KB |

### Exit criteria — pending verification

- pip install + `pytest tests/test_og_fetcher.py tests/test_enrichment.py` runs all-green.
- Re-forward an IG URL → `data/hydrations.jsonl` gains a row with `tier: "og_metadata"`, `data/enrichments.jsonl` gains a row with non-empty summary/entities/key_facts/topics.
- A news article URL forwarded as text picks up its title + lede via OG fetch.
- A YouTube URL still uses the existing transcript path (OG fallback never triggers because YouTube extraction succeeds first).
- `/stats` shows `enrichments.total` increased and `enrichments.skipped` did not.

---

## Fix 2.B — Telegram link-preview pickup ❌ Cancelled (2026-04-29)

### Why this is here

So that future-you (or a future contributor) doesn't re-investigate this and waste an afternoon. The original design assumed bots could read the rendered preview content Telegram caches for each URL. They can't.

### The finding

`Message.link_preview_options` (PTB v20.8+, Bot API 7.x) is a `LinkPreviewOptions` object with exactly these fields:

```
is_disabled, url, prefer_small_media, prefer_large_media, show_above_text
```

That's it. No `title`, `description`, `image`, or any preview content. There is no `WebPage` object on incoming `Message` instances. The Telegram Bot API simply does not deliver the rendered link preview to bots.

The reason is architectural: Telegram clients render the preview by calling `messages.getWebPagePreview` on the MTProto API, which is the *user-client* API (Telethon, Pyrogram, the official Telegram apps). The Bot API is a separate, narrower interface, and that endpoint isn't exposed there.

### What we'd need if we ever wanted to do this

A second process running as a **user** (not a bot), authenticated against MTProto via something like `telethon`, that listens to your saved messages or a private channel and forwards content to BrainTwin. That's a much larger code surface (different auth flow, session management, two listeners to keep alive) and isn't worth it when Fix 2.A's OG fetcher already returns the same content (og:title + og:description + og:image are exactly what Telegram's preview shows).

### Impact on the rest of Phase 2.5

- The runtime tier order shrank from 5 tiers to 4 (see "Tiering rule" above).
- Fix 3 (video transcription) was originally Tier 4; it's now Tier 3.
- Nothing in Fix 2.A or Fix 1 needed to change — the OG fetcher already provides the content Fix 2.B was supposed to deliver, just via one HTTP GET that 2.B would have skipped.
- For IG/FB hot links where our backend's IP gets rate-limited, the optimization Fix 2.B *would* have provided is genuinely lost. If that becomes a problem in practice, the fallback is a per-domain cache or the OpenAI Whisper API path noted in Fix 3 (no IP-side requests).

### Alternative we considered and rejected

Repurposing 2.B to capture user-typed commentary alongside the URL (today we throw `msg.text` away and send `text=""`). Real value, but small: it only fires when the user types a comment, and Phase 5's quiz layer will look at consumption signals not annotation signals. Tracked as a possible Phase 5+ "annotations" feature; not blocking Phase 2.5.

---

## Fix 3 — Local video transcription (yt-dlp + whisper.cpp) ✅ Shipped

### What

For IG reels, FB videos, TikTok, YouTube shorts — download the audio stream with yt-dlp, run `whisper.cpp` `small.en` locally, attach the transcript to `ProcessedContent`. On video URLs the orchestrator runs **both** OG fetch and transcription and merges the outputs (per sign-off 2026-04-29) — the OG description / post caption labels the content, the transcript carries what was actually said. Both go to the LLM, labelled.

### Sign-off decisions (2026-04-29)

| Decision | Locked at |
|---|---|
| Tier interaction | **Run BOTH OG and Whisper on video URLs and merge.** Caption becomes context, transcript becomes the dominant `clean_text`. Sidecar records both layers in `tiers_used`. |
| Whisper model | `small.en` — 244 MB, ~5s per 30s reel on M-series CPU. |
| Guards | **Single guard:** `video_max_duration_seconds = 600` (10 min). Anything longer → `TranscriptionSkipped(reason="video_too_long")`, sidecar still gets a row, OG content alone goes to enrichment. No file-size cap, no concurrency cap (deliberate — keep it simple, can add if a single bad URL ever causes trouble). |
| Install path | `brew install whisper-cpp` → binary at `/opt/homebrew/bin/whisper-cli`. Model downloaded once via `scripts/setup_whisper.sh` into `data/models/ggml-small.en.bin`. Both gitignored. |

### Where it runs

Same place as Fix 2.A — inside the `enqueue_enrichment` BackgroundTask, before `enrich()`. Hydration orchestrator branches:

```
hydrate_processed():
  if needs_hydration:
    og_meta      = await fetch_og(...)              # always tries
    if is_video_url(url, platform):
      transcript = await transcribe_video(...)      # only on video URLs
    merge → clean_text = "POST CAPTION ... TRANSCRIPT ..."
    persist hydration row → data/hydrations.jsonl
  return hydrated processed
```

Important: a video transcription failure (yt-dlp can't extract, login required, region-blocked) returns `None` and the orchestrator falls back to OG-only — the capture isn't lost. A `TranscriptionSkipped` (e.g., too long) records the skip reason in the sidecar but still uses OG content if available.

### Files shipped

| Path | Role |
|---|---|
| `backend/capture/video_transcriber.py` (new) | `is_video_url(url, platform)` regex + platform-tag matcher. `transcribe_video(url)` orchestrates: yt-dlp metadata probe → duration check → audio download → `whisper-cli` subprocess → cleanup. Returns `TranscriptionResult` / `TranscriptionSkipped` / `None`. yt-dlp imported lazily so a missing dep doesn't break the import graph. |
| `backend/capture/hydration.py` (rewritten) | Now runs OG and transcription in parallel-aware order. Merges outputs: builds `clean_text` with `--- POST CAPTION ---` and `--- TRANSCRIPT ---` separators. Picks the better title from OG → yt-dlp → existing. New sidecar schema with `tier` (dominant source) + `tiers_used` (every layer that contributed) + nested `og` and `transcript` blocks. |
| `backend/config.py` | Added `video_transcribe_enabled` (kill-switch), `video_max_duration_seconds` (600), `whisper_model_path`, `whisper_binary_path`, `video_temp_dir`. |
| `requirements.txt` | Added `yt-dlp`. |
| `.gitignore` | Added `data/models/`, `bin/`, plus belated `data/hydrations.jsonl`. |
| `scripts/setup_whisper.sh` (new) | Idempotent setup: `brew install whisper-cpp`, downloads `ggml-small.en.bin` from Hugging Face, sanity-checks both. Tells user to set `WHISPER_BINARY_PATH` in `.env` if their homebrew prefix isn't `/opt/homebrew`. |
| `tests/test_video_transcriber.py` (new) | URL pattern matching (positive + negative); kill-switch + missing-dep paths; too-long short-circuit; download-fails-returns-None; full happy path with mocked subprocesses. |
| `tests/test_enrichment.py` | New `TestVideoTranscriptMerge` class: 6 cases covering merge with both layers, transcribe-disabled fallback, too-long fallback, OG-empty transcript-only, non-video URL doesn't call transcriber, transcriber exception fallback. Existing `TestHydrationHook` test updated for new sidecar schema. |

### What `data/hydrations.jsonl` rows look like now

For an IG reel where both layers fired:

```json
{
  "timestamp": "...",
  "capture_id": "uuid",
  "url": "https://www.instagram.com/reel/...",
  "tier": "video_transcript",
  "tiers_used": ["og_metadata", "video_transcript"],
  "title_before": "Telegram link",
  "title_after": "Reel by @cookingnerd",
  "title_replaced": true,
  "clean_text_before_chars": 0,
  "clean_text_after_chars": 1247,
  "og": {
    "source": "og",
    "image_url": "https://cdn.instagram.com/x.jpg",
    "site_name": "Instagram",
    "description_chars": 124
  },
  "transcript": {
    "duration_seconds": 58.2,
    "extractor": "Instagram",
    "title": "Reel by @cookingnerd",
    "chars": 1080
  }
}
```

For a transcription that was skipped (too long):

```json
{
  "tier": "og_metadata",
  "tiers_used": ["og_metadata"],
  "og": { ... },
  "transcript_skipped": {
    "reason": "video_too_long",
    "duration_seconds": 3600.0
  }
}
```

### One-time setup

```bash
cd ~/Desktop/LLM/BrainTwin
source venv/bin/activate
pip install -r requirements.txt        # picks up yt-dlp + selectolax
bash scripts/setup_whisper.sh          # brew install + download model
```

### Cost

| Resource | Cost |
|---|---|
| Disk one-time | ~250 MB model + ~5 MB binary |
| RAM per transcription | ~1 GB (released after) |
| CPU per 30s reel on M-series Mac | ~5 seconds |
| Maintenance | `pip install -U yt-dlp` every 2-4 weeks when IG/FB shift their endpoints |

Hard cost: **$0/month**. If volume ever explodes (>100 reels/day) or the Mac is offline, swap in OpenAI Whisper API ($0.006/min, ~$3/mo at 20 reels/day) — drop-in 5-line change in `_run_whisper`.

### Exit criteria — verified 2026-04-30

- ✅ `bash scripts/setup_whisper.sh` completes; binary + model in place.
- ✅ `pip install -r requirements.txt` adds yt-dlp.
- ✅ `pytest tests/test_video_transcriber.py tests/test_enrichment.py -v` all green (86 cases).
- ✅ Real IG reel forward → `data/hydrations.jsonl` row with `tier: "video_transcript"`, `tiers_used: ["og_metadata", "video_transcript"]`, non-zero `transcript.chars`.
- ✅ Enrichment row references transcript-only content.

### Smoke-test learnings (worth preserving for future-us)

Two real-world gotchas surfaced during the live test that the design didn't anticipate:

1. **whisper.cpp can't decode m4a / webm / opus.** It silently exits with returncode 0, processes zero audio frames, writes no transcript file, and gives no error. Symptom in the log: `whisper-cli succeeded but produced no readable transcript` plus a stderr tail showing `decode time = 0.00 ms / 1 runs` and a sub-second total time. Fix shipped in `_run_whisper`: pre-convert any non-WAV input to 16 kHz mono signed-16-bit PCM via ffmpeg before invoking whisper-cli.
2. **ffmpeg is not always pulled in by `brew install whisper-cpp`.** Some Homebrew formula versions list it as a dep, some don't. The `setup_whisper.sh` script now explicitly checks and installs `ffmpeg` if missing.

Also surfaced: whisper.cpp's `-of <prefix>` output naming varies across versions (`<prefix>.txt` vs `<prefix>.en.txt` vs sometimes nothing — printing transcript to stdout instead). `_run_whisper` now tries the expected location, falls back to globbing the temp dir for any `.txt`, and finally falls back to reading whisper's stdout. Diagnostic warning lists the actual temp-dir contents and a stderr tail when all three miss, so future debugging starts with real data not guesses.

---

## Fix 4 — End-to-end verification ✅ Shipped (2026-04-30)

### What

Replay the 4 original failure URLs (and any similar URL-only captures that landed in `enrichment_skipped` since Fix 1 shipped) through the now-fixed pipeline. Confirm each produces a real enrichment row with non-trivial `summary` + `key_facts` + `topics`. Document the new tiered hydration model in `docs/phase2-smoke-test.md` as **Pass 8** so future debugging starts from a known-good walkthrough.

### Sign-off decisions (2026-04-30)

| Decision | Locked at |
|---|---|
| Replay scope | **Both `enrichment` and `enrichment_skipped` rows.** Skip `capture`-phase rows — those are capture-side failures replay can't fix. |
| Idempotency | **Skip silently when URL already has a successful enrichment row** (joined via `capture_id`). Safe to re-run any number of times. No `--force` flag in this commit; add later if you ever need to re-enrich for prompt experiments. |
| Send mechanism | **POST to `/capture` with the bot's payload shape** (`text=""`, `images=[]`). Backend treats it identically to a real bot capture, so the full hydration tier model runs end-to-end. Same code path = same behavior. |

### Files shipped

| Path | Role |
|---|---|
| `scripts/replay_failed_urls.py` (new) | CLI replay tool. Reads `data/capture_failures.jsonl`, filters to replayable phases, dedupes by URL, joins against `captures.jsonl ⨝ enrichments.jsonl` to skip already-enriched URLs, POSTs each survivor to `/capture`. Throttled (default 1s between posts). Flags: `--dry-run`, `--limit N`, `--phase {enrichment,enrichment_skipped}`, `--backend-url`, `--throttle-ms`, `--verbose`. Exit codes: 0 = at least one replay succeeded, 1 = nothing to do, 2 = backend unreachable. |
| `tests/test_replay_failed_urls.py` (new) | Tests for the pure-logic functions: load skips non-URL/malformed rows; URL-dedupe keeps first occurrence; phase filter respects defaults vs explicit override; already-enriched skip uses `capture_id → URL` join; bot-style payload shape matches what `handlers.py:handle_text` sends. |
| `docs/phase2-smoke-test.md` | Appended **Pass 8 — capture hydration** with sub-passes for setup, unit tests, single-reel forward, article URL, YouTube URL, and replay. Updated closing line from "all 7 passes" → "all 8 passes" and added the Phase-2.5 status flip step. |

### Exit criteria

- ✅ `python -m scripts.replay_failed_urls --dry-run` lists every URL that survived dedupe + already-enriched filtering, with original phase/reason annotations.
- ✅ `python -m scripts.replay_failed_urls` POSTs each through `/capture`, summary line shows `N ok, 0 failed, M skipped (already enriched)`.
- ✅ Re-running the same command yields `0 ok, 0 failed, M+N skipped` — proves idempotency.
- ✅ Pass 8 in `phase2-smoke-test.md` documents the full hydration walkthrough including the IG-reel happy path, OG-only article path, YouTube short-circuit, and the common failure modes from Fix 3 smoke-testing (whisper / ffmpeg / yt-dlp).
- ✅ This doc's status flipped from "DESIGN SIGNED OFF, NOT YET BUILT" to "PHASE 2.5 LIVE" (top of file).

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
