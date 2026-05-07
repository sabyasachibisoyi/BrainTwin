"""Chunking strategies for the Phase 3 storage layer.

Per docs/phase3-design.md A.5. The unit of retrieval is a chunk (β
schema). Different source kinds get different chunking treatments, all
dispatched through `chunk()` based on the `source_kind` argument.

Pure-function module — no I/O, no DB, no model calls. Takes text,
returns text. Embedding generation, persistence, and tagging happen in
the calling layer (the dual-write hook in Step 4 / the migration
script in Step 5).

Token counting strategy
-----------------------
We use a `chars / 4 ≈ tokens` heuristic instead of a real tokenizer
(tiktoken / HF tokenizers / etc.). Rationale:

  - Heuristic error is ~10% — a "256-token" target chunk might come out
    at 230-280 tokens. That's well within the noise of retrieval
    quality and saves us a 3-5 MB dep + an import-time cost.
  - The actual model that will embed these chunks (all-MiniLM-L6-v2,
    A.6) has a 512-token max input. Our 256-token target leaves
    plenty of margin even with heuristic drift.
  - If retrieval quality ever hints that chunk sizes are off, the swap
    to tiktoken is a one-function change at the bottom of this module.
    See TODO marker on `_estimate_token_count`.

Word-boundary respect
---------------------
The token-window strategy walks the text in target-sized windows but
backs up to the nearest whitespace before cutting, so chunks never
slice through a word. Overlap is also at word boundaries.

Edge cases
----------
  - Empty / whitespace-only input → empty list (no chunks).
  - Text shorter than the window → single chunk (no fallthrough).
  - Multiple consecutive blank lines collapse to one paragraph break.
  - chapters with empty/whitespace strings are skipped.
"""

from __future__ import annotations

import re


# ---- Source-kind constants (must match SOURCE_KIND values used in
# the chunks table per docs/phase3-design.md A.4) -----------------

SOURCE_KIND_ARTICLE_PARAGRAPH = "article_paragraph"
SOURCE_KIND_TRANSCRIPT_SEGMENT = "transcript_segment"
SOURCE_KIND_IMAGE_CAPTION = "image_caption"
SOURCE_KIND_SUMMARY = "summary"

ALL_SOURCE_KINDS = (
    SOURCE_KIND_ARTICLE_PARAGRAPH,
    SOURCE_KIND_TRANSCRIPT_SEGMENT,
    SOURCE_KIND_IMAGE_CAPTION,
    SOURCE_KIND_SUMMARY,
)


# ---- Token-budget defaults (per A.5) ---------------------------------

# Default token-window size when chunking long transcripts that lack
# chapter markers. 256 tokens ≈ 1024 chars at our heuristic.
DEFAULT_WINDOW_TOKENS = 256

# Overlap between consecutive windows. 64 tokens ≈ 256 chars. Overlap
# preserves context for ideas that straddle chunk boundaries — without
# it, a sentence spanning the cut gets split between two chunks and
# neither retrieves well.
DEFAULT_OVERLAP_TOKENS = 64

# Maximum chunk size for chapter-aware transcript chunking. Long
# chapters (e.g. a 30-minute deep-dive section of a podcast) get
# sub-split using the token-window strategy.
DEFAULT_MAX_CHAPTER_TOKENS = 800

# A transcript shorter than this many seconds gets stored as one chunk
# rather than token-windowed. Reels and TikToks are typically <90 s;
# we use 120 s as the threshold so a 90-second podcast trailer or
# short news clip still fits.
SHORT_TRANSCRIPT_SECONDS = 120


# ---- Token-counting heuristic ----------------------------------------

# Approximate ratio for English text. GPT-2/GPT-4 tokenizers average
# ~4 chars per token on natural-language inputs; ~3.5 for code-heavy
# text. We use 4 as a safe-side default.
_CHARS_PER_TOKEN = 4


def _estimate_token_count(text: str) -> int:
    """Heuristic char-based token estimate. See module docstring for
    the rationale and the swap path to a real tokenizer.
    TODO: if retrieval quality suggests the heuristic drift matters,
    swap this to `tiktoken.encoding_for_model("cl100k_base").encode`.
    """
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _tokens_to_chars(tokens: int) -> int:
    """Inverse heuristic — convert a token target into a char target."""
    return tokens * _CHARS_PER_TOKEN


