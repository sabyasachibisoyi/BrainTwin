# Phase 2 Smoke Test — Enrichment End-to-End

Phase 2 adds an LLM enrichment step to the capture pipeline. This guide walks through proving it works on your laptop, in numbered passes, so when something breaks you know exactly which seam.

What we're proving: **`/capture` → `data/captures.jsonl` (with `capture_id`) → FastAPI BackgroundTasks → enrichment_worker → Claude Haiku → `data/enrichments.jsonl`**.

For the architecture diagram and locked design decisions, see [docs/phase2-design.md](phase2-design.md).

---

## Pass 0 — Confirm the pieces exist

```bash
cd ~/Desktop/LLM/BrainTwin
source venv/bin/activate

# Phase 2 code present?
ls backend/knowledge/llm_client.py \
   backend/knowledge/prompts.py \
   backend/knowledge/enrichment.py \
   backend/knowledge/enrichment_worker.py

# Smoke / backfill / retry scripts?
ls scripts/mock_phase2_capture.py \
   scripts/backfill_enrichment.py \
   scripts/retry_failed_enrichments.py

# Tests?
ls tests/test_enrichment.py

# .env has the key?
grep -q '^ANTHROPIC_API_KEY=sk-' .env && echo "OK: API key set" || echo "MISSING: ANTHROPIC_API_KEY in .env"
```

All `ls` calls should succeed and the grep should print `OK: API key set`. If the key is missing, see Pass 1 below — the backend will still run but enrichment is silently disabled.

---

## Pass 1 — Unit tests (offline, no API calls)

```bash
cd ~/Desktop/LLM/BrainTwin
source venv/bin/activate
pip install -q pytest
pytest tests/test_enrichment.py -v
```

Expected: every test under `TestValidate`, `TestEnrich`, `TestWrapEnrichmentRecord`, `TestEnqueueEnrichment`, `TestFindUnenriched`, and `TestIsTestRow` passes (about 25 tests). These stub the Anthropic SDK so no API calls happen and no tokens get burned.

If a test fails, fix that before continuing. Red unit tests are cheaper to debug than red integration tests.

---

## Pass 2 — Backend in isolation with a real Haiku call

Proves: server boots, LLMClient connects to the Anthropic API, enrichment runs end-to-end on one synthetic capture.

**Terminal A — start the backend:**

```bash
cd ~/Desktop/LLM/BrainTwin
source venv/bin/activate
uvicorn backend.main:app --reload --port 8000
```

You should see something like:

```
INFO:backend.main:Recovering 0 unenriched captures from previous runs
INFO:     Application startup complete.
```

If you instead see:

```
WARNING ... ANTHROPIC_API_KEY is empty — Phase 2 enrichment is DISABLED.
```

…stop and put a real `ANTHROPIC_API_KEY=sk-ant-...` in `.env`, then restart.

**Terminal B — fire the Phase 2 mock capture:**

```bash
cd ~/Desktop/LLM/BrainTwin
source venv/bin/activate
python scripts/mock_phase2_capture.py
```

The script POSTs a real-shaped payload (a Bengaluru rent article that exercises Decision D — the `jugaad` / `₹` / `HSR Layout` fidelity rules), then polls `data/enrichments.jsonl` for up to 30 seconds for the matching `capture_id`.

Expected (last lines):

```
[5/5] GET /stats (after)
  total_captures:    N → N+1  (delta +1)
  enriched captures: M → M+1  (delta +1)

  --- Enrichment block ---
  summary:    A short summary about HSR Layout rents…
  entities:   [{"name": "Bengaluru", "type": "place"}, …]
  key_facts:  ["1BHK rents in HSR Layout start at ~₹25,000", …]
  topics:     ["bengaluru", "rent-crisis", "real-estate", …]
  ✓ Phase 2 fidelity check — 'jugaad' and 'HSR' both preserved.

✓ Phase 2 enrichment path works.
```

**If the script times out** ("no enrichment row, no failure row"): check Terminal A's log — the worker will have logged either a transient error chain or an unexpected exception. Common causes:

