"""Retry enrichment for captures that have no row in the enrichments
table.

Counterpart to the FastAPI startup recovery hook (Decision H). Use this
when:

  - You want to retry enrichment without bouncing the backend.
  - You've fixed the underlying cause of an enrichment failure (e.g.,
    refilled API quota, fixed a bad prompt) and want to clear the backlog.

Phase 3.5: this used to scan `data/captures.jsonl` and
`data/enrichments.jsonl`. After the cutover, captures and enrichments
live in SQL; the script walks the captures table via
`iter_unenriched_captures()` instead. Skipped (empty / oversized)
captures are still pulled from the failures log.

Usage:
    # Retry every unenriched capture
    python scripts/retry_failed_enrichments.py

    # Just count, don't call Haiku
    python scripts/retry_failed_enrichments.py --dry-run

    # Limit how many to retry per run (useful when working through a
    # large backlog without burning the whole budget)
    python scripts/retry_failed_enrichments.py --limit 10
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Make `backend.*` imports work when running this file as a script.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.capture.processor import ProcessedContent  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.knowledge.enrichment_worker import (  # noqa: E402
    enqueue_enrichment,
    hydrate_processed_from_capture,
    iter_unenriched_captures,
)
from backend.knowledge.llm_client import LLMClient  # noqa: E402
from backend.storage import DEFAULT_USER_ID  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("retry")


async def run(
    *,
    user_id: int,
    failures_path: Path,
    dry_run: bool,
    limit: int | None,
) -> int:
    targets: list[tuple[str, ProcessedContent]] = []
    async for capture in iter_unenriched_captures(
        user_id=user_id,
        failures_path=failures_path,
    ):
        processed = hydrate_processed_from_capture(capture)
        if processed is None:
            logger.warning(
                "Could not hydrate %s — empty content; tag in failures log "
                "or skip", capture.id[:8],
            )
            continue
        targets.append((capture.id, processed))
        if limit and len(targets) >= limit:
            break

    logger.info("Will retry %d capture(s)", len(targets))
    if not targets:
        return 0
    if dry_run:
        for cid, p in targets:
            logger.info("  %s — %.80s", cid[:8], p.title or "(no title)")
        return 0

    if not settings.anthropic_api_key:
        logger.error("ANTHROPIC_API_KEY is empty — set it in .env to retry.")
        return 2

    client = LLMClient()
    try:
        for i, (cid, processed) in enumerate(targets, 1):
            logger.info(
                "[%d/%d] Retrying %s — %.80s",
                i, len(targets), cid[:8], processed.title or "(no title)",
            )
            await enqueue_enrichment(cid, processed, client)
    finally:
        await client.aclose()
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Retry failed enrichments.")
    p.add_argument(
        "--user-id",
        type=int,
        default=DEFAULT_USER_ID,
        help="User to scan (default: %(default)s — Sabya).",
    )
    p.add_argument(
        "--failures",
        default=settings.capture_failures_path,
        type=Path,
        help="Path to capture_failures.jsonl (for skipped-rows filter).",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    rc = asyncio.run(run(
        user_id=int(args.user_id),
        failures_path=Path(args.failures),
        dry_run=bool(args.dry_run),
        limit=args.limit,
    ))
    sys.exit(rc)


if __name__ == "__main__":
    main()
