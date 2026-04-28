"""Backfill Phase 2 enrichment over existing data/captures.jsonl rows.

Implements Decision B (locked): backfill but skip test fixtures. Test
rows are identified by:
  - URL is empty / on example.com / starts with tg:// (Telegram debug)
  - clean_text is empty AND there's no transcript AND no image text
  - title is "Telegram link" with no body (Phase 1 mock script's signature)
  - metadata.source == "mock_capture" (set by scripts/mock_capture.py)

Idempotent — already-enriched capture_ids in data/enrichments.jsonl are
skipped. Re-run as many times as you like; only new rows pay Haiku cost.

Usage:
    # Dry run — count what would be enriched, don't call Haiku.
    python scripts/backfill_enrichment.py --dry-run

    # Actually run.
    python scripts/backfill_enrichment.py

    # Limit to first N to bound cost during testing.
    python scripts/backfill_enrichment.py --limit 5

    # Different file paths (rare).
    python scripts/backfill_enrichment.py \
        --captures data/captures.jsonl \
        --enrichments data/enrichments.jsonl

Cost reference: Haiku ~$0.003/capture. ~10-20 real captures in a fresh
log = $0.05. Cap with --limit if you're worried.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any, Iterator

# Make `backend.*` imports work when running this file as a script.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.capture.processor import ProcessedContent  # noqa: E402,F401
from backend.config import settings  # noqa: E402
from backend.knowledge.enrichment_worker import (  # noqa: E402
    enqueue_enrichment,
    hydrate_processed,
)
from backend.knowledge.llm_client import LLMClient  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("backfill")


# ---- Test-row classifier (Decision B) -------------------------------

_TEST_URL_HOSTS = ("example.com", "example.org", "test.local")


def is_test_row(row: dict[str, Any]) -> tuple[bool, str]:
    """Return (skip, reason). Conservative — only skips obvious fixtures."""
    url = (row.get("url") or "").strip().lower()
    title = (row.get("title") or "").strip()
    clean_text = (row.get("clean_text") or "").strip()
    transcript = (row.get("transcript") or "").strip() if row.get("transcript") else ""
    image_text = (row.get("image_text") or "").strip()
    metadata = row.get("metadata") or {}

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


# ---- I/O -------------------------------------------------------------

def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _enriched_ids(enrichments_path: Path) -> set[str]:
    out: set[str] = set()
    for row in _iter_jsonl(enrichments_path):
        cid = row.get("capture_id")
        if isinstance(cid, str) and cid:
            out.add(cid)
    return out


# ---- Main -----------------------------------------------------------

async def run(
    *,
    captures_path: Path,
    enrichments_path: Path,
    dry_run: bool,
    limit: int | None,
) -> int:
    enriched = _enriched_ids(enrichments_path)
    logger.info("Found %d already-enriched capture_ids", len(enriched))

    candidates: list[tuple[str, ProcessedContent]] = []
    skipped_test = 0
    skipped_already = 0
    skipped_unhydratable = 0
    minted_ids = 0

    for row in _iter_jsonl(captures_path):
        cid = row.get("capture_id")
        if not isinstance(cid, str) or not cid:
            # Row was written before Phase 2 added capture_id. Mint a
            # stable-ish UUID so we don't re-enrich the same row twice
            # across backfill runs. NOTE: this only stays stable within
            # a single backfill run — without rewriting captures.jsonl
            # we can't persist the minted ID. So a re-run will mint a
            # fresh ID and re-enrich. To make this idempotent for
            # pre-Phase-2 rows you'd need to migrate captures.jsonl.
            cid = str(uuid.uuid4())
            minted_ids += 1
        if cid in enriched:
            skipped_already += 1
            continue
        skip, reason = is_test_row(row)
        if skip:
            skipped_test += 1
            logger.debug("Skipping test row: %s (%s)", row.get("url"), reason)
            continue
        processed = hydrate_processed(row)
        if processed is None:
            skipped_unhydratable += 1
            logger.warning("Could not hydrate row: %s", row.get("url"))
            continue
        candidates.append((cid, processed))
        if limit and len(candidates) >= limit:
            break

    logger.info(
        "Candidates to enrich: %d (already-enriched: %d, test skipped: %d, "
        "unhydratable: %d, minted-ids-this-run: %d)",
        len(candidates), skipped_already, skipped_test,
        skipped_unhydratable, minted_ids,
    )
    if minted_ids:
        logger.warning(
            "WARNING: %d row(s) had no capture_id and got fresh UUIDs for this run. "
            "Re-running this script will mint fresh UUIDs again and re-enrich them. "
            "To make backfill idempotent for pre-Phase-2 rows, migrate captures.jsonl "
            "to add capture_id fields once.",
            minted_ids,
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
    p.add_argument("--captures", default="./data/captures.jsonl", type=Path)
    p.add_argument(
        "--enrichments",
        default=settings.enrichments_path,
        type=Path,
    )
    p.add_argument("--dry-run", action="store_true", help="Count, don't call Haiku")
    p.add_argument("--limit", type=int, default=None, help="Cap candidates")
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
