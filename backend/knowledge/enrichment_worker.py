"""Async enrichment worker.

Wraps the pure `enrich()` call with retry policy, SQL persistence, and
structured failure logging. This is the layer that FastAPI's
`BackgroundTasks` schedules from `/capture` (Decision H — async, never
block the capture path).

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

Persistence (Phase 3.5):
  - Successful enrichment → SQL via `sync_enrichment` (enrichment row +
    chunks + topics + entities + Chroma vectors). The old
    `enrichments.jsonl` writer was retired with the cutover.
  - Successful hydration  → SQL via `sync_hydration`. The old
    `hydrations.jsonl` writer was retired with the cutover.
  - Failures and skips    → `data/capture_failures.jsonl` with one of
    two phase tags. This log is intentionally NOT a SQL table — it's
    an operational record (see docs/phase3.5-cutover.md, decision 2).
      - phase: "enrichment"          → real failure (network, auth,
        malformed JSON after retry, schema, transient_exhausted, etc.).
        Surfaced in `/failures` and bot `/failures` by default.
      - phase: "enrichment_skipped"  → "nothing to enrich" cases
        (empty content, oversized content). Phase 2.5 hygiene Fix 1 —
        these aren't really failures, they're not-applicable. Excluded
        from `/failures` and bot `/failures` by default; surfaced via
        `?phase=enrichment_skipped` on demand.

Crash recovery: `iter_unenriched_captures()` runs a SQL query
(`captures LEFT JOIN enrichments`) for the default user and yields
each unenriched capture as a domain object. The skipped set still
comes from the failures log (no SQL counterpart) so the startup hook
doesn't re-call Haiku on the same empty content every boot. The same
helper backs `scripts/retry_failed_enrichments.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from backend.capture.hydration import HydrationResult, hydrate_processed as hydrate_capture
from backend.capture.processor import ProcessedContent
from backend.config import settings
from backend.knowledge.enrichment import (
    ContentTooLongError,
    EmptyContentError,
    SchemaError,
    enrich,
)
from backend.knowledge.llm_client import (
    LLMClient,
    MalformedResponseError,
    PermanentLLMError,
    TransientLLMError,
)
from backend.storage import (
    Capture,
    CaptureRepository,
    DEFAULT_USER_ID,
    session_scope,
)
from backend.storage.sync import sync_enrichment, sync_hydration


logger = logging.getLogger(__name__)


# Backoff schedule for transient retries. 4 attempts total = 1 initial
# + 3 retries. Total worst case: ~3.5s of sleeps, well under the 30s
# implicit budget we want for a background task.
TRANSIENT_BACKOFFS_SECONDS: tuple[float, ...] = (0.5, 1.0, 2.0)


# ---- Persistence -----------------------------------------------------
#
# Post-Phase-3.5: the only JSONL we still write to is `capture_failures.jsonl`,
# which is intentionally NOT a SQL table — it's an operational log
# (see docs/phase3.5-cutover.md, decision 2). Successful enrichments
# and hydrations go to SQL via `sync_enrichment` / `sync_hydration`
# inside the worker entry point below.

def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    """Append a row as one JSON line. Best-effort; logs and swallows
    OSError so a disk hiccup doesn't propagate into BackgroundTasks.

    Only used for the operational failures log; the knowledge JSONLs
    (captures, enrichments, hydrations) were retired in Phase 3.5."""
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
        # Phase 3.5 — hydration is SQL-only. Best-effort: sync_hydration
        # catches all errors internally and a failure here doesn't block
        # the enrichment that follows.
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
        # Phase 3.5 — enrichment is SQL-only. sync_enrichment writes
        # the enrichment row plus derived chunks / topics / entities
        # into SQL + Chroma. Errors are caught and logged inside;
        # nothing else persists the result.
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


# ---- Rebuilding ProcessedContent from a SQL Capture row ------------

def hydrate_processed_from_capture(capture: Capture) -> ProcessedContent | None:
    """Reconstruct a `ProcessedContent` from a `Capture` SQL row.

    Phase 3.5 replacement for the old `hydrate_processed(row: dict)`
    that consumed a captures.jsonl row. The new content columns
    (`clean_text`, `transcript`, `image_text`, `image_descriptions_json`,
    `text_source`) carry everything the enrichment worker needs.

    Returns None if the row has neither text nor transcript nor image
    text — there's nothing for the LLM to enrich and the caller should
    skip it. (Mirrors the old "missing required fields → skip" path.)
    """
    clean_text = capture.clean_text or ""
    transcript = capture.transcript
    image_text = capture.image_text or ""
    if not clean_text and not transcript and not image_text:
        return None

    try:
        image_descriptions = (
            json.loads(capture.image_descriptions_json)
            if capture.image_descriptions_json else []
        )
    except json.JSONDecodeError:
        image_descriptions = []

    try:
        metadata = json.loads(capture.raw_metadata_json) if capture.raw_metadata_json else {}
    except json.JSONDecodeError:
        metadata = {}

    try:
        return ProcessedContent(
            url=capture.url or "",
            title=capture.title or "",
            platform=capture.platform or "general",
            content_type=capture.content_type or "article",
            clean_text=clean_text,
            text_source=capture.text_source or "extension",
            transcript=transcript,
            image_descriptions=image_descriptions,
            image_text=image_text,
            timestamp=capture.captured_at,
            dwell_time_seconds=capture.dwell_seconds,
            metadata=metadata if isinstance(metadata, dict) else {},
        )
    except TypeError:
        return None


# ---- Crash recovery / manual catch-up -------------------------------

def _read_jsonl_field_where(
    path: Path, field: str, *, where_field: str, where_value: str
):
    """Stream values of `field` from rows where `where_field == where_value`.

    Phase 2.5 Fix 1 — used to pull capture_ids tagged as
    `enrichment_skipped` from the failures log so the startup recovery
    scan can skip them (otherwise the hook would re-call Haiku on the
    same empty content every boot).

    Kept post-3.5 because `capture_failures.jsonl` is intentionally NOT
    a SQL table (see docs/phase3.5-cutover.md, decision 2)."""
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


def _load_skipped_capture_ids(failures_path: Path | None = None) -> set[str]:
    """Set of capture_ids tagged `phase: enrichment_skipped` in the
    failures log. Used to exclude "nothing to enrich" rows from the
    recovery sweep."""
    fp = failures_path or Path(settings.capture_failures_path)
    return set(
        _read_jsonl_field_where(
            fp, "capture_id",
            where_field="phase", where_value="enrichment_skipped",
        )
    )


async def iter_unenriched_captures(
    *,
    user_id: int = DEFAULT_USER_ID,
    failures_path: Path | None = None,
) -> AsyncIterator[Capture]:
    """Yield each `Capture` that has no enrichment row, in capture
    order (oldest first), excluding any tagged `enrichment_skipped`
    in the failures log.

    Phase 3.5 replacement for the JSONL-scanning
    `find_unenriched_capture_ids` + the captures.jsonl re-hydration
    loop. Used by:
      - the FastAPI startup hook (to re-queue work that was in-flight
        when the previous process died)
      - `scripts/retry_failed_enrichments.py` (manual catch-up)
    """
    skipped_ids = _load_skipped_capture_ids(failures_path)
    async with session_scope() as session:
        cap_repo = CaptureRepository(session)
        rows = await cap_repo.unenriched(
            user_id=user_id,
            exclude_capture_ids=skipped_ids,
        )
    for cap in rows:
        yield cap