# ---- Strategy: paragraph split (articles) ----------------------------

# Pattern for paragraph break: blank line, optionally with whitespace.
# Captures the common cases: \n\n, \n\t\n, \n   \n, \n\r\n\r\n.
_PARAGRAPH_RE = re.compile(r"\n\s*\n+")


def _chunk_paragraphs(text: str) -> list[str]:
    """Split article body on blank-line paragraph breaks.

    Strips per-paragraph whitespace and drops empty results. Single
    paragraph (no blank lines) returns a single-element list.

    Paragraphs longer than DEFAULT_MAX_CHAPTER_TOKENS fall through to
    the token-window strategy — the embedding model (all-MiniLM-L6-v2,
    A.6) caps at 512 tokens, so a wall-of-text paragraph would
    otherwise truncate at embed time."""
    if not text or not text.strip():
        return []
    max_chars = _tokens_to_chars(DEFAULT_MAX_CHAPTER_TOKENS)
    out: list[str] = []
    for p in _PARAGRAPH_RE.split(text):
        s = p.strip()
        if not s:
            continue
        if len(s) <= max_chars:
            out.append(s)
        else:
            out.extend(_chunk_token_window(s))
    return out


# ---- Strategy: fixed token window with overlap (long transcripts) ----

# Any whitespace counts as a word boundary — transcripts and stripped
# HTML can contain \n / \t inside the body, not just spaces.
_WHITESPACE_RE = re.compile(r"\s")


def _last_whitespace(text: str, start: int, end: int) -> int:
    """Index of the last whitespace char in text[start:end], or -1."""
    last = -1
    for m in _WHITESPACE_RE.finditer(text, start, end):
        last = m.start()
    return last


