"""Async enrichment worker.

Wraps the pure `enrich()` call with retry policy, sidecar JSONL
persistence, and structured failure logging. This is the layer that
FastAPI's `BackgroundTasks` schedules from `/capture` (Decision H —
async, never block the capture path).

Design notes (per docs/phase2-design.md Decision H):
  - 3 retries on transient errors with 0.5s / 1s / 2s exponential backoff.
  - The single MalformedResponseError retry is handled inside `enrich()`
    via RETRY_REMINDER, so by the time we see one here it's already
    been re-tried once and we give up.
  - Permanent errors (auth, content-too-long) are NEVER retried.
  - All paths are best-effort: this worker must not raise out — its
    caller is BackgroundTasks which would just swallow exceptions and
    log nothing useful. We catch broadly at the boundary and route to
    the failure log so `/failures` surfaces the problem.

Sidecar files written:
  - data/enrichments.jsonl       on success (one row per capture_id)
  - data/capture_failures.jsonl  with one of two phase tags
      - phase: "enrichment"          → real failure (network, auth,
        malformed JSON after retry, schema, transient_exhausted, etc.).
        Surfaced in `/failures` and bot `/failures` by default.
      - phase: "enrichment_skipped"  → "nothing to enrich" cases
        (empty content, oversized content). Phase 2.5 hygiene Fix 1 —
        these aren't really failures, they're not-applicable. Excluded
        from `/failures` and bot `/failures` by default; surfaced via
        `?phase=enrichment_skipped` on demand.

Crash recovery: `find_unenriched_capture_ids()` scans the JSONLs and
returns capture_ids in captures.jsonl that have no matching row in
enrichments.jsonl AND haven't been tagged enrichment_skipped (otherwise
the startup hook would re-call Haiku on the same empty content every
boot). FastAPI startup hook calls this to re-queue work that was
in-flight when the process died. The same function backs
`scripts/retry_failed_enrichments.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import fields as dc_fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from backend.capture.hydration import HydrationResult, hydrate_processed as hydrate_capture
from backend.capture.processor import ProcessedContent
from backend.config import settings
from backend.knowledge.enrichment import (
    ContentTooLongError,
    EmptyContentError,
    SchemaError,
    enrich,
    wrap_enrichment_record,
)
from backend.knowledge.llm_client import (
    LLMClient,
    MalformedResponseError,
    PermanentLLMError,
    TransientLLMError,
)
from backend.storage.sync import sync_enrichment, sync_hydration


logger = logging.getLogger(__name__)


# Backoff schedule for transient retries. 4 attempts total = 1 initial
# + 3 retries. Total worst case: ~3.5s of sleeps, well under the 30s
# implicit budget we want for a background task.
TRANSIENT_BACKOFFS_SECONDS: tuple[float, ...] = (0.5, 1.0, 2.0)


# ---- Persistence -----------------------------------------------------

def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    """Append a row as one JSON line. Best-effort; logs and swallows
    OSError so a disk hiccup doesn't propagate into BackgroundTasks."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        logger.exception("Failed to append to %s", path)


def _build_enrichment_log_row(
    *,
    phase: str,
    capture_id: str,
    processed: ProcessedContent,
    reason: str,
) -> dict[str, Any]:
    """Shared row shape for both enrichment failures and skips.

    Phase 2.5 Fix 1: split the failure log into two phases — `enrichment`
    for real failures and `enrichment_skipped` for not-applicable cases
    (empty / oversized content). Same row shape so consumers (`/failures`,
    bot `/failures`, future digest agent) can render both with one
    template.
    """
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "capture_id": capture_id,
        "source": "enrichment_worker",
        "url": processed.url,
        "title": processed.title,
        "platform": processed.platform,
        "reason": reason,
        "text_preview": (processed.combined_text or "")[:200],
    }


