"""Retry enrichment for capture_ids that have no row in enrichments.jsonl.

Counterpart to the FastAPI startup recovery hook (Decision H). Use this
when:

  - You want to retry enrichment without bouncing the backend.
  - You've fixed the underlying cause of an enrichment failure (e.g.,
    refilled API quota, fixed a bad prompt) and want to clear the backlog.
  - You want to re-run after editing data/captures.jsonl by hand.

This does NOT apply the test-row classifier — if it's in captures.jsonl
without an enrichment, it gets retried. Use scripts/backfill_enrichment.py
if you want test-row filtering.

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
import json
import logging
import sys
from pathlib import Path
from typing import Any

# Make `backend.*` imports work when running this file as a script.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.capture.processor import ProcessedContent  # noqa: E402,F401
from backend.config import settings  # noqa: E402
from backend.knowledge.enrichment_worker import (  # noqa: E402
    enqueue_enrichment,
    find_unenriched_capture_ids,
    hydrate_processed,
)
from backend.knowledge.llm_client import LLMClient  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("retry")


async def run(
    *,
    captures_path: Path,
    enrichments_path: Path,
    dry_run: bool,
    limit: int | None,
) -> int:
    target_ids = set(find_unenriched_capture_ids(
        captures_path=captures_path,
        enrichments_path=enrichments_path,
    ))
    logger.info("Found %d unenriched capture_ids", len(target_ids))
    if not target_ids:
        return 0

    # Re-scan captures.jsonl to hydrate ProcessedContent for each target.
    targets: list[tuple[str, ProcessedContent]] = []
    with captures_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = row.get("capture_id")
            if not isinstance(cid, str) or cid not in target_ids:
                continue
            processed = hydrate_processed(row)
            if processed is None:
                logger.warning("Could not hydrate row for %s — skipping", cid[:8])
                continue
            targets.append((cid, processed))
            if limit and len(targets) >= limit:
                break

    logger.info("Will retry %d capture(s)", len(targets))
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
    p.add_argument("--captures", default="./data/captures.jsonl", type=Path)
    p.add_argument(
        "--enrichments",
        default=settings.enrichments_path,
        type=Path,
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    rc = asyncio.run(run(
        captures_path=Path(args.captures),
        enrichments_path=Path(args.enrichments),
        dry_run=bool(args.dry_run),
        limit=args.limit,
    ))
    sys.exit(rc)


if __name__ == "__main__":
    main()
