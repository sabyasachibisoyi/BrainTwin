"""Text extractors — clean raw captured text and pull YouTube transcripts.

The Chrome extension already does a first-pass DOM extraction (article
selectors, fallback to body text). This module does the second pass:
normalizes whitespace, strips boilerplate, and handles platform-specific
cases the extension can't (like YouTube transcripts).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse


# --- Constants -------------------------------------------------------------

MIN_MEANINGFUL_TEXT_LEN = 50
MAX_TEXT_LEN = 50_000  # Hard cap — avoid blowing up LLM token budgets later.


# --- Data types ------------------------------------------------------------


@dataclass
class ExtractedText:
    """Normalized text plus anything extracted out-of-band (e.g. transcript)."""

    clean_text: str
    source: str  # "extension", "youtube_transcript", "fallback"
    transcript: Optional[str] = None  # YouTube, if applicable

    @property
    def combined(self) -> str:
        """Text used for downstream enrichment/embeddings."""
        if self.transcript:
            return f"{self.clean_text}\n\n--- TRANSCRIPT ---\n{self.transcript}"
        return self.clean_text


# --- Text normalization ----------------------------------------------------


_WS_RE = re.compile(r"[ \t]+")
_NEWLINES_RE = re.compile(r"\n{3,}")
_BOILERPLATE_RE = re.compile(
    r"(cookie policy|accept all cookies|subscribe to our newsletter|"
    r"sign up for.*?newsletter|advertisement)",
    re.IGNORECASE,
)


def normalize_text(raw: str) -> str:
    """Collapse whitespace, strip junk lines, cap length.

    The extension grabs innerText which is already pretty clean, but it
    picks up nav menus, cookie banners, and repeated blank lines. We trim
    those down to something an LLM won't choke on.
    """
    if not raw:
        return ""

    # Normalize whitespace within lines
    lines = [_WS_RE.sub(" ", line).strip() for line in raw.splitlines()]

    # Drop empty + boilerplate lines
    cleaned = []
    for line in lines:
        if not line:
            cleaned.append("")
            continue
        if len(line) < 3:  # Single-character lines are almost always garbage
            continue
        if _BOILERPLATE_RE.search(line) and len(line) < 120:
            continue
        cleaned.append(line)

    text = "\n".join(cleaned)
    text = _NEWLINES_RE.sub("\n\n", text).strip()

    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN] + "\n\n[...truncated...]"

    return text


# --- YouTube ---------------------------------------------------------------


_YOUTUBE_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be")


def extract_youtube_video_id(url: str) -> Optional[str]:
    """Pull the video ID from any standard YouTube URL shape.

    Handles: youtube.com/watch?v=ID, youtu.be/ID, youtube.com/shorts/ID,
    youtube.com/embed/ID.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    if parsed.hostname not in _YOUTUBE_HOSTS:
        return None

    if parsed.hostname == "youtu.be":
        vid = parsed.path.lstrip("/")
        return vid or None

    if parsed.path == "/watch":
        return parse_qs(parsed.query).get("v", [None])[0]

    for prefix in ("/shorts/", "/embed/", "/live/"):
        if parsed.path.startswith(prefix):
            return parsed.path[len(prefix):].split("/")[0] or None

    return None


def fetch_youtube_transcript(video_id: str) -> Optional[str]:
    """Fetch transcript text using youtube-transcript-api.

    Returns None on any failure (no transcript, region-locked, network
    error) — callers should treat transcript as best-effort.
    """
    try:
        # Imported lazily so the rest of the module doesn't hard-fail
        # if the library isn't installed (e.g. in minimal test envs).
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None

    try:
        entries = YouTubeTranscriptApi.get_transcript(video_id)
    except Exception:
        # Library raises a mess of different exception types. Any of them
        # just means "no transcript available" for our purposes.
        return None

    return " ".join(entry["text"] for entry in entries if entry.get("text"))


# --- Public API ------------------------------------------------------------


def extract(raw_text: str, url: str = "", platform: str = "general") -> ExtractedText:
    """Main entry point — normalize text, add transcript if YouTube."""
    clean = normalize_text(raw_text)
    transcript = None
    source = "extension"

    if platform == "youtube" or (url and "youtube" in url) or (url and "youtu.be" in url):
        video_id = extract_youtube_video_id(url)
        if video_id:
            transcript = fetch_youtube_transcript(video_id)
            if transcript:
                source = "youtube_transcript"

    if not clean and not transcript:
        source = "fallback"

    return ExtractedText(clean_text=clean, source=source, transcript=transcript)