- Bad / expired API key → permanent error in log, `phase: "enrichment"` row in `data/capture_failures.jsonl`.
- Corporate / VPN proxy blocking `api.anthropic.com` → connection errors looping through 4 retries before logging `transient_exhausted`.
- Backend started without the API key → log warning at startup; the script exits early with `enrichment_scheduled=False`.

You can also peek at the JSONLs directly:

```bash
tail -1 data/captures.jsonl    | python -m json.tool
tail -1 data/enrichments.jsonl | python -m json.tool
```

The two rows should share the same `capture_id`.

---

## Pass 3 — Crash recovery

Proves: if the backend dies between persisting a capture and finishing enrichment, the next startup re-queues the unenriched ones.

```bash
# Terminal A — backend running.
# Terminal B — fire 3 captures in quick succession:
for i in 1 2 3; do
  python scripts/mock_phase2_capture.py --timeout 1
done

# Most likely 1-3 of those time out (the 1s budget is too tight on
# purpose — we want them to be in flight when we kill the backend).

# Now hard-kill the backend in Terminal A (Ctrl+C twice).
# Restart it:
uvicorn backend.main:app --reload --port 8000
```

In the new startup log, look for:

```
INFO:backend.main:Recovering N unenriched captures from previous runs
INFO:backend.main:Re-queued N unenriched captures
```

Wait ~10s, then:

```bash
# All capture_ids in captures.jsonl should now appear in enrichments.jsonl
diff \
  <(jq -r '.capture_id' data/captures.jsonl    | sort) \
  <(jq -r '.capture_id' data/enrichments.jsonl | sort)
```

Empty diff = recovery worked. If diff shows missing IDs, scan `data/capture_failures.jsonl` for `phase: "enrichment"` rows — those are the ones the worker explicitly gave up on (and they will NOT be retried again at startup; you have to run `scripts/retry_failed_enrichments.py` to retry them).

---

## Pass 4 — Backfill existing captures

Proves: the backfill script enriches your existing Phase 1 capture log without re-spending on already-enriched rows.

```bash
# Dry run first — shows what it would do, costs $0
python scripts/backfill_enrichment.py --dry-run
```

Expected output reports:

- `Found N already-enriched capture_ids`
- `Candidates to enrich: K (already-enriched: N, test skipped: T, unhydratable: U, minted-ids-this-run: M)`

Then for real (capped to 5 to bound cost during the smoke test):

```bash
python scripts/backfill_enrichment.py --limit 5
```

Watch the log; each enriched row writes one line to `data/enrichments.jsonl`. Re-running with the same `--limit 5` should skip the same 5 rows (idempotent on `capture_id`) and pick up the next 5 — proving the test-row classifier and the enriched-id skip both work.

> **Heads-up about pre-Phase-2 captures.** Rows in `data/captures.jsonl` written *before* this Phase 2 deploy don't have a `capture_id` field. The backfill script mints a fresh UUID per row per run, so re-running on those will re-enrich them every time. To make backfill idempotent over old rows, do a one-shot migration: read captures.jsonl, add `capture_id: <uuid>` to each row, write it back. That migration isn't shipped in v1 — small enough to do by hand for the existing log.

---

## Pass 5 — Retry failed enrichments

Proves: the retry script picks up anything in `captures.jsonl` that has no matching row in `enrichments.jsonl`.

```bash
# Dry run — see what's missing
python scripts/retry_failed_enrichments.py --dry-run

# Actually retry
python scripts/retry_failed_enrichments.py --limit 10
```

This re-runs `enqueue_enrichment` directly (no FastAPI involved) for each unenriched `capture_id`, so it works whether the backend is up or down.

---

## Pass 6 — `/stats` and `/failures` reflect Phase 2

```bash
curl -s http://127.0.0.1:8000/stats | python -m json.tool
```

Should now include an `enrichments` block:

```json
{
  "total_captures": 12,
  "total_entities": 47,
  "platforms": { "general": 12 },
  "last_capture": "2026-04-27T19:14:05+00:00",
  "enrichments": { "total": 11, "pending": 1 }
}
```

```bash
curl -s 'http://127.0.0.1:8000/failures?limit=5' | python -m json.tool
```

