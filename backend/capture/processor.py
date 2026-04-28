"""Capture processor — the Phase 1 orchestrator.

Takes a raw payload from the Chrome extension (or Telegram bot) and runs
it through the extraction pipeline:

    raw payload
        └─► text extractor (normalize + youtube transcript)
        └─► vision (describe each image via Claude Vision)
        └─► ProcessedContent (clean_text + image descriptions + metadata)

Phase 2 (enrichment + storage) will take ProcessedContent as its input.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from backend.capture.extractors import ExtractedText, extract
from backend.capture.vision import VisionResult, process_images


logger = logging.getLogger(__name__)


@dataclass
class CaptureInput:
    """Normalized view of whatever the extension/bot sent in."""

    url: str
    title: str
    platform: str
    content_type: str
    text: str
    images: list[str]
    timestamp: Optional[datetime]
    dwell_time_seconds: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProcessedContent:
    """Output of Phase 1 — ready for Phase 2 enrichment."""

    url: str
    title: str
    platform: str
    content_type: str
    clean_text: str
    text_source: str               # "extension" | "youtube_transcript" | "fallback"
    transcript: Optional[str]
    image_descriptions: list[dict] # flattened ImageDescription dicts
    image_text: str                # flattened text form of image content
    timestamp: str                 # ISO 8601
    dwell_time_seconds: int
    metadata: dict[str, Any]

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def combined_text(self) -> str:
        """Everything an LLM should see when enriching this capture."""
        parts = [self.clean_text]
        if self.transcript:
            parts.append(f"--- TRANSCRIPT ---\n{self.transcript}")
        if self.image_text:
            parts.append(f"--- IMAGES ---\n{self.image_text}")
        return "\n\n".join(p for p in parts if p).strip()


def process(capture: CaptureInput, *, skip_vision_api: bool = False) -> ProcessedContent:
    """Run a capture through the full Phase 1 pipeline.

    Args:
        capture: Normalized input from the extension or bot.
        skip_vision_api: Skip the Claude Vision call (still saves image
            bytes locally). Useful when ANTHROPIC_API_KEY isn't configured
            or when running offline tests.
    """
    logger.info(
        "Processing capture: platform=%s url=%s title=%.80s",
        capture.platform, capture.url, capture.title,
    )

    # --- Text extraction ---
    extracted: ExtractedText = extract(
        raw_text=capture.text,
        url=capture.url,
        platform=capture.platform,
    )

    # --- Vision ---
    vision: VisionResult = (
        process_images(capture.images, skip_api=skip_vision_api)
        if capture.images else VisionResult()
    )

    # --- Timestamp ---
    ts = capture.timestamp or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    return ProcessedContent(
        url=capture.url,
        title=capture.title,
        platform=capture.platform,
        content_type=capture.content_type,
        clean_text=extracted.clean_text,
        text_source=extracted.source,
        transcript=extracted.transcript,
        image_descriptions=[asdict(d) for d in vision.descriptions],
        image_text=vision.as_text(),
        timestamp=ts.isoformat(),
        dwell_time_seconds=capture.dwell_time_seconds,
        metadata=capture.metadata or {},
    )