def _chunk_token_window(
    text: str,
    *,
    window_tokens: int = DEFAULT_WINDOW_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[str]:
    """Walk `text` in token-sized windows, with overlap, respecting
    word boundaries.

    Algorithm: char-based windowing (per the heuristic), then back up
    to the nearest whitespace so chunks never slice through a word.
    Same trick on the overlap step — we advance by `(window - overlap)`
    chars and snap back to a word boundary.

    Returns single-element list when text is shorter than one window
    (no fallthrough into the loop)."""
    if not text or not text.strip():
        return []
    target_chars = _tokens_to_chars(window_tokens)
    overlap_chars = _tokens_to_chars(overlap_tokens)
    if overlap_chars >= target_chars:
        # Pathological config — caller asked for >=100% overlap. Clamp to
        # something sane so we don't infinite-loop.
        overlap_chars = max(1, target_chars // 4)

    text_len = len(text)
    if text_len <= target_chars:
        # Fits in one chunk; skip the windowing loop.
        return [text.strip()]

    chunks: list[str] = []
    i = 0
    while i < text_len:
        end = min(i + target_chars, text_len)
        if end < text_len:
            # Back up to the last whitespace inside this window so we
            # don't cut a word in half. If no whitespace found in the
            # window (unusual — extremely long word), fall through to
            # the raw cut.
            ws = _last_whitespace(text, i, end)
            if ws > i:
                end = ws
        chunk = text[i:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= text_len:
            break
        # Advance with overlap. If the word-boundary backup pulled
        # `end` so close to `i` that overlap would land us at or before
        # the previous start (e.g. one ultra-long token like a URL or
        # base64 blob fills most of the window), skip overlap and start
        # fresh at `end` so we always make forward progress.
        next_i = end - overlap_chars
        if next_i <= i:
            next_i = end
        # Skip leading whitespace at the new start position.
        while next_i < text_len and text[next_i].isspace():
            next_i += 1
        i = next_i
    return chunks


# ---- Strategy: chapter-aware (transcripts with chapter metadata) ----

def _chunk_chapters(
    chapter_texts: list[str],
    *,
    max_chapter_tokens: int = DEFAULT_MAX_CHAPTER_TOKENS,
) -> list[str]:
    """One chunk per chapter, sub-splitting long chapters via the
    token-window strategy.

    Caller is responsible for chapter segmentation — yt-dlp gives us
    chapter time ranges, but we need the transcript per chapter, which
    requires whisper segment-level timestamps mapped to chapters. That
    segmentation lives in `backend/capture/video_transcriber.py`
    (Phase 2.5+ enhancement); this function just consumes the result.

    Empty / whitespace-only chapter texts are skipped silently — a
    chapter with no spoken content (silent intro music, etc.) doesn't
    deserve its own chunk."""
    if not chapter_texts:
        return []
    max_chars = _tokens_to_chars(max_chapter_tokens)
    out: list[str] = []
    for chapter in chapter_texts:
        s = (chapter or "").strip()
        if not s:
            continue
        if len(s) <= max_chars:
            out.append(s)
        else:
            # Long chapter — sub-split. Use a slightly larger window
            # than the default so a single chapter can still be one
            # logical unit at retrieval time.
            out.extend(_chunk_token_window(
                s,
                window_tokens=max_chapter_tokens,
                overlap_tokens=DEFAULT_OVERLAP_TOKENS,
            ))
    return out


# ---- Strategy: whole thing (short content: captions, summaries) ----

def _chunk_whole(text: str) -> list[str]:
    """Return the input as a single chunk (or empty list if the input
    is empty / whitespace-only). Used for image captions, OG
    descriptions, enrichment summaries, and short transcripts."""
    s = (text or "").strip()
    return [s] if s else []


# ---- Top-level dispatcher --------------------------------------------

def chunk(
    *,
    source_kind: str,
    text: str,
    chapter_texts: list[str] | None = None,
    transcript_duration_seconds: float | None = None,
) -> list[str]:
    """Apply the A.5 chunking rule for the given source_kind.

    Args:
        source_kind: one of the SOURCE_KIND_* constants. Determines
            which chunking strategy fires.
        text: the raw content to chunk.
        chapter_texts: ONLY meaningful for transcripts. If provided
            (non-empty list), chapter-aware chunking fires; if None or
            empty, falls through to short-vs-windowed transcript logic.
        transcript_duration_seconds: ONLY meaningful for transcripts
            without chapters. If less than SHORT_TRANSCRIPT_SECONDS,
            the whole transcript becomes one chunk; otherwise it gets
            token-windowed.

    Returns:
        Ordered list of chunk text strings. The caller is responsible
        for assigning chunk_index (zero-based positional) and the
        source_kind tag on the resulting `chunks` table row.

    Raises:
        ValueError: source_kind not in ALL_SOURCE_KINDS.
    """
    if source_kind == SOURCE_KIND_ARTICLE_PARAGRAPH:
        return _chunk_paragraphs(text)

    if source_kind == SOURCE_KIND_TRANSCRIPT_SEGMENT:
        if chapter_texts:
            return _chunk_chapters(chapter_texts)
        # No chapters — decide by duration if available
        if (
            transcript_duration_seconds is not None
            and transcript_duration_seconds < SHORT_TRANSCRIPT_SECONDS
        ):
            return _chunk_whole(text)
        return _chunk_token_window(text)

    if source_kind in (SOURCE_KIND_IMAGE_CAPTION, SOURCE_KIND_SUMMARY):
        return _chunk_whole(text)

    raise ValueError(
        f"unknown source_kind={source_kind!r}; "
        f"expected one of {ALL_SOURCE_KINDS}"
    )


__all__ = [
    # constants
    "SOURCE_KIND_ARTICLE_PARAGRAPH",
    "SOURCE_KIND_TRANSCRIPT_SEGMENT",
    "SOURCE_KIND_IMAGE_CAPTION",
    "SOURCE_KIND_SUMMARY",
    "ALL_SOURCE_KINDS",
    "DEFAULT_WINDOW_TOKENS",
    "DEFAULT_OVERLAP_TOKENS",
    "DEFAULT_MAX_CHAPTER_TOKENS",
    "SHORT_TRANSCRIPT_SECONDS",
    # public API
    "chunk",
]
