"""Backfill Phase 2 enrichment over captures already in SQL.

Implements Decision B (locked): backfill but skip test fixtures. Test
rows are identified by:
  - URL is empty / on example.com / starts with tg:// (Telegram debug)
  - clean_text is empty AND there's no transcript AND no image text
  - title is "Telegram link" with no body (Phase 1 mock script's signature)
  - metadata.source == "mock_capture" (set by scripts/mock_capture.py)

Idempotent — captures that already have an enrichment row are skipped
via the same SQL join the startup hook uses. Re-run as many times as
you like; only new rows pay Haiku cost.

Phase 3.5: this script used to walk `data/captures.jsonl` and skip
already-enriched ids from `data/enrichments.jsonl`. After the cutover,
both live in SQL, so we walk `iter_unenriched_captures()` instead and
apply the test-row classifier on the SQL row's content columns.

Usage:
    # Dry run — count what would be enriched, don't call Haiku.
    python scripts/backfill_enrichment.py --dry-run

    # Actually run.
    python scripts/backfill_enrichment.py

    # Limit to first N to bound cost during testing.
    python scripts/backfill_enrichment.py --limit 5

Cost reference: Haiku ~$0.003/capture. ~10-20 real captures in a fresh
log = $0.05. Cap with --limit if you're worried.
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

from backend.capture.processor import ProcessedContent  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.knowledge.enrichment_worker import (  # noqa: E402
    enqueue_enrichment,
    hydrate_processed_from_capture,
    iter_unenriched_captures,
)
from backend.knowledge.llm_client import LLMClient  # noqa: E402
from backend.storage import Capture, DEFAULT_USER_ID  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("backfill")


# ---- Test-row classifier (Decision B) -------------------------------

_TEST_URL_HOSTS = ("example.com", "example.org", "test.local")


def is_test_capture(capture: Capture) -> tuple[bool, str]:
    """Return (skip, reason). Conservative — only skips obvious fixtures.

    Works against a `Capture` SQL row instead of a JSONL dict so the
    same fingerprint logic still applies after the Phase 3.5 cutover.
    """
    url = (capture.url or "").strip().lower()
    title = (capture.title or "").strip()
    clean_text = (capture.clean_text or "").strip()
    transcript = (capture.transcript or "").strip() if capture.transcript else ""
    image_text = (capture.image_text or "").strip()
    try:
        metadata: Any = (
            json.loads(capture.raw_metadata_json)
            if capture.raw_metadata_json else {}
        )
    except json.JSONDecodeError:
        metadata = {}

    # Mock-capture-script fingerprint
    if isinstance(metadata, dict) and metadata.get("source") == "mock_capture":
        return True, "mock_capture metadata"

    # Empty URL
    if not url:
        return True, "empty url"

    # Telegram debug-only signature: bot writes "Telegram link" with no body
    if title == "Telegram link" and not clean_text and not image_text:
        return True, "empty telegram link"

    # Test domains
    for host in _TEST_URL_HOSTS:
        if host in url:
            return True, f"test domain ({host})"

    # Nothing-to-enrich (also caught by EmptyContentError later, but
    # cheaper to skip pre-flight)
    if not (clean_text or transcript or image_text):
        return True, "no extractable text"

    return False, ""


# ---- Main -----------------------------------------------------------

async def run(
    *,
    user_id: int,
    failures_path: Path,
    dry_run: bool,
    limit: int | None,
) -> int:
    candidates: list[tuple[str, ProcessedContent]] = []
    skipped_test = 0
    skipped_unhydratable = 0

    async for capture in iter_unenriched_captures(
        user_id=user_id,
        failures_path=failures_path,
    ):
        skip, reason = is_test_capture(capture)
        if skip:
            skipped_test += 1
            logger.debug("Skipping test row: %s (%s)", capture.url, reason)
            continue
        processed = hydrate_processed_from_capture(capture)
        if processed is None:
            skipped_unhydratable += 1
            logger.warning("Could not hydrate row: %s", capture.url)
            continue
        candidates.append((capture.id, processed))
        if limit and len(candidates) >= limit:
            break

    logger.info(
        "Candidates to enrich: %d (test skipped: %d, unhydratable: %d)",
        len(candidates), skipped_test, skipped_unhydratable,
    )

    if dry_run:
        logger.info("--dry-run: not calling Haiku. Exiting.")
        return 0

    if not candidates:
        return 0

    if not settings.anthropic_api_key:
        logger.error("ANTHROPIC_API_KEY is empty — set it in .env to run backfill.")
        return 2

    client = LLMClient()
    try:
        for i, (cid, processed) in enumerate(candidates, 1):
            logger.info(
                "[%d/%d] Enriching %s — %.80s",
                i, len(candidates), cid[:8], processed.title or "(no title)",
            )
            await enqueue_enrichment(cid, processed, client)
    finally:
        await client.aclose()
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Backfill Phase 2 enrichment.")
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
    p.add_argument("--dry-run", action="store_true", help="Count, don't call Haiku")
    p.add_argument("--limit", type=int, default=None, help="Cap candidates")
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
