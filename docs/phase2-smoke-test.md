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

## When all 7 passes are green

You're done with Phase 2. Update [docs/phase2-design.md](phase2-design.md) status from "AWAITING SMOKE TEST" to "PHASE 2 LIVE" and move on to Phase 3 (storage layer — ChromaDB + SQLite).