def _log_enrichment_failure(
    *,
    capture_id: str,
    processed: ProcessedContent,
    reason: str,
) -> None:
    """Mirror of `_log_failure` in main.py but with `phase: "enrichment"`
    so the existing `/failures` endpoint and the bot's `/failures`
    command can group/filter (Decision C). Reserved for *real* failures
    only — empty / oversized content goes to `_log_enrichment_skipped`
    (Phase 2.5 Fix 1)."""
    row = _build_enrichment_log_row(
        phase="enrichment",
        capture_id=capture_id,
        processed=processed,
        reason=reason,
    )
    _append_jsonl(Path(settings.capture_failures_path), row)


def _log_enrichment_skipped(
    *,
    capture_id: str,
    processed: ProcessedContent,
    reason: str,
) -> None:
    """Phase 2.5 Fix 1 — log a "nothing to enrich" case as
    `phase: "enrichment_skipped"` so it doesn't pollute the real failure
    metric. Same row shape as a failure; consumers filter by phase."""
    row = _build_enrichment_log_row(
        phase="enrichment_skipped",
        capture_id=capture_id,
        processed=processed,
        reason=reason,
    )
    _append_jsonl(Path(settings.capture_failures_path), row)


def _persist_enrichment(*, capture_id: str, enrichment: dict[str, Any]) -> None:
    """Write a successful enrichment to data/enrichments.jsonl."""
    record = wrap_enrichment_record(capture_id=capture_id, enrichment=enrichment)
    _append_jsonl(Path(settings.enrichments_path), record)


def _persist_hydration(record: dict[str, Any]) -> None:
    """Phase 2.5 Fix 2 — write a hydration sidecar row.

    Original captures.jsonl row stays untouched (audit trail of what
    arrived from the client). The sidecar pattern matches enrichments.jsonl
    so consumers can join by `capture_id`."""
    _append_jsonl(Path(settings.hydrations_path), record)


# ---- Worker entry point ---------------------------------------------

