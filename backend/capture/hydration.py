"""Capture hydration orchestrator — Phase 2.5 Fixes 2 + 3.

Decides whether a `ProcessedContent` row needs more content before
enrichment can do anything useful, and (when it does) fills it in from
the cheapest available sources.

Tier order (locked after Fix 2.B cancellation, see
docs/phase2.5-capture-hydration.md):

    1. Use raw_text if non-empty                                      ← Phase 1, processor.py
    2. Else fetch OG metadata from URL                                ← Fix 2.A (this file)
    3. Else if video URL → yt-dlp + whisper.cpp local transcription   ← Fix 3 (this file)
    4. Else mark phase: "enrichment_skipped"                          ← Fix 1

For video URLs we run BOTH OG and transcription and merge the outputs:
the OG description / post caption becomes the title context, and the
spoken transcript becomes `clean_text` (per Fix 3 sign-off 2026-04-29).
The two layers complement each other — an Instagram reel's caption
tells you what the post is about; the transcript tells you what was
said. Both are valuable signal.

Sidecar JSONL: one row per hydrated capture in `data/hydrations.jsonl`,
joined to `captures.jsonl` and `enrichments.jsonl` by `capture_id`.
The `tier` field tags the dominant source (`og_metadata` /
`video_transcript`); `tiers_used` lists every layer that contributed,
so future debugging knows exactly where each character came from.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from backend.capture.og_fetcher import OGMetadata, fetch_og_metadata
from backend.capture.processor import ProcessedContent
from backend.capture.video_transcriber import (
    TranscriptionResult,
    TranscriptionSkipped,
    is_video_url,
    transcribe_video,
)
from backend.config import settings


logger = logging.getLogger(__name__)


# Type aliases for the injection points — keeps the orchestrator
# trivially mockable in tests without monkeypatching network code.
OGFetcher = Callable[[str], Awaitable[Optional[OGMetadata]]]
VideoTranscriber = Callable[
    [str], Awaitable["TranscriptionResult | TranscriptionSkipped | None"]
]


# Title placeholders the bot/extension assign when they have nothing
# better. We treat these as "title was missing" so a real title from
# OG / video metadata can replace them rather than being recorded as
# a no-op title overwrite.
_PLACEHOLDER_TITLES = frozenset({
    "",
    "telegram link",
    "untitled",
    "no title",
})


@dataclass(frozen=True)
class HydrationResult:
    """What the orchestrator decided.

    `processed` is always non-None — either the input unchanged (when
    no hydration was needed or possible) or a new `ProcessedContent`
    with hydrated fields. `record` is the JSONL row to persist when
    hydration actually fired (None when nothing happened).
    """

    processed: ProcessedContent
    record: Optional[dict[str, Any]] = None

    @property
    def hydrated(self) -> bool:
        return self.record is not None


# ---- Helpers ----------------------------------------------------------

def _needs_hydration(processed: ProcessedContent) -> bool:
    """A capture needs hydration when there's nothing for the LLM to chew
    on AND we have a URL we could fetch.

    Mirrors the `EmptyContentError` precondition in
    `enrichment.py:_combined_text` — if `combined_text` is non-empty we
    have something already, hydration is a no-op."""
    if not (processed.url or "").startswith(("http://", "https://")):
        return False
    if processed.combined_text.strip():
        return False
    return True


def _is_placeholder_title(title: Optional[str]) -> bool:
    return (title or "").strip().lower() in _PLACEHOLDER_TITLES


async def _safe_call_og(
    log_prefix: str,
    url: str,
    fetcher: OGFetcher,
) -> Optional[OGMetadata]:
    """Wrap the OG fetch in a try/except so a buggy fetcher can't kill
    enrichment. Returns None on any failure."""
    try:
        return await fetcher(url)
    except Exception as e:  # noqa: BLE001
        logger.warning("%s OG fetcher raised unexpectedly: %s", log_prefix, e)
        return None


async def _safe_call_transcriber(
    log_prefix: str,
    url: str,
    transcriber: VideoTranscriber,
) -> "TranscriptionResult | TranscriptionSkipped | None":
    """Same belt-and-braces pattern as `_safe_call_og`. The transcriber
    promises not to raise but a yt-dlp version bump or a full disk
    could change that — we'd rather log and fall through than hand the
    BackgroundTask an uncaught exception (it'd silently die)."""
    try:
        return await transcriber(url)
    except Exception as e:  # noqa: BLE001
        logger.warning("%s video transcriber raised unexpectedly: %s", log_prefix, e)
        return None


def _build_record(
    *,
    capture_id: str,
    processed: ProcessedContent,
    new_clean_text: str,
    new_title: Optional[str],
    title_replaced: bool,
    og_meta: Optional[OGMetadata],
    transcription: Optional[TranscriptionResult],
    skipped: Optional[TranscriptionSkipped],
) -> dict[str, Any]:
    """Sidecar row shape — one row per hydrated capture, joined by
    `capture_id`. Records every layer that contributed.

    `tier` is the dominant content source (the one whose text became
    `clean_text`); `tiers_used` lists all layers that fired so a
    debugger can reconstruct exactly what happened.
    """
    tiers_used: list[str] = []
    if og_meta and og_meta.is_useful():
        tiers_used.append("og_metadata")
    if transcription is not None:
        tiers_used.append("video_transcript")
    primary_tier = (
        "video_transcript" if transcription is not None
        else "og_metadata" if og_meta and og_meta.is_useful()
        else "unknown"
    )

    row: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "capture_id": capture_id,
        "url": processed.url,
        "tier": primary_tier,
        "tiers_used": tiers_used,
        "title_before": processed.title,
        "title_after": new_title,
        "title_replaced": title_replaced,
        "clean_text_before_chars": len(processed.clean_text or ""),
        "clean_text_after_chars": len(new_clean_text),
    }
    # Only record OG when it actually contributed (had a useful
    # description). A title-only OG result counts as "we tried, got
    # nothing" — `tiers_used` already excludes it; recording it here
    # would be misleading noise in the sidecar.
    if og_meta is not None and og_meta.is_useful():
        row["og"] = {
            "source": og_meta.source,
            "image_url": og_meta.image_url,
            "site_name": og_meta.site_name,
            "description_chars": len(og_meta.description or ""),
        }
    if transcription is not None:
        row["transcript"] = {
            "duration_seconds": transcription.duration_seconds,
            "extractor": transcription.extractor,
            "title": transcription.title,
            "chars": len(transcription.transcript or ""),
        }
    if skipped is not None:
        row["transcript_skipped"] = {
            "reason": skipped.reason,
            "duration_seconds": skipped.duration_seconds,
        }
    return row


