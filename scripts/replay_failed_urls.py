"""Replay historical capture-failures through the now-fixed pipeline.

Phase 2.5 Fix 4. The 4 IG/FB URLs from the bug report (and any similar
URL-only captures that landed in `enrichment_skipped` since Fix 1
shipped) deserve to flow through the new hydration tiers — OG fetch
+ video transcription — so we can confirm Phase 2.5 actually closes
the loop on the bug that motivated it.

Sign-off (2026-04-30):
  - Pull URLs from capture_failures.jsonl tagged either `enrichment`
    or `enrichment_skipped`. Skip `capture`-phase rows — those are
    capture-side failures, replaying won't help.
  - Dedupe by URL (keep first occurrence — its capture_id wins).
  - Skip silently when the URL already has a successful enrichment
    row joined via capture_id (idempotent re-runs).
  - POST to /capture with the bot's payload shape so the full pipeline
    (hydration → enrich → sidecars) runs end-to-end.

Usage from repo root:

    python -m scripts.replay_failed_urls               # default — all phases, real run
    python -m scripts.replay_failed_urls --dry-run     # list what would be replayed
    python -m scripts.replay_failed_urls --limit 4     # cap after N replays
    python -m scripts.replay_failed_urls --phase enrichment_skipped
    python -m scripts.replay_failed_urls --backend-url http://127.0.0.1:8000

Exit codes:
  0  = at least one replay succeeded (or --dry-run completed)
  1  = nothing to replay / all candidates already enriched
  2  = backend unreachable
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx

# Allow `python -m scripts.replay_failed_urls` to find backend.* imports
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import settings  # noqa: E402


logger = logging.getLogger("replay")


# Phases worth replaying. Capture-side failures are skipped because the
# pipeline can't help if the bot/extension never produced a usable row.
_REPLAYABLE_PHASES: frozenset[str] = frozenset({"enrichment", "enrichment_skipped"})


# ---- Data shapes -----------------------------------------------------

@dataclass(frozen=True)
class FailureRow:
    """Just the fields we care about from capture_failures.jsonl."""
    capture_id: str | None
    url: str
    phase: str
    title: str | None
    platform: str | None
    timestamp: str | None
    reason: str | None


@dataclass(frozen=True)
class ReplayCandidate:
    """A URL that survived dedupe + already-enriched filtering."""
    url: str
    title: str
    platform: str
    original_capture_id: str | None
    original_phase: str
    original_reason: str | None


@dataclass
class ReplaySummary:
    """End-of-run breakdown — used for both human output and tests."""
    total_failure_rows: int = 0
    candidates_after_dedupe: int = 0
    skipped_already_enriched: int = 0
    skipped_capture_phase: int = 0
    posted: int = 0
    posted_ok: int = 0
    posted_failed: int = 0


# ---- File loading ----------------------------------------------------

def _load_jsonl(path: Path) -> Iterable[dict]:
    """Stream rows from a JSONL file, skipping malformed ones."""
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


def load_failure_rows(failures_path: Path) -> list[FailureRow]:
    rows: list[FailureRow] = []
    for raw in _load_jsonl(failures_path):
        url = raw.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        rows.append(FailureRow(
            capture_id=raw.get("capture_id"),
            url=url,
            phase=raw.get("phase") or "capture",
            title=raw.get("title"),
            platform=raw.get("platform"),
            timestamp=raw.get("timestamp"),
            reason=raw.get("reason"),
        ))
    return rows


def load_already_enriched_urls(
    captures_path: Path,
    enrichments_path: Path,
) -> set[str]:
    """Return the set of URLs whose capture_id has a row in
    enrichments.jsonl. We use captures.jsonl to bridge capture_id → URL
    because enrichments.jsonl doesn't carry the URL itself (Decision
    I — keep enrichment rows minimal).

    The empty-set behaviour when either file is missing is intentional:
    a fresh repo with no prior enrichments shouldn't skip anything.
    """
    enriched_ids: set[str] = set()
    for row in _load_jsonl(enrichments_path):
        cid = row.get("capture_id")
        if isinstance(cid, str) and cid:
            enriched_ids.add(cid)
    if not enriched_ids:
        return set()

    enriched_urls: set[str] = set()
    for row in _load_jsonl(captures_path):
        cid = row.get("capture_id")
        url = row.get("url")
        if isinstance(cid, str) and cid in enriched_ids and isinstance(url, str):
            enriched_urls.add(url)
    return enriched_urls


# ---- Selection -------------------------------------------------------

def select_candidates(
    failures: list[FailureRow],
    *,
    already_enriched_urls: set[str],
    phase_filter: str | None = None,
) -> tuple[list[ReplayCandidate], ReplaySummary]:
    """Apply phase filter, dedupe by URL, drop URLs already enriched.

    Order of `failures` is preserved — first occurrence of each URL
    wins, so the replay reflects the chronological order of the
    original failures."""
    summary = ReplaySummary(total_failure_rows=len(failures))
    seen_urls: set[str] = set()
    candidates: list[ReplayCandidate] = []
    for f in failures:
        if phase_filter is not None:
            if f.phase != phase_filter:
                continue
        elif f.phase not in _REPLAYABLE_PHASES:
            summary.skipped_capture_phase += 1
            continue
        if f.url in seen_urls:
            continue
        seen_urls.add(f.url)
        candidates.append(ReplayCandidate(
            url=f.url,
            title=f.title or "Replay capture",
            platform=f.platform or "general",
            original_capture_id=f.capture_id,
            original_phase=f.phase,
            original_reason=f.reason,
        ))
    summary.candidates_after_dedupe = len(candidates)

    if not already_enriched_urls:
        return candidates, summary

    keep: list[ReplayCandidate] = []
    for c in candidates:
        if c.url in already_enriched_urls:
            summary.skipped_already_enriched += 1
            continue
        keep.append(c)
    return keep, summary


# ---- Posting ---------------------------------------------------------

def _bot_style_payload(c: ReplayCandidate) -> dict:
    """Match what the Telegram bot's handle_text sends for a URL forward.
    The backend then runs the full Phase 2.5 hydration pipeline against
    this — same code path as a real bot capture."""
    return {
        "url": c.url,
        "title": c.title,
        "platform": c.platform,
        "content_type": "article",
        "text": "",
        "images": [],
        "dwell_time_seconds": 0,
        "metadata": {
            "source": "replay_failed_urls",
            "original_phase": c.original_phase,
            "original_reason": c.original_reason,
            "original_capture_id": c.original_capture_id,
        },
    }


async def post_one(
    client: httpx.AsyncClient,
    backend_url: str,
    candidate: ReplayCandidate,
) -> tuple[bool, str]:
    """POST one candidate. Returns (ok, message)."""
    try:
        resp = await client.post(
            backend_url.rstrip("/") + "/capture",
            json=_bot_style_payload(candidate),
            timeout=20.0,
        )
    except httpx.HTTPError as e:
        return False, f"network: {e}"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        body = resp.json()
    except json.JSONDecodeError:
        return False, "non-json response"
    cid = body.get("capture_id", "?")
    return True, f"capture_id={cid[:8]} text_len={body.get('text_length', '?')}"


# ---- Main ------------------------------------------------------------

async def _run(args: argparse.Namespace) -> int:
    failures_path = Path(args.failures or settings.capture_failures_path)
    captures_path = Path(args.captures or "./data/captures.jsonl")
    enrichments_path = Path(args.enrichments or settings.enrichments_path)

    failures = load_failure_rows(failures_path)
    if not failures:
        logger.info("No URL-bearing rows in %s — nothing to replay.", failures_path)
        return 1

    already = load_already_enriched_urls(captures_path, enrichments_path)
    candidates, summary = select_candidates(
        failures,
        already_enriched_urls=already,
        phase_filter=args.phase,
    )

    logger.info(
        "Found %d failure rows. After phase filter + dedupe: %d candidates. "
        "Already enriched (skipped): %d. To replay: %d.",
        summary.total_failure_rows,
        summary.candidates_after_dedupe,
        summary.skipped_already_enriched,
        len(candidates),
    )

    if not candidates:
        logger.info("Nothing to replay.")
        return 1

    if args.limit is not None:
        candidates = candidates[: args.limit]
        logger.info("Limit applied: replaying %d.", len(candidates))

    if args.dry_run:
        for i, c in enumerate(candidates, 1):
            print(f"[dry-run] {i:>3}. {c.url}  (was: phase={c.original_phase} reason={c.original_reason})")
        return 0

    # Real run — check backend health first.
    async with httpx.AsyncClient() as client:
        try:
            health = await client.get(args.backend_url.rstrip("/") + "/health", timeout=3.0)
            if health.status_code != 200:
                logger.error("Backend /health returned %d — aborting.", health.status_code)
                return 2
        except httpx.HTTPError as e:
            logger.error("Backend not reachable at %s: %s", args.backend_url, e)
            return 2

        for i, c in enumerate(candidates, 1):
            ok, msg = await post_one(client, args.backend_url, c)
            summary.posted += 1
            if ok:
                summary.posted_ok += 1
                logger.info("[%d/%d] ok  %s — %s", i, len(candidates), c.url, msg)
            else:
                summary.posted_failed += 1
                logger.warning("[%d/%d] err %s — %s", i, len(candidates), c.url, msg)
            if i < len(candidates) and args.throttle_ms > 0:
                await asyncio.sleep(args.throttle_ms / 1000.0)

    print()
    print(f"Replay summary: {summary.posted_ok} ok, {summary.posted_failed} failed, "
          f"{summary.skipped_already_enriched} skipped (already enriched).")
    print("Watch progress:")
    print("  tail -f data/hydrations.jsonl     # OG / video_transcript layers")
    print("  tail -f data/enrichments.jsonl    # final enriched rows")
    print("  tail -f data/capture_failures.jsonl  # phase=enrichment_skipped if hydration produced nothing")
    return 0 if summary.posted_ok > 0 else 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Replay historical capture-failures through the Phase 2.5 pipeline.",
    )
    p.add_argument("--backend-url", default="http://127.0.0.1:8000",
                   help="Backend base URL (default: http://127.0.0.1:8000)")
    p.add_argument("--failures", default=None,
                   help=f"Path to capture_failures.jsonl (default: {settings.capture_failures_path})")
    p.add_argument("--captures", default=None,
                   help="Path to captures.jsonl (default: ./data/captures.jsonl)")
    p.add_argument("--enrichments", default=None,
                   help=f"Path to enrichments.jsonl (default: {settings.enrichments_path})")
    p.add_argument("--phase", choices=["enrichment", "enrichment_skipped"], default=None,
                   help="Restrict to one phase (default: both replayable phases)")
    p.add_argument("--limit", type=int, default=None,
                   help="Stop after N replays (default: no limit)")
    p.add_argument("--throttle-ms", type=int, default=1000,
                   help="Delay between POSTs to be nice to backend + Anthropic (default: 1000ms)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print URLs that would be replayed; don't POST")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="DEBUG logging")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
