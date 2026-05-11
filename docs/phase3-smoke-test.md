# Phase 3 Smoke Test — Run + Verify Locally

Phase 3 adds the SQL + Vector storage layer in dual-write mode. This guide walks through proving it works on your Mac, in numbered passes, so when something breaks you know exactly which seam.

What we're proving:

```
/capture → captures.jsonl  AND  SQL captures + Chroma (chunks/topics/entities)
enrichment_worker → enrichments.jsonl + hydrations.jsonl  AND  SQL enrichments + chunks + topics + entities
historical JSONLs → SQL + Chroma  (via scripts/migrate_jsonl_to_sql.py)
```

For the locked design decisions and the build-step log, see [phase3-design.md](phase3-design.md).

---

## Pass 0 — Confirm the pieces exist

```bash
cd ~/Desktop/LLM/BrainTwin
source venv/bin/activate

# Phase 3 storage code present?
ls backend/storage/db.py \
   backend/storage/schema.py \
   backend/storage/models.py \
   backend/storage/repositories/ \
   backend/storage/embedder.py \
   backend/storage/vector_store.py \
   backend/storage/chunking.py \
   backend/storage/sync.py

# Phase 3 scripts?
ls scripts/migrate_jsonl_to_sql.py \
   scripts/inspect_storage.py \
   scripts/run_tests.py

# Phase 3 tests?
ls tests/test_storage_schema.py \
   tests/test_storage_repos.py \
   tests/test_embedder.py \
   tests/test_vector_store.py \
   tests/test_chunking.py \
   tests/test_storage_sync.py \
   tests/test_main_wiring.py \
   tests/test_migrate_jsonl_to_sql.py

# Config flag turned on?
python -c "from backend.config import settings; print('dual_write =', settings.storage_dual_write)"
```

All `ls` calls should succeed, and `dual_write = True` is the default. To turn dual-write off temporarily (e.g. while debugging a SQL issue), set `STORAGE_DUAL_WRITE=false` in `.env` and restart.

---

## Pass 1 — Unit tests (offline, no API calls, no real models)

The full suite uses an in-memory SQLite (pinned via `tests/conftest.py`) and a tmp-dir Chroma per test. No network, no Haiku, no sentence-transformers downloads.

```bash
cd ~/Desktop/LLM/BrainTwin
source venv/bin/activate
python scripts/run_tests.py
```

What should pass:

| Test file | What it proves |
|---|---|
| `test_storage_schema.py` | The 9 tables exist with the right columns, FKs, and constraints. |
| `test_storage_repos.py` | Every repository CRUD method works, including tenant-scoped reads. |
| `test_embedder.py` | The lazy `sentence-transformers` wrapper loads and embeds (stubbed). |
| `test_vector_store.py` | ChromaVectorStore CRUD + per-collection isolation. |
| `test_chunking.py` | Paragraph / token-window / chapter-aware split correctly; overlap honored. |
| `test_storage_sync.py` | `sync_capture` / `sync_hydration` / `sync_enrichment` round-trip; idempotent on duplicate; best-effort (never raises on SQL/Chroma hiccup). |
| `test_main_wiring.py` | `/capture` POST writes to SQL alongside the JSONL; startup hook seeds user_id=1. |
| `test_migrate_jsonl_to_sql.py` | Migration script's 13 orchestration cases (test-row skip, idempotency on rerun, bad-JSON continue, orphan-parent rejection, etc.). |

You should also see Phase 1 + Phase 2 tests pass (`test_capture.py`, `test_enrichment.py`, `test_og_fetcher.py`, `test_replay_failed_urls.py`). Red unit tests are cheaper to debug than red integration tests — fix them first before moving on.

**Filtering during dev:**

```bash
python scripts/run_tests.py -k storage   # only storage tests
python scripts/run_tests.py -x           # stop on first failure
python scripts/run_tests.py -s           # show prints / log output
```

---

## Pass 2 — Inspect the live stores (read-only)

`scripts/inspect_storage.py` is your "Chroma GUI" and SQL spot-checker. It opens the same `PersistentClient` and SQLAlchemy engine the live code uses, then dumps row counts and samples.

```bash
python scripts/inspect_storage.py
```

You should see three sections: **SQL row counts** (whole DB + Sabya-scoped), **most recent captures + enrichments**, and **Chroma collections** (`chunks`, `topics`, `entities`) with counts + a sample of each.