def _merge_clean_text(
    og_meta: Optional[OGMetadata],
    transcription: Optional[TranscriptionResult],
) -> str:
    """Build the merged `clean_text` an LLM should see.

    Priority:
      - If we have a transcript, it's the dominant signal — what was
        actually SAID in the video. Prepend the OG description (caption)
        as labeled context so the LLM can use both.
      - Otherwise, OG description alone.
    """
    if transcription is not None and transcription.transcript:
        parts: list[str] = []
        if og_meta and og_meta.description and og_meta.description.strip():
            parts.append(f"--- POST CAPTION ---\n{og_meta.description.strip()}")
        parts.append(f"--- TRANSCRIPT ---\n{transcription.transcript.strip()}")
        return "\n\n".join(parts)
    if og_meta and og_meta.is_useful():
        return (og_meta.description or "").strip()
    return ""


def _pick_title(
    processed_title: str,
    og_meta: Optional[OGMetadata],
    transcription: Optional[TranscriptionResult],
) -> tuple[Optional[str], bool]:
    """Pick the best replacement title and whether we replaced one.

    Order of preference: OG title → yt-dlp video title → existing title.
    Only fires when the existing title is a known placeholder.
    """
    if not _is_placeholder_title(processed_title):
        return processed_title, False
    candidate: Optional[str] = None
    if og_meta and og_meta.title:
        candidate = og_meta.title.strip()
    elif transcription and transcription.title:
        candidate = transcription.title.strip()
    if not candidate:
        return processed_title, False
    return candidate, True


