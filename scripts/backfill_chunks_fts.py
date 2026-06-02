"""Phase 4 M.1 — One-shot rebuild of the `chunks_fts` FTS5 index.

`chunks_fts` is an external-content FTS5 index pointing at the
`chunks` table (see `backend/storage/schema.py` and
`docs/phase4-vague-recall-design.md` V.1 for the why). On a brand-new
schema there's nothing to backfill — the AFTER INSERT trigger handles
new chunks as they're written. But:

  - Any database that pre-dates the Phase 4 M.1 migration has chunk
    rows that were never picked up by the trigger (which didn't exist
    yet). They need a one-shot backfill into the index.
  - If the index ever drifts out of sync with `chunks.text` (operator
    truncates `chunks_fts` for diagnosis, a botched manual SQL edit,
    etc.) running this script puts them back in step.

For an external-content FTS5 table the backfill primitive is the
special `'rebuild'` command — `INSERT INTO chunks_fts(chunks_fts)
VALUES('rebuild')` walks the source table and rebuilds the index from
scratch. We use that rather than an explicit row-by-row INSERT because
it's the documented, atomic way to do this for external-content tables.

Idempotent: re-running is safe — the rebuild always produces the same
index state for the same `chunks` table contents.

Usage
-----
    # Dry run — counts chunks and reports what would be rebuilt.
    python scripts/backfill_chunks_fts.py --dry-run

    # Real run.
    python scripts/backfill_chunks_fts.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Make `backend.*` imports work when running this file as a script —
# matches the pattern in scripts/inspect_storage.py and
# scripts/migrate_jsonl_to_sql.py.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select, text  # noqa: E402

from backend.config import settings  # noqa: E402
from backend.storage import init_db, session_scope  # noqa: E402
from backend.storage.db import _safe_url  # noqa: E402
from backend.storage.schema import chunks  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("backfill_chunks_fts")


async def _count_chunks() -> int:
    async with session_scope() as session:
        result = await session.execute(
            select(func.count()).select_from(chunks)
        )
        return int(result.scalar_one())


async def _count_fts_rows() -> int:
    """Best-effort count of rows currently in the FTS5 index.

    For external-content FTS5 tables, `SELECT COUNT(*) FROM chunks_fts`
    works as expected — same as any virtual table. If the index doesn't
    exist yet (fresh DB pre-migration), returns 0."""
    async with session_scope() as session:
        try:
            result = await session.execute(
                text("SELECT COUNT(*) FROM chunks_fts")
            )
            return int(result.scalar_one())
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not count chunks_fts rows: %s", e)
            return 0


async def _rebuild_fts() -> None:
    """Issue the FTS5 'rebuild' command. Atomic within a transaction —
    SQLite walks the source content table and rebuilds the index."""
    async with session_scope() as session:
        await session.execute(
            text("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        )


async def _run(args: argparse.Namespace) -> int:
    logger.info("Targeting database: %s", _safe_url(settings.database_url))

    # `init_db()` is idempotent and ensures the FTS5 table + triggers
    # exist before we try to rebuild. Without this, a fresh DB would
    # get a "no such table: chunks_fts" error.
    await init_db()

    chunk_count = await _count_chunks()
    fts_count_before = await _count_fts_rows()

    logger.info(
        "Found %d chunk rows; FTS5 index currently holds %d rows.",
        chunk_count, fts_count_before,
    )

    if args.dry_run:
        logger.info(
            "Dry run — would rebuild chunks_fts to mirror all %d chunks. "
            "No changes made.",
            chunk_count,
        )
        return 0

    if chunk_count == 0:
        logger.info("No chunks to index. Exiting cleanly.")
        return 0

    logger.info("Issuing FTS5 'rebuild' command…")
    await _rebuild_fts()

    fts_count_after = await _count_fts_rows()
    logger.info(
        "Done. FTS5 index now holds %d rows (was %d, %d chunks in source).",
        fts_count_after, fts_count_before, chunk_count,
    )
    if fts_count_after != chunk_count:
        logger.warning(
            "Row-count mismatch: chunks=%d, chunks_fts=%d. "
            "If this is unexpected, inspect the DB by hand — `rebuild` "
            "ought to produce a 1:1 mirror.",
            chunk_count, fts_count_after,
        )
        return 1
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Rebuild the chunks_fts FTS5 index from the chunks table.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Count chunks and report what would be rebuilt; make no changes.",
    )
    args = p.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