Should include a `by_phase` breakdown:

```json
{
  "total": 4,
  "by_phase": { "capture": 1, "enrichment": 3 },
  "recent": [ ... ]
}
```

You can filter by phase:

```bash
curl -s 'http://127.0.0.1:8000/failures?phase=enrichment' | python -m json.tool
```

---

## Pass 7 — Telegram bot `/failures` includes Phase 2

If the Telegram bot is running (Phase 1 setup), open a chat with it and send `/failures`. The reply should now look like:

```
⚠️ 7 failures (4 capture, 3 enrichment) — last 7:
• 2026-04-27T... [chrome] processing: ...
• 2026-04-27T... [enrich] permanent: bad_request: ...
• ...
```

The `[enrich]` tag identifies enrichment-phase failures; `[chrome]` / `[telegram]` identify capture-phase failures (Decision C).

---

## Pass 8 — Capture hydration (Phase 2.5)

Goal: prove the tiered hydration model (OG metadata + local video transcription) actually closes the gap that surfaced after Pass 1 — IG/FB URL forwards landing in `capture_failures.jsonl` with `reason: "empty_content"`. Design + per-fix file lists are in [docs/phase2.5-capture-hydration.md](phase2.5-capture-hydration.md).

### Pass 8.0 — One-time setup

```bash
cd ~/Desktop/LLM/BrainTwin
source venv/bin/activate

# Phase 2.5 deps: selectolax (HTML parser) + yt-dlp (audio extractor)
pip install -r requirements.txt

# whisper.cpp + small.en model + ffmpeg (idempotent)
bash scripts/setup_whisper.sh
```

The setup script installs `whisper-cpp` and `ffmpeg` via Homebrew if not already present, downloads `ggml-small.en.bin` (~244 MB) into `data/models/`, and sanity-checks both. ffmpeg is mandatory — whisper.cpp can't decode m4a/webm/opus, so we pre-convert via ffmpeg.

### Pass 8.1 — Unit tests (offline)

```bash
pytest tests/test_og_fetcher.py tests/test_video_transcriber.py tests/test_replay_failed_urls.py -v 2>&1 | tail -20
# Re-run the existing enrichment suite too — Phase 2.5 added merge tests:
pytest tests/test_enrichment.py -v 2>&1 | tail -20
```

All-green expected on every suite. The video-transcriber tests use mocked subprocesses, so this works even before Pass 8.0 is finished.

### Pass 8.2 — Single fresh IG/FB reel forward (live, end-to-end)

Backend + bot both running. From your phone, open Instagram or Facebook, find any reel with spoken content, share to your BrainTwin bot.

```bash
# Terminal — watch the hydration sidecar in real time
tail -f data/hydrations.jsonl
```

Expected log lines (in the uvicorn terminal):

```
INFO  Processing capture: platform=instagram url=https://...
INFO  hydrate[xxxxxxxx] hydrated tier=video_transcript tiers_used=['og_metadata', 'video_transcript'] title_replaced=True clean_text_chars=1247
INFO  enrich[xxxxxxxx] enriched (N entities, N facts, N topics)
```

| Check | What you should see |
|---|---|
| `data/hydrations.jsonl` last row | `tier: "video_transcript"`, `tiers_used: ["og_metadata", "video_transcript"]`, `transcript.chars > 0`, `og.source` populated. |
| `data/enrichments.jsonl` last row | Non-empty `summary`, `key_facts`, `topics` that reference what was *said* in the reel — not just the post caption. |
| `/tmp/braintwin_yt_*` after the run | Empty / removed. The orchestrator cleans up its temp dir. |
| `/stats` | `enrichments.total` increased by 1, `enrichments.skipped` unchanged. |

### Pass 8.3 — Article URL falls through to OG only

Forward (or paste in browser → bookmarklet) any news article URL — something with proper `og:title` + `og:description`. The hydration row should be:

```
tier: "og_metadata"
tiers_used: ["og_metadata"]
og: { source: "og", description_chars: NN, ... }
# no transcript block — non-video URL, transcription tier didn't fire
```

### Pass 8.4 — YouTube URL still uses the existing transcript path

