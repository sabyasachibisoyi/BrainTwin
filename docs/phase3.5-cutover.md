# Phase 3.5 — JSONL Cutover

> **Status as of 2026-06-02 — PHASE 3.5 BUILT.**
>
> The three knowledge JSONLs that mirrored SQL during the Phase 3
> dual-write window — `data/captures.jsonl`, `data/enrichments.jsonl`,
> `data/hydrations.jsonl` — were retired. SQL (`data/braintwin.db`)
> plus ChromaDB (`data/chroma/`) are the sole authoritative stores
> for captures, enrichments, hydrations, chunks, topics, and entities.
>
> The `storage_dual_write` setting was deleted with the cutover. There
> is no longer an "off" state; the operator-controllable disablement
> was a dual-write-era affordance.

This document records the two open decisions surfaced at the start of
the cutover and what changed in the code as a result. The Phase 3
design notes (docs/phase3-design.md, section B.1) flagged this work
but didn't pre-commit to either decision because the right answer
depended on what we'd actually shipped by the time the dual-write
window closed.

## Decisions

### Decision 1 — Where does processed capture content live after the cutover?

**Resolution: extend `captures` with text columns (Option A).**

The Phase 3 `captures` table held only metadata (`url`, `title`,
`platform`, `dwell_seconds`, `raw_metadata_json`). The full processed
content — `clean_text`, `transcript`, `image_text`, image descriptions
— lived only in `captures.jsonl`. Chunks store paragraph-sized slices
of the text, but only get written on enrichment success, so anything
that crashed in flight before enrichment completed would have been
unrecoverable after the JSONL writer went away.

Three options were considered:

(A) Add explicit columns for the content fields on `captures`. Schema
    change, but clean: the captures row becomes the new authoritative
    store, recovery is a SQL `LEFT JOIN`, and future agent queries
    can run cheap `ILIKE` against the text columns.

(B) One `processed_payload_json TEXT` column on `captures`. Simpler
    migration (one column), more flexible, slightly worse for
    ad-hoc SQL queries on text.

(C) Drop crash recovery for in-flight captures. Defensible because
    crashes are rare, but it's a real regression and the operator
    has no way to find the lost work.

We chose (A). The new columns are nullable so historical rows that
pre-date the migration stay valid:

```
captures.clean_text             TEXT NULL
captures.transcript             TEXT NULL
captures.image_text             TEXT NULL
captures.image_descriptions_json TEXT NULL    -- JSON array of ImageDescription dicts
captures.text_source            TEXT NULL    -- "extension" | "youtube_transcript" | "fallback"
```

### Decision 2 — Does `capture_failures.jsonl` go away too?

**Resolution: keep it as an append-only ops log.**

`capture_failures.jsonl` is fundamentally different from the other
three JSONLs. It's an operational record (when, where, why a capture
or enrichment failed) — not part of the knowledge graph. It doesn't
participate in the dual-write seam (no `sync_failure` exists; nothing
mirrors it into SQL).

We considered adding a `capture_failures` table and folding it into
the cutover, but the failures log is small, easy to grep, append-only,
and the operator workflow around it (`/failures` endpoint, the bot's
`/failures` command, `grep` for debugging) is well-served by JSONL.
Promoting it to a table would also introduce a new dependency for
the enrichment skip path, which we'd rather not.

If `capture_failures.jsonl` ever grows uncomfortably large, the answer
is rotation, not a SQL table.

## What changed in code

### Schema migration

- **`backend/storage/schema.py`** — added five nullable columns to
  the `captures` table (see Decision 1).
- **`backend/storage/models.py`** — `Capture` dataclass gained the
  matching fields, all defaulting to `None`.
- **`backend/storage/db.py`** — `init_db()` now runs a narrow
  `ALTER TABLE … ADD COLUMN IF NOT EXISTS` sweep after `create_all`,
  driven by an explicit `_PENDING_COLUMN_ADDS` list. SQLAlchemy's
  `metadata.create_all` is CREATE-IF-NOT-EXISTS only and will not
  add new columns to an existing table; this sweep closes the gap
  without bringing in alembic for one column set. Both SQLite and
  Postgres support the `ALTER TABLE` form used. When the next
  migration lands, append to the list; when the list gets long
  enough to be uncomfortable, that's the trigger for alembic.
- **`backend/storage/repositories/capture_repo.py`** — `_row_to_capture`,
  `CaptureRepository.create`, and the new `unenriched` /
  `latest_captured_at` / `platform_counts` methods all read and write
  the new fields.

### Writers retired

- **`backend/main.py`** — the `CAPTURES_LOG` writer was removed.
  `/capture` now persists via a single `sync_capture` call that
  carries all the content fields, and surfaces `persisted=True/False`
  in the response. A failed persist routes to `capture_failures.jsonl`
  with a `phase=capture` row.
- **`backend/knowledge/enrichment_worker.py`** — `_persist_enrichment`
  and `_persist_hydration` (the two functions that wrote
  `enrichments.jsonl` and `hydrations.jsonl`) were removed. The
  worker now calls `sync_enrichment` / `sync_hydration` directly.
  Failures and skips still write to `capture_failures.jsonl`.
