"""Phase 3 Step 5 — Backfill historical JSONLs into SQL + Chroma.

Implements docs/phase3-design.md B.5. This is a one-shot (idempotent,
re-runnable) migration that reads the three sidecar JSONLs:

    data/captures.jsonl      → captures (+ users.id=1 if absent)
    data/hydrations.jsonl    → hydrations
    data/enrichments.jsonl   → enrichments + chunks + topics + entities + junctions

…and lands them in the new SQL store and the ChromaDB vector collections,
using the same `sync_*` functions the live dual-write path uses
(`backend.storage.sync`). Reusing those functions means historical and
live data go through identical chunking + embedding + vocabulary code
paths — no chance of a "migration bug" that the live path doesn't have.

What this script DOES NOT do
----------------------------
- Does not re-call Anthropic. Existing summaries / topics / entities
  from `enrichments.jsonl` are taken as-is.
- Does not re-fetch URLs or re-transcribe videos. Hydration sidecars
  carry only metadata (title, image_url, char counts) — the rendered
  body / transcript was never persisted to disk in Phase 2.5. So
  historically-hydrated captures will land in SQL with whatever
  `clean_text` and `transcript` they had when first captured (typically
  empty for OG-hydrated rows and Telegram-forwarded reels). The
  enrichment summary is always chunked, so each enriched capture
  contributes at minimum one summary chunk to the vector store —
  enough for vague-recall retrieval (use case B). Future captures
  written by the Step 4 dual-write path will have full content.

Order of operations (B.5.5)
---------------------------
1. Ensure schema + user_id=1 (Sabya).
2. captures.jsonl     → sync_capture (idempotent via tenant-scoped get).
3. hydrations.jsonl   → sync_hydration (skipped if SQL already has any
                        hydration row for that capture_id).
4. enrichments.jsonl  → sync_enrichment (skipped if SQL already has any
                        enrichment row for that capture_id). Triggers
                        chunk + embed + topic + entity writes.

Idempotency (B.5.1)
-------------------
Each step pre-loads the set of capture_ids already mirrored to SQL and
skips those rows. So re-running after a partial failure picks up where
the previous run left off without duplicating data. Pre-Phase-2 capture
rows lacking a `capture_id` field get a deterministic uuid5 derived
from (url + timestamp) so the same legacy row gets the same id across
runs.

Validation failures (B.5.3)
---------------------------
Bad rows are logged to data/migration_failures.jsonl with
{source_file, line_number, raw_row, error_reason} and the migration
continues. Operator reviews the failures, fixes if possible, re-runs
(idempotent). The migration never aborts on a single bad row.

Usage
-----
    # Dry run — count, validate, write nothing.
    python scripts/migrate_jsonl_to_sql.py --dry-run

    # Real run.
    python scripts/migrate_jsonl_to_sql.py

    # Verify after running. Prints SQL row counts vs JSONL line counts
    # and spot-checks 5 random capture_ids' content. Non-zero exit on
    # any mismatch.
    python scripts/migrate_jsonl_to_sql.py --verify

    # Bound work to first N candidates per file (debugging only).
    python scripts/migrate_jsonl_to_sql.py --limit 10

    # Skip the test-fixture skip (mock_capture, example.com, etc.).
    # Use only if you really want test rows in SQL.
    python scripts/migrate_jsonl_to_sql.py --include-test-rows
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

# Make `backend.*` imports work when running this file as a script.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dataclasses import fields as dc_fields  # noqa: E402

from backend.capture.processor import ProcessedContent  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.storage import (  # noqa: E402
    CaptureRepository,
    ChunkRepository,
    EnrichmentRepository,
    HydrationRepository,
    UserRepository,
)
from backend.storage.db import init_db, session_scope  # noqa: E402
from backend.storage.sync import (  # noqa: E402
    DEFAULT_USER_ID,
    sync_capture,
    sync_enrichment,
    sync_hydration,
)
from sqlalchemy import select  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("migrate")


# Deterministic namespace for minted capture_ids on pre-Phase-2 rows.
# Stable across runs so re-running picks up the same row by the same
# id and skip-on-exists works.
_LEGACY_CAPTURE_NS = uuid.UUID("a4d29c6e-7d1e-4f8e-b9b1-1a2b3c4d5e6f")


# ---- Helpers --------------------------------------------------------

# Test-row classifier — kept local to this script since post-Phase-3.5
# `scripts/backfill_enrichment.py` walks SQL rows (`is_test_capture`)
# and no longer exposes a JSONL-dict variant. Rules match the original
# backfill classifier so historical fixtures get skipped consistently.
_TEST_URL_HOSTS = ("example.com", "example.org", "test.local")


def is_test_row(row: dict[str, Any]) -> tuple[bool, str]:
    """Return (skip, reason). Conservative — only skips obvious fixtures.

    Identifies:
      - URL is empty / on example.com / starts with tg:// (Telegram debug)
      - clean_text is empty AND there's no transcript AND no image text
      - title is "Telegram link" with no body (Phase 1 mock script's signature)
      - metadata.source == "mock_capture" (scripts/mock_capture.py)
    """
    url = (row.get("url") or "").strip().lower()
    title = (row.get("title") or "").strip()
    clean_text = (row.get("clean_text") or "").strip()
    transcript = (row.get("transcript") or "").strip() if row.get("transcript") else ""
    image_text = (row.get("image_text") or "").strip()
    metadata = row.get("metadata") or {}

    if isinstance(metadata, dict) and metadata.get("source") == "mock_capture":
        return True, "mock_capture metadata"
    if not url:
        return True, "empty url"
    if title == "Telegram link" and not clean_text and not image_text:
        return True, "empty telegram link"
    for host in _TEST_URL_HOSTS:
        if host in url:
            return True, f"test domain ({host})"
    if not (clean_text or transcript or image_text):
        return True, "no extractable text"
    return False, ""


# Defaults used when an old pre-Phase-2 row is missing fields the
# `ProcessedContent` dataclass now expects. These match the dataclass
# default values where they have one. Inlined here post-Phase-3.5 —
# the live worker no longer reads JSONL captures, but this migration
# script still has to translate historical rows into ProcessedContent.
_HYDRATION_DEFAULTS: dict[str, Any] = {
    "transcript": None,
    "image_descriptions": [],
    "image_text": "",
    "metadata": {},
    "dwell_time_seconds": 0,
}


def hydrate_processed(row: dict[str, Any]) -> Optional[ProcessedContent]:
    """Reconstruct a `ProcessedContent` from a captures.jsonl row.

    Frozen historical helper — used only by this migration script to
    translate pre-cutover JSONL rows into the in-memory shape
    `sync_enrichment` expects. The live path uses
    `hydrate_processed_from_capture` (a `Capture` SQL row) instead.
    """
    expected = {f.name for f in dc_fields(ProcessedContent)}
    payload = {k: v for k, v in row.items() if k in expected}
    for k, v in _HYDRATION_DEFAULTS.items():
        payload.setdefault(k, v)
    if expected - payload.keys():
        return None
    try:
        return ProcessedContent(**payload)
    except TypeError:
        return None


def _iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any], str]]:
    """Stream a JSONL file, yielding (line_number, parsed_row, raw_line).

    Skips blank lines silently. Malformed JSON yields a (lineno, None,
    raw_line) sentinel so the caller can log it as a validation failure
    rather than crash."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                yield lineno, json.loads(line), line
            except json.JSONDecodeError as e:
                # Sentinel: row is None, raw_line carries the offender.
                yield lineno, {"__bad_json__": True, "error": str(e)}, line


def _mint_legacy_capture_id(row: dict[str, Any]) -> str:
    """Deterministic uuid5 for pre-Phase-2 capture rows.

    Without `capture_id`, the row needs SOMETHING stable that the
    migration can use as the SQL primary key and that re-runs will
    re-derive identically. uuid5 over (url, timestamp) is the simplest
    safe choice — both fields are present in every legacy row and the
    pair is unique enough in practice. Truly malformed rows missing
    both will fall through to a one-time uuid4 (caller will then see
    them re-inserted on every re-run; acceptable for the long tail).
    """
    url = (row.get("url") or "").strip()
    ts = (row.get("timestamp") or "").strip()
    if url or ts:
        return str(uuid.uuid5(_LEGACY_CAPTURE_NS, f"{url}|{ts}"))
    return str(uuid.uuid4())


_failures_dirs_created: set[Path] = set()


def _log_failure(
    failures_path: Path,
    *,
    source_file: str,
    line_number: int,
    raw_row: Any,
    error_reason: str,
) -> None:
    """Append a B.5.3 validation-failure row to data/migration_failures.jsonl.

    Mirrors the shape used elsewhere in the codebase (newline-delimited
    JSON, UTF-8, no fancy encoding tricks). mkdir runs once per
    failures_path the first time we see it — subsequent calls skip the
    syscall (negligible cost, but adds up across thousands of failures)."""
    record = {
        "source_file": source_file,
        "line_number": line_number,
        "raw_row": raw_row,
        "error_reason": error_reason,
    }
    if failures_path not in _failures_dirs_created:
        failures_path.parent.mkdir(parents=True, exist_ok=True)
        _failures_dirs_created.add(failures_path)
    with failures_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---- Idempotency probes --------------------------------------------

async def _existing_capture_ids(user_id: int) -> set[str]:
    from backend.storage.schema import captures
    async with session_scope() as session:
        result = await session.execute(
            select(captures.c.id).where(captures.c.user_id == user_id)
        )
        return {row.id for row in result}


async def _existing_hydrated_capture_ids(user_id: int) -> set[str]:
    """capture_ids that already have at least one hydration row in SQL."""
    from backend.storage.schema import captures, hydrations
    async with session_scope() as session:
        result = await session.execute(
            select(hydrations.c.capture_id)
            .join(captures, hydrations.c.capture_id == captures.c.id)
            .where(captures.c.user_id == user_id)
            .distinct()
        )
        return {row.capture_id for row in result}


async def _existing_enriched_capture_ids(user_id: int) -> set[str]:
    async with session_scope() as session:
        return await EnrichmentRepository(session).enriched_capture_ids(
            user_id=user_id,
        )


async def _ensure_default_user() -> None:
    """Same shape as backend.main._ensure_default_user — idempotent
    seed of user_id=1 (Sabya, per B.5.4). Lifted here so the migration
    script doesn't depend on FastAPI's startup hook having run."""
    async with session_scope() as session:
        repo = UserRepository(session)
        if await repo.get(DEFAULT_USER_ID) is None:
            await repo.create(
                email="sabya.bisoyi@gmail.com",
                display_name="Sabya",
                user_id=DEFAULT_USER_ID,
            )
            logger.info("Seeded default user_id=%s (Sabya)", DEFAULT_USER_ID)


# ---- Stage 1: captures ----------------------------------------------

async def _migrate_captures(
    *,
    captures_path: Path,
    failures_path: Path,
    user_id: int,
    dry_run: bool,
    include_test_rows: bool,
    limit: Optional[int],
    real_capture_ids: Optional[set[str]] = None,
) -> dict[str, int]:
    """Walk captures.jsonl and call sync_capture on each non-test row.

    Returns counts dict — caller logs the summary.

    `real_capture_ids` is the set of capture_ids that have at least one
    downstream artifact (hydration or enrichment row). For those captures
    we bypass the test-fixture filter entirely — if the live pipeline
    produced downstream artifacts for the row, it's real, regardless of
    what the heuristic classifier says. This handles the common pattern
    where a Telegram-forwarded URL lands with title="Telegram link" and
    clean_text="" (which `is_test_row` would otherwise flag as an empty
    Telegram link fixture), but Phase 2.5 hydration later populates the
    content via OG metadata or video transcription. Without this
    override, the migration would orphan every downstream artifact whose
    parent matches that signature."""
    counts = {
        "seen": 0, "test_skipped": 0, "already_in_sql": 0,
        "minted_ids": 0, "inserted": 0, "failed": 0, "bad_json": 0,
    }
    real_capture_ids = real_capture_ids or set()
    # Pre-load even in dry-run so the counts the operator sees match
    # what a real run would actually do (otherwise dry-run reports
    # every row as "inserted" even when SQL is already populated).
    existing = await _existing_capture_ids(user_id)

    for lineno, row, raw in _iter_jsonl(captures_path):
        counts["seen"] += 1
        if isinstance(row, dict) and row.get("__bad_json__"):
            counts["bad_json"] += 1
            _log_failure(
                failures_path,
                source_file=str(captures_path),
                line_number=lineno,
                raw_row=raw,
                error_reason=row.get("error", "json_decode_error"),
            )
            continue

        cid = row.get("capture_id")
        if not isinstance(cid, str) or not cid:
            cid = _mint_legacy_capture_id(row)
            counts["minted_ids"] += 1

        # Only apply the test-fixture filter if the live pipeline didn't
        # already produce downstream artifacts for this capture. This
        # rescues Telegram-forwarded URLs that look like "empty link"
        # fixtures but were hydrated downstream.
        if not include_test_rows and cid not in real_capture_ids:
            skip, reason = is_test_row(row)
            if skip:
                counts["test_skipped"] += 1
                logger.debug("captures.jsonl:%d skip (%s)", lineno, reason)
                continue

        if cid in existing:
            counts["already_in_sql"] += 1
            continue

        if dry_run:
            counts["inserted"] += 1  # would-have-inserted
            continue

        # Match the live /capture path: NULL when no metadata, not "{}".
        # Drift here would leave queries like "captures with no metadata"
        # returning different sets depending on whether the row was
        # migrated or written live.
        metadata = row.get("metadata")
        ok = await sync_capture(
            capture_id=cid,
            url=row.get("url"),
            title=row.get("title"),
            platform=row.get("platform"),
            content_type=row.get("content_type"),
            captured_at=row.get("timestamp") or "",
            dwell_seconds=int(row.get("dwell_time_seconds") or 0),
            raw_metadata_json=(
                json.dumps(metadata, ensure_ascii=False) if metadata else None
            ),
            user_id=user_id,
        )
        if ok:
            counts["inserted"] += 1
            existing.add(cid)
        else:
            # sync_capture returns False on duplicate OR on error. The
            # pre-load above filters duplicates, so a False here means
            # something legitimately went wrong — log it.
            counts["failed"] += 1
            _log_failure(
                failures_path,
                source_file=str(captures_path),
                line_number=lineno,
                raw_row=row,
                error_reason="sync_capture returned False (see worker log)",
            )

        if limit and counts["inserted"] >= limit:
            logger.info("--limit %d reached for captures", limit)
            break

    return counts


# ---- Stage 2: hydrations --------------------------------------------

async def _migrate_hydrations(
    *,
    hydrations_path: Path,
    failures_path: Path,
    user_id: int,
    dry_run: bool,
    limit: Optional[int],
) -> dict[str, int]:
    """Walk hydrations.jsonl and call sync_hydration on each row.

    Idempotency: pre-load the set of capture_ids that already have
    hydration rows in SQL and skip those. The hydrations table has no
    UNIQUE on capture_id (Phase 2.5 allowed multiple tiers per capture
    — og_metadata first, video_transcript second), so we can't rely on
    a constraint. Coarse-grained skip (any hydration → skip whole row)
    is the safe choice."""
    counts = {
        "seen": 0, "skipped_no_capture_id": 0, "already_in_sql": 0,
        "inserted": 0, "failed": 0, "bad_json": 0,
    }
    # Pre-load even in dry-run — see _migrate_captures for the same reasoning.
    existing = await _existing_hydrated_capture_ids(user_id)
    captures_in_sql = await _existing_capture_ids(user_id)

    for lineno, row, raw in _iter_jsonl(hydrations_path):
        counts["seen"] += 1
        if isinstance(row, dict) and row.get("__bad_json__"):
            counts["bad_json"] += 1
            _log_failure(
                failures_path,
                source_file=str(hydrations_path),
                line_number=lineno,
                raw_row=raw,
                error_reason=row.get("error", "json_decode_error"),
            )
            continue

        cid = row.get("capture_id")
        if not isinstance(cid, str) or not cid:
            counts["skipped_no_capture_id"] += 1
            _log_failure(
                failures_path,
                source_file=str(hydrations_path),
                line_number=lineno,
                raw_row=row,
                error_reason="hydration row has no capture_id",
            )
            continue

        if cid in existing:
            counts["already_in_sql"] += 1
            continue

        if cid not in captures_in_sql:
            # Parent capture was filtered out (test fixture or bad row).
            # FK would fail; skip with a logged note. Applies in dry-run
            # too — the operator wants to see this gap before committing.
            counts["failed"] += 1
            _log_failure(
                failures_path,
                source_file=str(hydrations_path),
                line_number=lineno,
                raw_row=row,
                error_reason=f"parent capture {cid} not in SQL (filtered or failed)",
            )
            continue

        if dry_run:
            counts["inserted"] += 1
            continue

        ok = await sync_hydration(
            capture_id=cid,
            tier=row.get("tier", "unknown"),
            source_payload_json=json.dumps(row, ensure_ascii=False),
            hydrated_at=row.get("timestamp") or "",
        )
        if ok:
            counts["inserted"] += 1
            existing.add(cid)
        else:
            counts["failed"] += 1
            _log_failure(
                failures_path,
                source_file=str(hydrations_path),
                line_number=lineno,
                raw_row=row,
                error_reason="sync_hydration returned False (see worker log)",
            )

        if limit and counts["inserted"] >= limit:
            logger.info("--limit %d reached for hydrations", limit)
            break

    return counts


# ---- Stage 3: enrichments (+ chunks + topics + entities) -----------

def _build_real_capture_ids(
    *,
    hydrations_path: Path,
    enrichments_path: Path,
) -> set[str]:
    """Return capture_ids that have at least one downstream artifact —
    a hydration row or an enrichment row referencing the capture_id.

    Used by Stage 1 to override the test-fixture skip rule. If the live
    pipeline produced a hydration or enrichment for a capture, the
    pipeline considered it real and we should mirror it to SQL even if
    the heuristic `is_test_row` classifier would otherwise reject it
    (e.g. Telegram-forwarded URLs with `title="Telegram link"` and
    `clean_text=""`, which become non-empty only after Phase 2.5
    hydration writes into `hydrations.jsonl`)."""
    real: set[str] = set()
    for path in (hydrations_path, enrichments_path):
        for _lineno, row, _raw in _iter_jsonl(path):
            if not isinstance(row, dict) or row.get("__bad_json__"):
                continue
            cid = row.get("capture_id")
            if isinstance(cid, str) and cid:
                real.add(cid)
    return real


def _build_capture_lookup(captures_path: Path) -> dict[str, dict[str, Any]]:
    """Index captures.jsonl by capture_id (or minted legacy id) so the
    enrichment pass can hand a ProcessedContent to sync_enrichment."""
    out: dict[str, dict[str, Any]] = {}
    for _lineno, row, _raw in _iter_jsonl(captures_path):
        if not isinstance(row, dict) or row.get("__bad_json__"):
            continue
        cid = row.get("capture_id")
        if not isinstance(cid, str) or not cid:
            cid = _mint_legacy_capture_id(row)
        out[cid] = row
    return out


async def _migrate_enrichments(
    *,
    captures_path: Path,
    enrichments_path: Path,
    failures_path: Path,
    user_id: int,
    dry_run: bool,
    limit: Optional[int],
) -> dict[str, int]:
    counts = {
        "seen": 0, "skipped_no_capture_id": 0, "already_in_sql": 0,
        "missing_parent": 0, "unhydratable": 0, "inserted": 0,
        "failed": 0, "bad_json": 0,
    }

    capture_index = _build_capture_lookup(captures_path)
    # Pre-load even in dry-run — see _migrate_captures for the same reasoning.
    existing = await _existing_enriched_capture_ids(user_id)
    captures_in_sql = await _existing_capture_ids(user_id)

    for lineno, row, raw in _iter_jsonl(enrichments_path):
        counts["seen"] += 1
        if isinstance(row, dict) and row.get("__bad_json__"):
            counts["bad_json"] += 1
            _log_failure(
                failures_path,
                source_file=str(enrichments_path),
                line_number=lineno,
                raw_row=raw,
                error_reason=row.get("error", "json_decode_error"),
            )
            continue

        cid = row.get("capture_id")
        if not isinstance(cid, str) or not cid:
            counts["skipped_no_capture_id"] += 1
            _log_failure(
                failures_path,
                source_file=str(enrichments_path),
                line_number=lineno,
                raw_row=row,
                error_reason="enrichment row has no capture_id",
            )
            continue

        if cid in existing:
            counts["already_in_sql"] += 1
            continue

        if cid not in captures_in_sql:
            # Same posture as stage 2 — surface the gap in dry-run too.
            counts["missing_parent"] += 1
            _log_failure(
                failures_path,
                source_file=str(enrichments_path),
                line_number=lineno,
                raw_row=row,
                error_reason=f"parent capture {cid} not in SQL (filtered or failed)",
            )
            continue

        # Defensive: a corrupted row might have `enrichment` as a string
        # or list. Without this guard the next .get() raises AttributeError
        # mid-loop and the whole migration aborts.
        enrichment_obj = row.get("enrichment")
        if enrichment_obj is not None and not isinstance(enrichment_obj, dict):
            counts["failed"] += 1
            _log_failure(
                failures_path,
                source_file=str(enrichments_path),
                line_number=lineno,
                raw_row=row,
                error_reason=(
                    f"enrichment field is {type(enrichment_obj).__name__}, "
                    f"expected dict"
                ),
            )
            continue

        capture_row = capture_index.get(cid)
        processed = (
            hydrate_processed(capture_row) if capture_row is not None else None
        )
        # processed may be None — that's fine; sync_enrichment skips
        # the chunk pipeline when processed is None and just writes
        # the enrichment row + topics + entities. The summary won't
        # be chunked in that case, but vague-recall still works via
        # capture metadata.
        if capture_row is not None and processed is None:
            counts["unhydratable"] += 1
            logger.warning(
                "enrichments.jsonl:%d cannot hydrate parent capture %s — "
                "writing enrichment row + topics/entities only",
                lineno, cid,
            )

        enrichment = enrichment_obj or {}
        summary = enrichment.get("summary")
        topics = enrichment.get("topics") or []
        entities = enrichment.get("entities") or []
        # The live path normalizes entity dicts to {label, entity_type}
        # but the JSONL emits {name, type}; sync_enrichment already
        # handles both shapes (reads name OR label, type OR entity_type),
        # so we pass straight through.
        key_facts = enrichment.get("key_facts") or []

        if dry_run:
            counts["inserted"] += 1
            continue

        ok = await sync_enrichment(
            capture_id=cid,
            summary=summary,
            key_facts_json=json.dumps(key_facts, ensure_ascii=False),
            topics=topics if isinstance(topics, list) else [],
            entities=entities if isinstance(entities, list) else [],
            model=row.get("model"),
            enriched_at=row.get("enriched_at") or "",
            processed=processed,
            user_id=user_id,
        )
        if ok:
            counts["inserted"] += 1
            existing.add(cid)
        else:
            counts["failed"] += 1
            _log_failure(
                failures_path,
                source_file=str(enrichments_path),
                line_number=lineno,
                raw_row=row,
                error_reason="sync_enrichment returned False (see worker log)",
            )

        if limit and counts["inserted"] >= limit:
            logger.info("--limit %d reached for enrichments", limit)
            break

    return counts


# ---- Verify subcommand (B.5.8) --------------------------------------

def _count_jsonl_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


async def _verify(
    *,
    captures_path: Path,
    hydrations_path: Path,
    enrichments_path: Path,
    user_id: int,
    include_test_rows: bool,
) -> int:
    """Print SQL row counts vs JSONL line counts and spot-check 5 random
    capture_ids' content. Non-zero exit on any mismatch.

    Notes on the comparison:
      - JSONL line count is the upper bound. SQL count subtracts test
        fixtures (skipped at migration time per Decision B from
        backfill_enrichment.py) unless --include-test-rows was used.
      - For hydrations, multiple JSONL rows for the same capture_id
        DO each map to a SQL row (no UNIQUE constraint). Counts should
        match modulo skipped legacy rows lacking capture_id.
    """
    from backend.storage.schema import (
        captures as captures_t,
        chunks as chunks_t,
        enrichments as enrichments_t,
        hydrations as hydrations_t,
    )
    from sqlalchemy import func

    # --- JSONL bookkeeping ------------------------------------------
    jsonl_capture_lines = 0
    jsonl_capture_kept = 0  # post-test-skip, with a derivable capture_id
    for _lineno, row, _raw in _iter_jsonl(captures_path):
        if not isinstance(row, dict) or row.get("__bad_json__"):
            continue
        jsonl_capture_lines += 1
        if not include_test_rows:
            skip, _reason = is_test_row(row)
            if skip:
                continue
        jsonl_capture_kept += 1

    jsonl_hydration_lines = _count_jsonl_lines(hydrations_path)
    jsonl_enrichment_lines = _count_jsonl_lines(enrichments_path)

    # --- SQL counts -------------------------------------------------
    async with session_scope() as session:
        sql_captures = (await session.execute(
            select(func.count()).select_from(captures_t)
            .where(captures_t.c.user_id == user_id)
        )).scalar_one()
        sql_hydrations = (await session.execute(
            select(func.count()).select_from(hydrations_t)
            .join(captures_t, hydrations_t.c.capture_id == captures_t.c.id)
            .where(captures_t.c.user_id == user_id)
        )).scalar_one()
        sql_enrichments = (await session.execute(
            select(func.count()).select_from(enrichments_t)
            .join(captures_t, enrichments_t.c.capture_id == captures_t.c.id)
            .where(captures_t.c.user_id == user_id)
        )).scalar_one()
        sql_chunks = (await session.execute(
            select(func.count()).select_from(chunks_t)
            .join(captures_t, chunks_t.c.capture_id == captures_t.c.id)
            .where(captures_t.c.user_id == user_id)
        )).scalar_one()

    logger.info("--- VERIFY: row counts ---")
    logger.info(
        "captures   : JSONL=%d (kept after test-skip=%d) vs SQL=%d",
        jsonl_capture_lines, jsonl_capture_kept, sql_captures,
    )
    logger.info(
        "hydrations : JSONL=%d vs SQL=%d",
        jsonl_hydration_lines, sql_hydrations,
    )
    logger.info(
        "enrichments: JSONL=%d vs SQL=%d",
        jsonl_enrichment_lines, sql_enrichments,
    )
    logger.info("chunks (derived)            : SQL=%d", sql_chunks)

    # The captures count is the only one we hold to a strict >= match
    # because of test-row skipping. Hydrations and enrichments may have
    # rows whose parent capture got filtered (logged to migration_failures);
    # those are expected gaps, not errors.
    rc = 0
    if sql_captures < jsonl_capture_kept:
        logger.error(
            "MISMATCH: SQL captures (%d) < kept JSONL captures (%d). "
            "Check data/migration_failures.jsonl for the gap.",
            sql_captures, jsonl_capture_kept,
        )
        rc = 1

    # --- Spot-check 5 random capture_ids ----------------------------
    async with session_scope() as session:
        cap_repo = CaptureRepository(session)
        chunk_repo = ChunkRepository(session)
        all_ids_result = await session.execute(
            select(captures_t.c.id).where(captures_t.c.user_id == user_id)
        )
        all_ids = [r.id for r in all_ids_result]

    if not all_ids:
        logger.warning("No captures in SQL — nothing to spot-check.")
        return rc

    sample = random.sample(all_ids, min(5, len(all_ids)))
    logger.info("--- VERIFY: spot-check %d random capture_ids ---", len(sample))
    for cid in sample:
        async with session_scope() as session:
            cap_repo = CaptureRepository(session)
            chunk_repo = ChunkRepository(session)
            cap = await cap_repo.get(cid, user_id=user_id)
            chunks = await chunk_repo.list_by_capture(cid, user_id=user_id)
        if cap is None:
            logger.error("spot-check FAIL: %s not found", cid)
            rc = 1
            continue
        logger.info(
            "  %s | platform=%s | chunks=%d | title=%.60s",
            cid[:8], cap.platform or "(none)", len(chunks),
            (cap.title or "(no title)"),
        )
    return rc


# ---- Main -----------------------------------------------------------

async def run(
    *,
    captures_path: Path,
    hydrations_path: Path,
    enrichments_path: Path,
    failures_path: Path,
    dry_run: bool,
    include_test_rows: bool,
    limit: Optional[int],
    verify_only: bool,
) -> int:
    # Phase 3.5 — the storage_dual_write gate was retired with the
    # JSONL writers; SQL is now the only persistence path so there's
    # nothing to flip. This script remains as a frozen historical
    # backfill tool for pre-Phase-3 JSONL archives.

    # Always ensure schema + default user before anything reads SQL.
    # init_db is idempotent (CREATE TABLE IF NOT EXISTS) and _ensure_default_user
    # is a single SELECT + maybe-INSERT, so this is cheap even on dry-run
    # / verify — and required by dry-run too because pre-loading the
    # existence sets reads from the captures / enrichments / hydrations
    # tables.
    try:
        await init_db()
        await _ensure_default_user()
    except Exception as e:  # noqa: BLE001
        logger.error("init_db / ensure_default_user failed: %s", e)
        return 2

    if verify_only:
        return await _verify(
            captures_path=captures_path,
            hydrations_path=hydrations_path,
            enrichments_path=enrichments_path,
            user_id=DEFAULT_USER_ID,
            include_test_rows=include_test_rows,
        )

    if dry_run:
        logger.info("--dry-run: no SQL/Chroma writes will happen.")

    # Build the set of capture_ids that have downstream artifacts. Stage 1
    # uses this to rescue captures that match a test-fixture heuristic
    # (e.g. Telegram-forwarded "empty link" pattern) but were actually
    # hydrated/enriched by the live pipeline downstream. Without this,
    # the test-fixture skip would orphan every hydration + enrichment
    # whose parent matches the empty-Telegram-link signature.
    real_capture_ids = _build_real_capture_ids(
        hydrations_path=hydrations_path,
        enrichments_path=enrichments_path,
    )
    if real_capture_ids:
        logger.info(
            "Loaded %d capture_ids with downstream artifacts "
            "(these bypass test-fixture skip)",
            len(real_capture_ids),
        )

    # Stage 1 — captures
    logger.info("Stage 1/3 — captures.jsonl → captures table")
    cap_counts = await _migrate_captures(
        captures_path=captures_path,
        failures_path=failures_path,
        user_id=DEFAULT_USER_ID,
        dry_run=dry_run,
        include_test_rows=include_test_rows,
        limit=limit,
        real_capture_ids=real_capture_ids,
    )
    logger.info("Stage 1 counts: %s", cap_counts)

    # Stage 2 — hydrations
    logger.info("Stage 2/3 — hydrations.jsonl → hydrations table")
    hyd_counts = await _migrate_hydrations(
        hydrations_path=hydrations_path,
        failures_path=failures_path,
        user_id=DEFAULT_USER_ID,
        dry_run=dry_run,
        limit=limit,
    )
    logger.info("Stage 2 counts: %s", hyd_counts)

    # Stage 3 — enrichments + derived (chunks, topics, entities)
    logger.info("Stage 3/3 — enrichments.jsonl → enrichments + chunks + topics + entities")
    enr_counts = await _migrate_enrichments(
        captures_path=captures_path,
        enrichments_path=enrichments_path,
        failures_path=failures_path,
        user_id=DEFAULT_USER_ID,
        dry_run=dry_run,
        limit=limit,
    )
    logger.info("Stage 3 counts: %s", enr_counts)

    total_failed = (
        cap_counts["failed"] + hyd_counts["failed"] + enr_counts["failed"]
        + cap_counts["bad_json"] + hyd_counts["bad_json"] + enr_counts["bad_json"]
    )
    if total_failed:
        logger.warning(
            "Migration completed with %d row-level failures. "
            "See %s for details.",
            total_failed, failures_path,
        )
    else:
        logger.info("Migration completed cleanly.")

    if dry_run:
        logger.info("Dry run complete. Re-run without --dry-run to apply.")
    else:
        logger.info("Tip: run `python scripts/migrate_jsonl_to_sql.py --verify`")

    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Phase 3 Step 5 — backfill JSONL sidecars into SQL + Chroma.",
    )
    p.add_argument("--captures", default="./data/captures.jsonl", type=Path)
    p.add_argument(
        "--hydrations", default=settings.hydrations_path, type=Path,
    )
    p.add_argument(
        "--enrichments", default=settings.enrichments_path, type=Path,
    )
    p.add_argument(
        "--failures",
        default="./data/migration_failures.jsonl",
        type=Path,
        help="Where to log row-level validation failures (B.5.3).",
    )
    p.add_argument("--dry-run", action="store_true", help="Count, don't write.")
    p.add_argument("--verify", action="store_true",
                   help="Verify SQL row counts vs JSONL + spot-check.")
    p.add_argument("--include-test-rows", action="store_true",
                   help="Also migrate rows that look like test fixtures.")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap inserts per stage (debugging).")
    args = p.parse_args()

    rc = asyncio.run(run(
        captures_path=Path(args.captures),
        hydrations_path=Path(args.hydrations),
        enrichments_path=Path(args.enrichments),
        failures_path=Path(args.failures),
        dry_run=bool(args.dry_run),
        include_test_rows=bool(args.include_test_rows),
        limit=args.limit,
        verify_only=bool(args.verify),
    ))
    sys.exit(rc)


if __name__ == "__main__":
    main()