Forward a YouTube video URL. The Phase 1 YouTube transcript extractor runs first inside `processor.process()`, so `clean_text` is non-empty before hydration is even reached. You should see:

```
INFO  Processing capture: platform=youtube url=...
# NO hydrate[...] line — _needs_hydration() returned False because content already present
INFO  enrich[xxxxxxxx] enriched (...)
```

`data/hydrations.jsonl` gains no row for this capture. The Phase 1 path is preserved.

### Pass 8.5 — Replay the historical IG/FB failures

Now the four IG/FB URLs from the original Pass-1 bug (and any similar URL-only forwards that landed in `enrichment_skipped` since Fix 1 shipped) deserve a re-run through the now-fixed pipeline.

```bash
# Backend must be running.

# 1. Dry-run first — shows what would be replayed without POSTing.
python -m scripts.replay_failed_urls --dry-run

# 2. Real run — replays each unique URL through /capture in bot-style.
#    Throttled by default to be polite to the backend + Anthropic API.
python -m scripts.replay_failed_urls

# 3. Restrict to one phase if you want:
python -m scripts.replay_failed_urls --phase enrichment_skipped --limit 10
```

What it does:

- Reads `data/capture_failures.jsonl`, keeps rows tagged `enrichment` or `enrichment_skipped` (skips `capture` — those need different fixes).
- Dedupes by URL (first occurrence wins).
- Skips URLs whose original `capture_id` already has a successful row in `data/enrichments.jsonl` (idempotent — safe to re-run any number of times).
- POSTs each survivor to `/capture` with the bot's payload shape, so the full Phase 2.5 hydration pipeline runs end-to-end.

Expected summary line:

```
Replay summary: N ok, 0 failed, M skipped (already enriched).
```

Verification after the replay:

```bash
# How many IG/FB URLs from the original bug now have hydrations?
grep -c '"tier": "video_transcript"' data/hydrations.jsonl
grep -c '"tier": "og_metadata"' data/hydrations.jsonl

# /stats should show enrichments.total ticked up by N, skipped unchanged
curl -s http://127.0.0.1:8000/stats | python -m json.tool

# Spot-check that what was previously empty now has content
tail -1 data/enrichments.jsonl | python -m json.tool
```

### Common Pass-8 failure modes

| Symptom | Most likely cause | Fix |
|---|---|---|
| Hydration log shows `tier=og_metadata` for an IG reel (no transcript) | whisper-cli, ffmpeg, or the model file isn't where the backend expects | Run Pass 8.0 again. Check `which whisper-cli && which ffmpeg && ls -la data/models/`. If paths differ from defaults, set `WHISPER_BINARY_PATH` in `.env`. |
| Log says `whisper-cli succeeded but produced no readable transcript` with `decode time = 0.00 ms` | whisper.cpp got an encoded audio it can't decode (m4a/webm/opus) | ffmpeg conversion is missing. `brew install ffmpeg`, restart uvicorn. |
| Log says `yt-dlp probe failed for ...` | yt-dlp's IG/FB extractor broke (they ship breakage every few weeks) | `pip install -U yt-dlp` and re-forward. |
| `replay_failed_urls.py` reports "Backend not reachable" | Backend isn't running | Start uvicorn in another terminal. |
| `replay_failed_urls.py` reports "0 ok" with everything skipped | Every URL in failures.jsonl already has an enrichment row | Working as designed. Add `--force`-equivalent in a future ship if you need to re-enrich. |
| Reel forward produces a transcript but enrichment row's `summary` ignores it | LLM prompt doesn't surface transcript content well — separate concern from hydration | Inspect with `tail -1 data/enrichments.jsonl`; if recurring, tune `backend/knowledge/prompts.py`. |

---

## When all 8 passes are green

You're done with Phase 2 + Phase 2.5. Update [docs/phase2-design.md](phase2-design.md) status from "AWAITING SMOKE TEST" to "PHASE 2 LIVE" and [docs/phase2.5-capture-hydration.md](phase2.5-capture-hydration.md) status to "PHASE 2.5 LIVE", then move on to Phase 3 (storage layer — ChromaDB + SQLite).