If you've never run the backend or the migration, expect counts to be zero — that's correct on a fresh checkout.

**Drilling into one capture across both stores:**

```bash
python scripts/inspect_storage.py --capture-id <uuid>
```

This is the workhorse command for debugging Phase 4 retrieval later: it shows you the SQL row, the hydrations, the enrichment, every chunk + source_kind, and the matching Chroma vectors. If a capture is "missing" from one store, this is how you spot it.

---

## Pass 3 — Backfill the historical JSONLs into SQL + Chroma

`scripts/migrate_jsonl_to_sql.py` walks `data/captures.jsonl` → `data/hydrations.jsonl` → `data/enrichments.jsonl` and lands every row in SQL + Chroma via the same `sync_*` functions the live dual-write path uses. Idempotent — re-running picks up where the last run left off without duplicating data.

**Step A — dry run.** Validate every JSONL row, count what would land in SQL, write nothing.

```bash
python scripts/migrate_jsonl_to_sql.py --dry-run
```

Look for:

- `Stage 1 counts: {'seen': N, 'test_skipped': X, 'already_in_sql': 0, 'minted_ids': Y, 'inserted': Z, 'failed': 0, 'bad_json': 0}` — three stage summaries, one per JSONL file.
- `minted_ids` > 0 means some pre-Phase-2 capture rows lacked a `capture_id` and got a deterministic uuid5 derived from `(url, timestamp)`. Re-runs are idempotent because uuid5 is deterministic.
- `failed` and `bad_json` both 0 on dry-run = clean inputs.

**Step B — real run.**

```bash
python scripts/migrate_jsonl_to_sql.py
```

This actually inserts. For each row it streams through to keep memory bounded; each `sync_*` call is its own transaction so a crash leaves at most one partial row (the next run resumes from there).

Any row-level validation failures (bad JSON, orphan hydration parents, sync_* returning False) go to `data/migration_failures.jsonl` with `{source_file, line_number, raw_row, error_reason}` — review and re-run.

**Step C — verify.**

```bash
python scripts/migrate_jsonl_to_sql.py --verify
```

Prints SQL row counts vs JSONL line counts (kept-after-test-skip) and spot-checks 5 random capture_ids' chunks. Exits non-zero on mismatch.

You should see:

```
captures   : JSONL=N (kept after test-skip=M) vs SQL=M
hydrations : JSONL=H vs SQL=H
enrichments: JSONL=E vs SQL=E
chunks (derived)            : SQL=K     # K ≈ E × 2-5 depending on content length
```

If `SQL captures < kept JSONL captures`, look at `data/migration_failures.jsonl` for the gap. Common causes: a JSONL row's `capture_id` is bound to another user_id (FK collides), or the row's `metadata` field is malformed JSON.

**Useful flags during testing:**

```bash
python scripts/migrate_jsonl_to_sql.py --limit 5            # cap inserts per stage
python scripts/migrate_jsonl_to_sql.py --include-test-rows  # also migrate test fixtures
```

---

## Pass 4 — End-to-end with a live capture (dual-write smoke test)

Proves: a fresh `/capture` POST lands in JSONL **and** SQL **and** Chroma; subsequent enrichment lands in JSONL **and** SQL (enrichments + chunks + topics + entities) **and** Chroma vectors.

**Terminal A — backend:**

```bash
cd ~/Desktop/LLM/BrainTwin
source venv/bin/activate
uvicorn backend.main:app --reload --port 8000
```

Watch the startup log. You want:

```
INFO ... Seeded default user_id=1 (Sabya)        # first run only
INFO ... Application startup complete.
```

If you see:

```
WARNING ... Phase 3 SQL schema init failed ...
```

…fix the SQL setup before continuing. The JSONL path still works, but dual-write to SQL is effectively off this run.

**Terminal B — fire a capture:**

```bash
python scripts/mock_phase2_capture.py        # POST + poll for enrichment
```

When the poll finishes, grab the new `capture_id` from the response and drill into it:

```bash
CID=$(tail -1 data/captures.jsonl | python -c "import json,sys; print(json.loads(sys.stdin.read())['capture_id'])")
echo "capture_id = $CID"
python scripts/inspect_storage.py --capture-id "$CID"
```

Expected output:

- SQL: `captures` row present (`platform`, `captured_at`, etc.).
- SQL: `enrichments` row with non-empty `summary`.
- SQL: `chunks` count ≥ 1 (at least one summary chunk; more if `clean_text` / `transcript` non-empty).
- Chroma: same chunk count in the `chunks` collection, each tagged with `source_kind` + `user_id`.

**What to check in the `/capture` response itself:**

```bash
curl -s -X POST http://127.0.0.1:8000/capture \
     -H 'Content-Type: application/json' \
     -d '{"url":"https://en.wikipedia.org/wiki/Knowledge_graph","title":"KG","platform":"general","content_type":"article","text":"A knowledge graph is …","timestamp":"2026-05-11T00:00:00Z","dwell_time_seconds":40,"metadata":{}}' \
   | python -m json.tool
```

The response includes a `sql_synced` boolean. It's `true` on a successful dual-write, `false` on either a duplicate or a SQL error (best-effort — the JSONL path is unaffected). If you're seeing `sql_synced: false` for fresh captures, check the uvicorn log for the swallowed warning.

---

## Pass 5 — Failure modes you'll want to recognize

These are the seams to know about so you can debug fast.

### A — SQL schema init fails on startup

Symptom in log: `Phase 3 SQL schema init failed (dual-write to SQL effectively disabled this run): ...`

Cause: usually a stale `data/braintwin.db` from a schema mid-change, or a file permission issue. Fix:

```bash
rm data/braintwin.db          # drop the DB; init_db will recreate
uvicorn backend.main:app --reload --port 8000
```

This is safe in Phase 3 dual-write mode — the JSONLs are still the source of truth, and `scripts/migrate_jsonl_to_sql.py` can rebuild SQL from them at any time.

### B — Default user seed fails

Symptom in log: `Phase 3 default-user seed failed (sync_capture writes will FK-violate on user_id=1): ...`

Cause: a previous run inserted user_id=1 with a different email, or the users table got corrupted. Fix the row directly or drop the DB as above.

### C — `sql_synced: false` on every capture

Cause 1: `storage_dual_write` is off. Check `python -c "from backend.config import settings; print(settings.storage_dual_write)"`.

Cause 2: the capture's `capture_id` is colliding with one already in SQL under a different user_id. Look for `sync_capture failed for <cid>` in the uvicorn log — the warning carries the actual SQL error.

### D — Chunk count in SQL ≠ Chunk count in Chroma

This means the dual-write got partway through `_sync_chunks_and_vectors`: SQL chunks landed but the Chroma upsert failed. Detect with:

```bash
python scripts/inspect_storage.py --capture-id <cid>
# compare "chunks N" (SQL) vs the "chunks collection: M vector(s)" line
```

Repair: re-run `python scripts/migrate_jsonl_to_sql.py`. The migration's "chunks already exist for this capture → skip" guard means it won't touch SQL, but it also (intentionally) won't repair Chroma in v1. A targeted Chroma-only repair script is a Phase 3.5 follow-up if this proves to happen in practice.

### E — Re-running migrate creates fresh `minted_ids` every time

Cause: a row in `captures.jsonl` has no `capture_id` AND no `url` AND no `timestamp` — the script can't derive a stable uuid5 and falls back to uuid4. Fix the row by adding either `url` or `timestamp`, then re-run.

### F — Tests pass locally but `pytest -k something` finds nothing

The runner script auto-prepends `tests/` and `-v` if you don't pass either:

```bash
python scripts/run_tests.py -k migration     # equivalent to: pytest tests/ -v -k migration
```

If you need to point at one file:

```bash
python scripts/run_tests.py tests/test_storage_sync.py -v
```

---

## What "Phase 3 verified" looks like

You're ready to open the two-week dual-write window when:

- [ ] `python scripts/run_tests.py` passes green.
- [ ] `python scripts/migrate_jsonl_to_sql.py --dry-run` reports zero `bad_json` and zero `failed` across all three stages.
- [ ] `python scripts/migrate_jsonl_to_sql.py` then `--verify` exits zero.
- [ ] `python scripts/mock_phase2_capture.py` lands a new capture with `sql_synced: true` in the response.
- [ ] `python scripts/inspect_storage.py --capture-id <new-cid>` shows the capture, enrichment, and chunks in both SQL and Chroma.

Once all five tick, just keep capturing as usual. Periodically run `inspect_storage.py` to spot-check vocabulary growth and chunk counts. After ~2 weeks of stable dual-write, Phase 3.5 cuts the JSONL writers and SQL + Chroma become sole path.