- **`backend/storage/sync.py`** — `storage_dual_write` short-circuits
  removed from `sync_capture`, `sync_hydration`, and `sync_enrichment`.
  Module docstring updated to reflect that this is the sole
  persistence path, not a side-channel.

### Read paths re-pointed at SQL

- **`backend/main.py /stats`** — now reads from SQL only via three new
  repository methods: `CaptureRepository.count_by_user` /
  `platform_counts` / `latest_captured_at`,
  `EnrichmentRepository.count_enriched_captures_by_user`, and
  `EntityRepository.count_capture_mentions_by_user`. The skipped-set
  is still read from `capture_failures.jsonl` (see Decision 2).
- **`backend/main.py /failures`** — unchanged. Reads
  `capture_failures.jsonl` directly, same shape as before.
- **`backend/main.py _recover_unenriched`** — replaced the JSONL scan
  with a SQL query (`captures LEFT JOIN enrichments`) via
  `iter_unenriched_captures`. The failures log is still consulted
  to exclude `enrichment_skipped` rows.
- **`backend/knowledge/enrichment_worker.py`** — the JSONL-scanning
  `find_unenriched_capture_ids` and JSONL-row-fed
  `hydrate_processed(row: dict)` were replaced by the
  SQL-fed `iter_unenriched_captures()` async iterator and
  `hydrate_processed_from_capture(capture: Capture)` helper.

### Scripts

- **`scripts/retry_failed_enrichments.py`** — rewritten to walk
  `iter_unenriched_captures()` instead of JSONL files. The
  `--captures` / `--enrichments` CLI flags were dropped; the new
  flags are `--user-id` and `--failures`.
- **`scripts/backfill_enrichment.py`** — rewritten to walk
  `iter_unenriched_captures()` and apply a new SQL-row-based
  `is_test_capture(Capture)` classifier (replacing the JSONL-dict
  `is_test_row(dict)`). The legacy capture_id minting branch was
  dropped — every row in SQL already has a stable `id`.
- **`scripts/migrate_jsonl_to_sql.py`** — kept as a frozen historical
  tool for backfilling archived pre-cutover JSONLs. The
  `storage_dual_write` abort check was removed (it would always
  pass post-cutover). The script's local-only `hydrate_processed`
  JSONL-row helper was moved into the script.
- **`scripts/mock_capture.py` / `scripts/mock_phase2_capture.py`** —
  updated to poll `/stats` instead of tailing the JSONLs.

### Tests

- **`tests/test_enrichment.py`** — the `tmp_jsonl` fixture was
  replaced with `worker_writes`, which spies on `sync_enrichment`
  and `sync_hydration` so the existing control-flow tests can
  assert what the worker tried to persist without standing up SQL.
  Failure-side assertions still read `capture_failures.jsonl`
  because that log survived the cutover. The
  `TestEnqueueEnrichmentDualWrite` end-to-end test was renamed to
  `TestEnqueueEnrichmentEndToEnd` and stopped flipping the
  retired dual-write flag.
- **`tests/test_main_wiring.py`** — the `TestDualWriteOffStartup`
  class and the `dual_write_on` fixture were removed. The init /
  user-seed try-block-independence tests still cover the startup
  hook.
- **`tests/test_migrate_jsonl_to_sql.py`** — the two tests that
  asserted the migration script bailed when
  `storage_dual_write=False` were removed. The script no longer
  consults the flag.
- The TestFindUnenriched class (which tested the JSONL-scanning
  helper) was replaced by `TestIterUnenrichedCaptures`, exercising
  the SQL-backed equivalent against an in-memory SQLite. The
  TestIsTestRow class became `TestIsTestCapture`, exercising the
  SQL-row-fed classifier.

## What stayed the same

- `capture_failures.jsonl` (and `migration_failures.jsonl`) keep
  their existing on-disk shape and the `/failures` endpoint.
- The Chrome extension and Telegram bot payloads — they post the
  same JSON they always did. The change is entirely server-side.
- The 9-table SQL schema and the 3 Chroma collections — chunks,
  topics, entities — were already in place from Phase 3.

## Operational guidance after the cutover

- The new `/capture` response carries `persisted: bool` instead of
  the old `sql_synced: bool`. The Chrome extension and Telegram bot
  can treat `persisted=false` as a hard failure (capture did not
  land in SQL) and retry; during the dual-write window
  `sql_synced=false` was non-fatal because the JSONL still held the
  row.
- A stale `STORAGE_DUAL_WRITE=false` in someone's `.env` is silently
  ignored — pydantic-settings drops unknown env vars by default.
- The historical JSONLs under `data/` are not consulted by the
  running backend. They can be archived offline or replayed via
  `scripts/migrate_jsonl_to_sql.py --dry-run` then a real run if any
  of them still hold rows that never made it through the dual-write
  window.

## What comes next

Phase 4 — the agent layer — can begin in earnest. The retrieval +
synthesis quizzes and the indirect-clue inference game are now able
to assume SQL as the single source of truth for the corpus, with no
two-store reconciliation logic needed. The empty `backend/agent/`
package is where that work lands.