# ---- Entry point ------------------------------------------------------

async def hydrate_processed(
    capture_id: str,
    processed: ProcessedContent,
    *,
    fetcher: Optional[OGFetcher] = None,
    transcriber: Optional[VideoTranscriber] = None,
) -> HydrationResult:
    """Run the hydration pipeline. Returns a `HydrationResult` that
    `enqueue_enrichment` consumes.

    `fetcher` and `transcriber` default to `None` (resolved at call-time
    to the current module-level `fetch_og_metadata` / `transcribe_video`)
    so tests can `monkeypatch.setattr` on this module to swap in stubs
    without touching the network. Binding the defaults in the signature
    would freeze the originals at import time and defeat the patch.

    Best-effort: any failure inside the fetcher or transcriber returns
    the input unchanged. We never raise — the enrichment worker treats
    hydration as advisory."""
    if not _needs_hydration(processed):
        return HydrationResult(processed=processed)

    log_prefix = f"hydrate[{capture_id[:8]}]"

    # Resolve injection points at call time so monkeypatch works.
    active_fetcher: OGFetcher = fetcher or fetch_og_metadata
    active_transcriber: VideoTranscriber = transcriber or transcribe_video

    # ---- Tier 2 — OG fetch (always runs when enabled) ---------------
    og_meta: Optional[OGMetadata] = None
    if settings.og_fetch_enabled:
        og_meta = await _safe_call_og(log_prefix, processed.url, active_fetcher)

    # ---- Tier 3 — video transcription (only on video URLs) ----------
    transcription: Optional[TranscriptionResult] = None
    skipped: Optional[TranscriptionSkipped] = None
    if (
        settings.video_transcribe_enabled
        and is_video_url(processed.url, processed.platform)
    ):
        outcome = await _safe_call_transcriber(
            log_prefix, processed.url, active_transcriber,
        )
        if isinstance(outcome, TranscriptionResult):
            transcription = outcome
        elif isinstance(outcome, TranscriptionSkipped):
            skipped = outcome
            logger.info(
                "%s video transcription skipped: reason=%s duration=%s",
                log_prefix, outcome.reason, outcome.duration_seconds,
            )
        # outcome=None → yt-dlp couldn't extract; OG-only fallback below.

    # ---- Did anything contribute? ----------------------------------
    have_og = og_meta is not None and og_meta.is_useful()
    have_transcript = transcription is not None
    if not have_og and not have_transcript:
        if og_meta is None:
            logger.info("%s no OG and no transcript — falling through to skip", log_prefix)
        else:
            logger.info("%s OG returned title-only and no transcript — falling through to skip", log_prefix)
        return HydrationResult(processed=processed)

    # ---- Merge -----------------------------------------------------
    new_clean_text = _merge_clean_text(og_meta, transcription)
    new_title, title_replaced = _pick_title(processed.title, og_meta, transcription)

    # text_source labels the dominant content layer for downstream
    # consumers that just want a one-word provenance hint.
    text_source = "video_transcript" if have_transcript else "og_metadata"

    hydrated_processed = replace(
        processed,
        clean_text=new_clean_text,
        text_source=text_source,
        title=new_title or processed.title,
        # Carry the transcript on the dataclass too — consumers that want
        # just the spoken-word content (Phase 5 quiz layer) can find it
        # here without re-parsing clean_text.
        transcript=transcription.transcript if have_transcript else processed.transcript,
    )

    record = _build_record(
        capture_id=capture_id,
        processed=processed,
        new_clean_text=new_clean_text,
        new_title=new_title,
        title_replaced=title_replaced,
        og_meta=og_meta,
        transcription=transcription,
        skipped=skipped,
    )
    logger.info(
        "%s hydrated tier=%s tiers_used=%s title_replaced=%s clean_text_chars=%d",
        log_prefix, record["tier"], record["tiers_used"],
        title_replaced, len(new_clean_text),
    )
    return HydrationResult(processed=hydrated_processed, record=record)