async def enqueue_enrichment(
    capture_id: str,
    processed: ProcessedContent,
    client: LLMClient,
) -> None:
    """Enrich a capture with retries, persist or log-failure.

    Designed to be the function FastAPI's `BackgroundTasks.add_task`
    calls. It MUST NOT raise — anything that escapes here is lost,
    because BackgroundTasks doesn't surface exceptions anywhere visible.

    Decision H retry policy:
      - Transient (network / 5xx / rate-limit): up to 4 attempts total
        with 0.5s / 1s / 2s backoff between them.
      - MalformedResponse: already retried once inside enrich() with
        RETRY_REMINDER; if we see it here, give up and log.
      - Permanent (auth / 4xx / content-too-long): no retry, log immediately.
      - Skips (empty content / oversized): log with descriptive reason,
        no API call wasted.
    """
    log_prefix = f"enrich[{capture_id[:8]}]"

    # Phase 2.5 Fix 2.A — try to hydrate empty captures from OG metadata
    # before we hand them to enrich(). This is what turns a forwarded
    # IG/FB URL (which arrives as `clean_text=""`) into something the
    # LLM can summarise. No-op when the capture already has content,
    # when the URL has no usable OG tags, or when og_fetch_enabled=False.
    try:
        hydration: HydrationResult = await hydrate_capture(capture_id, processed)
    except Exception as e:  # noqa: BLE001
        # Defensive: hydrate_capture promises not to raise, but a bug
        # there must not kill enrichment. Log and continue with the
        # un-hydrated row — worst case we hit EmptyContentError below
        # and route to enrichment_skipped, same as before Fix 2.
        logger.warning("%s hydration raised unexpectedly: %s", log_prefix, e)
        hydration = HydrationResult(processed=processed)

    if hydration.hydrated and hydration.record is not None:
        _persist_hydration(hydration.record)
        # Phase 3 Step 4b — dual-write the hydration row into SQL.
        # Best-effort: sync_hydration catches all errors internally.
        await sync_hydration(
            capture_id=capture_id,
            tier=hydration.record.get("tier", "unknown"),
            source_payload_json=json.dumps(hydration.record, ensure_ascii=False),
            hydrated_at=hydration.record.get(
                "timestamp",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    processed = hydration.processed

    last_transient: TransientLLMError | None = None

    # Attempt 0 + len(TRANSIENT_BACKOFFS) retries = 4 total attempts.
    for attempt in range(len(TRANSIENT_BACKOFFS_SECONDS) + 1):
        try:
            enrichment = await enrich(processed, client=client)

        except EmptyContentError as e:
            # Phase 2.5 Fix 1 — not a failure, just nothing to do.
            logger.info("%s skipped — empty content: %s", log_prefix, e)
            _log_enrichment_skipped(
                capture_id=capture_id,
                processed=processed,
                reason="empty_content",
            )
            return

        except ContentTooLongError as e:
            # Phase 2.5 Fix 1 — same hygiene treatment as empty content.
            logger.warning("%s skipped — content too long: %s", log_prefix, e)
            _log_enrichment_skipped(
                capture_id=capture_id,
                processed=processed,
                reason=f"content_too_long: {e}",
            )
            return

        except TransientLLMError as e:
            last_transient = e
            if attempt < len(TRANSIENT_BACKOFFS_SECONDS):
                delay = TRANSIENT_BACKOFFS_SECONDS[attempt]
                logger.warning(
                    "%s transient error on attempt %d (%s) — retrying in %ss",
                    log_prefix, attempt + 1, e, delay,
                )
                await asyncio.sleep(delay)
                continue
            # Exhausted retries.
            logger.error(
                "%s transient error after %d attempts: %s",
                log_prefix, attempt + 1, e,
            )
            _log_enrichment_failure(
                capture_id=capture_id,
                processed=processed,
                reason=f"transient_exhausted: {e}",
            )
            return

        except PermanentLLMError as e:
            logger.error("%s permanent LLM error — not retrying: %s", log_prefix, e)
            _log_enrichment_failure(
                capture_id=capture_id,
                processed=processed,
                reason=f"permanent: {e}",
            )
            return

        except MalformedResponseError as e:
            # enrich() already retried once with RETRY_REMINDER.
            logger.error("%s malformed JSON after retry: %s", log_prefix, e)
            _log_enrichment_failure(
                capture_id=capture_id,
                processed=processed,
                reason=f"malformed_json: {e}",
            )
            return

        except SchemaError as e:
            logger.error("%s schema validation failed: %s", log_prefix, e)
            _log_enrichment_failure(
                capture_id=capture_id,
                processed=processed,
                reason=f"schema: {e}",
            )
            return

        except Exception as e:  # noqa: BLE001
            # Last-resort safety net — a bug here would silently kill
            # enrichment for everything until restart.
            logger.exception("%s unexpected error: %s", log_prefix, e)
            _log_enrichment_failure(
                capture_id=capture_id,
                processed=processed,
                reason=f"unexpected: {type(e).__name__}: {e}",
            )
            return

        # Success path — break out of retry loop.
        _persist_enrichment(capture_id=capture_id, enrichment=enrichment)
        # Phase 3 Step 4b — dual-write the enrichment + derived chunks
        # / topics / entities into SQL + Chroma. Best-effort: any
        # SQL/Chroma failure is caught inside sync_enrichment, logged,
        # and swallowed so the JSONL path stays unaffected.
        #
        # TODO(Phase 5+): when fallback models are wired (e.g.
        # haiku → sonnet on transient errors), record the model that
        # actually responded — `settings.enrichment_model` is the
        # configured primary, not necessarily the one that ran.
        await sync_enrichment(
            capture_id=capture_id,
            summary=enrichment.get("summary"),
            key_facts_json=json.dumps(
                enrichment.get("key_facts") or [], ensure_ascii=False,
            ),
            topics=enrichment.get("topics") or [],
            entities=enrichment.get("entities") or [],
            model=settings.enrichment_model,
            enriched_at=datetime.now(timezone.utc).isoformat(),
            processed=processed,
        )
        logger.info(
            "%s enriched (%d entities, %d facts, %d topics)",
            log_prefix,
            len(enrichment.get("entities") or []),
            len(enrichment.get("key_facts") or []),
            len(enrichment.get("topics") or []),
        )
        return

    # Defensive: shouldn't reach here, the loop returns on every path.
    if last_transient is not None:
        _log_enrichment_failure(
            capture_id=capture_id,
            processed=processed,
            reason=f"transient_exhausted: {last_transient}",
        )


# ---- Hydration (shared by main.py startup + scripts) ----------------

# Defaults used when an old pre-Phase-2 row is missing fields the
# `ProcessedContent` dataclass now expects. These match the dataclass
# default values where they have one.
_HYDRATION_DEFAULTS: dict[str, Any] = {
    "transcript": None,
    "image_descriptions": [],
    "image_text": "",
    "metadata": {},
    "dwell_time_seconds": 0,
}


def hydrate_processed(row: dict[str, Any]) -> ProcessedContent | None:
    """Reconstruct a `ProcessedContent` from a captures.jsonl row.

    Returns None if required fields are still missing after applying
    defaults — caller should skip those rows. Used by:
      - main.py startup recovery scan
      - scripts/backfill_enrichment.py
      - scripts/retry_failed_enrichments.py

    Single source of truth so the three call sites can't drift.
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


# ---- Crash recovery / manual catch-up -------------------------------

def _read_jsonl_field(path: Path, field: str) -> Iterable[str]:
    """Stream values of one field from a JSONL file, skipping bad rows."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            v = row.get(field)
            if isinstance(v, str) and v:
                yield v


def _read_jsonl_field_where(
    path: Path, field: str, *, where_field: str, where_value: str
) -> Iterable[str]:
    """Stream values of `field` from rows where `where_field == where_value`.

    Phase 2.5 Fix 1 — used to pull capture_ids tagged as
    `enrichment_skipped` from the failures log so the startup recovery
    scan can skip them (otherwise the hook would re-call Haiku on the
    same empty content every boot)."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get(where_field) != where_value:
                continue
            v = row.get(field)
            if isinstance(v, str) and v:
                yield v


def find_unenriched_capture_ids(
    *,
    captures_path: Path | None = None,
    enrichments_path: Path | None = None,
    failures_path: Path | None = None,
) -> list[str]:
    """Return capture_ids in captures.jsonl with no row in enrichments.jsonl
    AND no row in capture_failures.jsonl tagged `phase: "enrichment_skipped"`.

    Used both by the FastAPI startup hook (to re-queue work that was
    in-flight when the previous process died) and by
    `scripts/retry_failed_enrichments.py` (manual on-demand catch-up).

    Phase 2.5 Fix 1: also exclude IDs that were already evaluated and
    intentionally skipped (empty / oversized content). Without this, every
    process restart would re-call Haiku on the same hopeless rows.

    Order is preserved (oldest unenriched first) so retries process
    in the same order the captures arrived.
    """
    cp = captures_path or Path("./data/captures.jsonl")
    ep = enrichments_path or Path(settings.enrichments_path)
    fp = failures_path or Path(settings.capture_failures_path)

    enriched_ids = set(_read_jsonl_field(ep, "capture_id"))
    skipped_ids = set(
        _read_jsonl_field_where(
            fp, "capture_id", where_field="phase", where_value="enrichment_skipped"
        )
    )
    excluded = enriched_ids | skipped_ids

    unenriched: list[str] = []
    seen: set[str] = set()
    for cid in _read_jsonl_field(cp, "capture_id"):
        if cid in excluded or cid in seen:
            continue
        seen.add(cid)
        unenriched.append(cid)
    return unenriched
