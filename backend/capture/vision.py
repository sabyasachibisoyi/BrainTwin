"""Vision processing — describe images/memes using Claude Vision.

Handles both image URLs (fetch bytes) and base64 data URLs (decode).
Saves image bytes to data/images/ and returns a structured description
so downstream enrichment can reason over images as if they were text.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from backend.config import settings


logger = logging.getLogger(__name__)


# --- Constants -------------------------------------------------------------

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # Claude Vision limit is ~5MB per image.
FETCH_TIMEOUT_S = 10.0

_DATA_URL_RE = re.compile(
    r"^data:image/(?P<mime>png|jpeg|jpg|gif|webp);base64,(?P<data>.+)$",
    re.IGNORECASE | re.DOTALL,
)


# --- Data types ------------------------------------------------------------


@dataclass
class ImageDescription:
    """What Claude saw in an image, plus where we stored the bytes."""

    local_path: Optional[str]  # path under data/images, or None if we couldn't save
    description: str            # free-text description from Claude Vision
    extracted_text: str = ""    # any text IN the image (OCR-ish)
    source_url: str = ""        # original URL or "<data-url>"
    error: Optional[str] = None # set if we couldn't process the image


@dataclass
class VisionResult:
    """Aggregate vision output for a capture."""

    descriptions: list[ImageDescription] = field(default_factory=list)

    def as_text(self) -> str:
        """Flatten image descriptions into text for enrichment prompts."""
        parts = []
        for i, desc in enumerate(self.descriptions, 1):
            if desc.error:
                continue
            block = f"[IMAGE {i}] {desc.description}"
            if desc.extracted_text:
                block += f"\n  Text in image: {desc.extracted_text}"
            parts.append(block)
        return "\n\n".join(parts)


# --- Image loading ---------------------------------------------------------


def _decode_data_url(data_url: str) -> Optional[tuple[bytes, str]]:
    """Decode a base64 data URL. Returns (bytes, mime) or None."""
    match = _DATA_URL_RE.match(data_url)
    if not match:
        return None
    try:
        payload = base64.b64decode(match.group("data"), validate=False)
    except (ValueError, base64.binascii.Error):
        return None
    mime = f"image/{match.group('mime').lower().replace('jpg', 'jpeg')}"
    return payload, mime


def _fetch_url(url: str) -> Optional[tuple[bytes, str]]:
    """Download an image. Returns (bytes, mime) or None."""
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        logger.warning("Failed to fetch image %s: %s", url, e)
        return None

    content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    if not content_type.startswith("image/"):
        logger.warning("Non-image content-type for %s: %s", url, content_type)
        return None

    data = resp.content
    if len(data) > MAX_IMAGE_BYTES:
        logger.warning("Image %s exceeds %d bytes, skipping", url, MAX_IMAGE_BYTES)
        return None

    return data, content_type


def load_image(src: str) -> Optional[tuple[bytes, str]]:
    """Load image bytes from either a URL or a base64 data URL."""
    if src.startswith("data:"):
        return _decode_data_url(src)
    if src.startswith(("http://", "https://")):
        return _fetch_url(src)
    return None


def save_image(data: bytes, mime: str) -> Optional[Path]:
    """Save image bytes to data/images/ using a content hash for dedup."""
    try:
        images_dir = Path(settings.images_path)
        images_dir.mkdir(parents=True, exist_ok=True)
        ext = mime.split("/")[-1]
        if ext == "jpeg":
            ext = "jpg"
        digest = hashlib.sha256(data).hexdigest()[:16]
        path = images_dir / f"{digest}.{ext}"
        if not path.exists():
            path.write_bytes(data)
        return path
    except OSError as e:
        logger.warning("Failed to save image: %s", e)
        return None


# --- Claude Vision call ----------------------------------------------------


_VISION_PROMPT = """Describe this image for a knowledge base. Respond in this format:

DESCRIPTION: One or two sentences on what the image shows — who, what, where, cultural context (e.g., Bollywood meme, Indian political cartoon, tech product screenshot).

TEXT_IN_IMAGE: Any visible text, captions, or dialogue in the image. Write "none" if there's no text.

Keep it concise. No preamble."""


def _parse_vision_response(text: str) -> tuple[str, str]:
    """Split Claude's response into (description, extracted_text)."""
    description = ""
    extracted = ""

    # Simple section parsing — tolerant of minor format drift.
    lines = text.strip().splitlines()
    current = None
    buffers: dict[str, list[str]] = {"DESCRIPTION": [], "TEXT_IN_IMAGE": []}

    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("DESCRIPTION:"):
            current = "DESCRIPTION"
            rest = stripped.split(":", 1)[1].strip()
            if rest:
                buffers[current].append(rest)
        elif stripped.upper().startswith("TEXT_IN_IMAGE:"):
            current = "TEXT_IN_IMAGE"
            rest = stripped.split(":", 1)[1].strip()
            if rest:
                buffers[current].append(rest)
        elif current and stripped:
            buffers[current].append(stripped)

    description = " ".join(buffers["DESCRIPTION"]).strip()
    extracted = " ".join(buffers["TEXT_IN_IMAGE"]).strip()
    if extracted.lower() == "none":
        extracted = ""

    # Fallback: if parsing failed entirely, just use the whole text.
    if not description and text.strip():
        description = text.strip()

    return description, extracted


def describe_image(data: bytes, mime: str) -> tuple[str, str]:
    """Call Claude Vision. Returns (description, extracted_text).

    Raises on API errors so the caller can decide whether to swallow.
    """
    try:
        from anthropic import Anthropic
    except ImportError as e:
        raise RuntimeError("anthropic SDK not installed") from e

    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env")

    client = Anthropic(api_key=settings.anthropic_api_key)
    b64 = base64.b64encode(data).decode("ascii")

    response = client.messages.create(
        model=settings.enrichment_model,  # Haiku is plenty for image describe.
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": b64},
                    },
                    {"type": "text", "text": _VISION_PROMPT},
                ],
            }
        ],
    )

    text = response.content[0].text if response.content else ""
    return _parse_vision_response(text)


# --- Public API ------------------------------------------------------------


def process_images(image_srcs: list[str], *, skip_api: bool = False) -> VisionResult:
    """Process a list of image sources (URLs or data URLs).

    Args:
        image_srcs: list of http(s) URLs or base64 data URLs
        skip_api: if True, save bytes but skip the Claude Vision call
            (useful for tests and for running without an API key)
    """
    result = VisionResult()

    for src in image_srcs:
        loaded = load_image(src)
        if loaded is None:
            result.descriptions.append(
                ImageDescription(
                    local_path=None,
                    description="",
                    source_url=src if not src.startswith("data:") else "<data-url>",
                    error="failed to load image",
                )
            )
            continue

        data, mime = loaded
        saved_path = save_image(data, mime)

        if skip_api:
            result.descriptions.append(
                ImageDescription(
                    local_path=str(saved_path) if saved_path else None,
                    description="[vision API skipped]",
                    source_url=src if not src.startswith("data:") else "<data-url>",
                )
            )
            continue

        try:
            description, extracted = describe_image(data, mime)
            result.descriptions.append(
                ImageDescription(
                    local_path=str(saved_path) if saved_path else None,
                    description=description,
                    extracted_text=extracted,
                    source_url=src if not src.startswith("data:") else "<data-url>",
                )
            )
        except Exception as e:
            logger.warning("Vision API failed for %s: %s", src[:80], e)
            result.descriptions.append(
                ImageDescription(
                    local_path=str(saved_path) if saved_path else None,
                    description="",
                    source_url=src if not src.startswith("data:") else "<data-url>",
                    error=f"vision api error: {e}",
                )
            )

    return result
