"""Tests for backend/storage/chunking.py — Phase 3 Step 3.

Run with: pytest tests/test_chunking.py -v

Pure-function module under test. No fixtures needed beyond direct
calls. All A.5 rules covered:

  - Paragraph split for articles (basic + edge cases)
  - Token-window split for long transcripts (size, overlap, word
    boundaries)
  - Chapter-aware chunking (short chapters → 1 chunk each, long
    chapters → sub-split)
  - Whole-thing strategy for short content (captions, summaries)
  - Dispatcher routes correctly per source_kind
  - Error path on unknown source_kind
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.storage.chunking import (  # noqa: E402
    DEFAULT_MAX_CHAPTER_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_WINDOW_TOKENS,
    SHORT_TRANSCRIPT_SECONDS,
    SOURCE_KIND_ARTICLE_PARAGRAPH,
    SOURCE_KIND_IMAGE_CAPTION,
    SOURCE_KIND_SUMMARY,
    SOURCE_KIND_TRANSCRIPT_SEGMENT,
    chunk,
    _chunk_chapters,
    _chunk_paragraphs,
    _chunk_token_window,
    _chunk_whole,
    _tokens_to_chars,
)


# ---- Paragraph splitting (article body) ------------------------------

class TestChunkParagraphs:
    def test_three_paragraphs(self):
        text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        out = _chunk_paragraphs(text)
        assert out == [
            "First paragraph.",
            "Second paragraph.",
            "Third paragraph.",
        ]

    def test_handles_multiple_blank_lines(self):
        text = "Para one.\n\n\n\nPara two."
        out = _chunk_paragraphs(text)
        assert out == ["Para one.", "Para two."]

    def test_handles_indented_blank_lines(self):
        text = "Para one.\n   \nPara two.\n\t\nPara three."
        out = _chunk_paragraphs(text)
        assert out == ["Para one.", "Para two.", "Para three."]

    def test_strips_per_paragraph_whitespace(self):
        text = "  Para one.  \n\n   Para two.\n   "
        out = _chunk_paragraphs(text)
        assert out == ["Para one.", "Para two."]

    def test_single_paragraph_returns_single_chunk(self):
        out = _chunk_paragraphs("Just one block of text, no blank lines.")
        assert out == ["Just one block of text, no blank lines."]

    def test_empty_input_returns_empty_list(self):
        assert _chunk_paragraphs("") == []
        assert _chunk_paragraphs("   ") == []
        assert _chunk_paragraphs("\n\n\n") == []

    def test_only_one_real_paragraph_among_blanks(self):
        out = _chunk_paragraphs("\n\n   \n\nOnly real one.\n\n   \n\n")
        assert out == ["Only real one."]

    def test_oversized_paragraph_falls_through_to_token_window(self):
        """A wall-of-text paragraph exceeding DEFAULT_MAX_CHAPTER_TOKENS
        would otherwise blow past the embedding model's 512-token cap.
        Should sub-split via the token-window strategy."""
        max_chars = _tokens_to_chars(DEFAULT_MAX_CHAPTER_TOKENS)
        big_para = ("word " * (max_chars // 2))  # ~2x the max
        text = f"Short intro.\n\n{big_para}\n\nShort outro."
        out = _chunk_paragraphs(text)
        # Intro + outro stay one chunk each; the big paragraph splits.
        assert out[0] == "Short intro."
        assert out[-1] == "Short outro."
        assert len(out) >= 3
        # No chunk exceeds the embedding budget.
        for c in out:
            assert len(c) <= max_chars + _tokens_to_chars(DEFAULT_OVERLAP_TOKENS)


# ---- Token-window splitting (long transcripts, fallback) -------------

class TestChunkTokenWindow:
    def test_short_text_returns_single_chunk(self):
        # ~10 chars, way below default 1024-char window
        out = _chunk_token_window("Short bit.")
        assert out == ["Short bit."]

    def test_empty_input_returns_empty_list(self):
        assert _chunk_token_window("") == []
        assert _chunk_token_window("    ") == []

    def test_long_text_splits_with_overlap(self):
        # Build text big enough to force at least 2 chunks
        word = "lorem "
        text = word * 500  # ~3000 chars, well past 1024-char window
        out = _chunk_token_window(text)
        assert len(out) >= 2

        # Window size is in chars (token * 4); first chunk should be
        # close to that target (with possible word-boundary trim).
        target_chars = _tokens_to_chars(DEFAULT_WINDOW_TOKENS)
        assert len(out[0]) <= target_chars

    def test_overlap_preserves_words_across_boundaries(self):
        """A unique word placed inside the overlap zone of two adjacent
        windows must appear in BOTH chunks if overlap is doing its job.

        With window=200 chars and overlap=80 chars, chunk1 covers
        [0, 200) and chunk2 starts around (200-80)=120. So anchoring
        at position ~150 lands in both. Asserting >=1 would pass even
        with overlap broken — we want >=2."""
        anchor = "OBSERVABILIA"  # unique token, won't appear elsewhere
        prefix = "x " * 75   # 150 chars, anchor lands at position 150
        suffix = " y" * 100  # 200 chars
        text = prefix + anchor + suffix
        out = _chunk_token_window(
            text,
            window_tokens=50,    # 200 char window
            overlap_tokens=20,   # 80 char overlap
        )
        chunks_with_anchor = [c for c in out if anchor in c]
        assert len(chunks_with_anchor) >= 2, (
            f"anchor {anchor!r} should appear in two adjacent chunks "
            f"(overlap broken?); got {len(chunks_with_anchor)}: {out}"
        )

    def test_word_boundary_respected(self):
        """No chunk should end mid-word when there's a whitespace
        candidate within the window."""
        text = "alpha " * 400  # 2400 chars, lots of word boundaries
        out = _chunk_token_window(text)
        for c in out:
            # If a chunk ends with a partial word, it would end with
            # something other than whitespace OR the full word "alpha".
            # We assert the trimmed chunk ends on a complete word.
            last = c.rsplit(maxsplit=1)[-1] if " " in c else c
            assert last == "alpha", (
                f"chunk ends mid-word: ...{c[-30:]!r}"
            )

    def test_clamps_pathological_overlap(self):
        """Overlap >= window would cause an infinite loop. Function
        clamps overlap to a sane fraction internally."""
        text = "x " * 1000
        # Overlap == window; should NOT hang
        out = _chunk_token_window(
            text,
            window_tokens=100,
            overlap_tokens=100,
        )
        assert len(out) >= 1

    def test_no_whitespace_in_overlap_zone_terminates(self):
        """Defensive: if all whitespace in a window sits inside the
        first `overlap_chars` of the start (one giant unbroken token
        — URL, base64 blob, etc.), the older code could loop forever
        re-emitting the same chunk. Forward progress must be guaranteed."""
        # 5-char prefix of normal text, then a 1000-char unbroken token.
        # With a 200-char window and 80-char overlap, the only space is
        # at position 4 — well inside the overlap zone.
        text = "abc " + ("x" * 1000)
        out = _chunk_token_window(
            text,
            window_tokens=50,    # 200 chars
            overlap_tokens=20,   # 80 chars
        )
        # Has to actually return something and not hang.
        assert len(out) >= 1
        # And the full text is covered (last chunk ends with the tail).
        assert out[-1].endswith("x")

    def test_word_boundary_with_newlines(self):
        """Whitespace boundary backup must respect \\n and \\t, not
        only the space character."""
        # 500 chars with newlines as the only inter-word whitespace.
        line = "alphabetagamma"  # 14 chars, no spaces
        text = "\n".join([line] * 60)  # ~900 chars, no spaces, only \n
        out = _chunk_token_window(text, window_tokens=50, overlap_tokens=10)
        # Every chunk should end on a complete `line` token, never
        # mid-word like "alphabetag".
        for c in out:
            tail = c.rsplit(maxsplit=1)[-1] if any(ch.isspace() for ch in c) else c
            assert tail == line, f"chunk ends mid-word: ...{c[-20:]!r}"


# ---- Chapter-aware chunking ------------------------------------------

class TestChunkChapters:
    def test_short_chapters_one_chunk_each(self):
        chapters = [
            "Intro to kanban.",
            "WIP limits explained.",
            "Closing thoughts.",
        ]
        out = _chunk_chapters(chapters)
        assert out == chapters

    def test_long_chapter_gets_sub_split(self):
        long_chapter = "lorem " * 1500  # ~9000 chars, way past 800-token (3200-char) cap
        short_chapter = "Brief outro."
        out = _chunk_chapters([long_chapter, short_chapter])
        # The long chapter should produce multiple chunks; the short
        # one stays as one. So total is >= 3.
        assert len(out) >= 3
        assert out[-1] == "Brief outro."

    def test_skips_empty_chapters(self):
        out = _chunk_chapters(["Real content.", "", "   ", "More content."])
        assert out == ["Real content.", "More content."]

    def test_empty_list_returns_empty(self):
        assert _chunk_chapters([]) == []


# ---- Whole-thing strategy (captions, summaries) ----------------------

class TestChunkWhole:
    def test_returns_single_chunk(self):
        assert _chunk_whole("A short caption.") == ["A short caption."]

    def test_strips_whitespace(self):
        assert _chunk_whole("  trim me  ") == ["trim me"]

    def test_empty_returns_empty_list(self):
        assert _chunk_whole("") == []
        assert _chunk_whole("   ") == []
        assert _chunk_whole(None) == []  # type: ignore[arg-type]


# ---- Dispatcher (top-level chunk()) ----------------------------------

class TestChunkDispatcher:
    def test_article_routes_to_paragraphs(self):
        text = "First.\n\nSecond.\n\nThird."
        out = chunk(source_kind=SOURCE_KIND_ARTICLE_PARAGRAPH, text=text)
        assert out == ["First.", "Second.", "Third."]

    def test_summary_routes_to_whole(self):
        out = chunk(source_kind=SOURCE_KIND_SUMMARY, text="Summary here.")
        assert out == ["Summary here."]

    def test_image_caption_routes_to_whole(self):
        out = chunk(source_kind=SOURCE_KIND_IMAGE_CAPTION, text="alt text")
        assert out == ["alt text"]

    def test_transcript_with_chapters_routes_to_chapter_aware(self):
        chapters = ["Chapter A content.", "Chapter B content."]
        # `text` argument is ignored when chapter_texts is provided.
        out = chunk(
            source_kind=SOURCE_KIND_TRANSCRIPT_SEGMENT,
            text="ignored",
            chapter_texts=chapters,
        )
        assert out == chapters

    def test_short_transcript_routes_to_whole(self):
        text = "Short reel transcript."
        out = chunk(
            source_kind=SOURCE_KIND_TRANSCRIPT_SEGMENT,
            text=text,
            transcript_duration_seconds=45.0,
        )
        assert out == [text]

    def test_long_transcript_routes_to_token_window(self):
        text = "lorem " * 800  # ~4800 chars, > 1024-char window
        out = chunk(
            source_kind=SOURCE_KIND_TRANSCRIPT_SEGMENT,
            text=text,
            transcript_duration_seconds=600.0,  # 10 minutes
        )
        assert len(out) >= 2

    def test_transcript_no_duration_defaults_to_window(self):
        """When duration is unknown, we play it safe and use the
        windowed strategy. (Better to over-chunk a short transcript
        than to put a 30-minute one into a single chunk.)"""
        text = "lorem " * 800
        out = chunk(
            source_kind=SOURCE_KIND_TRANSCRIPT_SEGMENT,
            text=text,
            transcript_duration_seconds=None,
        )
        assert len(out) >= 2

    def test_unknown_source_kind_raises(self):
        with pytest.raises(ValueError) as exc:
            chunk(source_kind="not_a_real_kind", text="anything")
        assert "not_a_real_kind" in str(exc.value)

    def test_empty_text_returns_empty_list(self):
        """Empty / whitespace-only text on the whole-strategy paths
        (summary, image_caption) should return [] not [""]."""
        for kind in (SOURCE_KIND_SUMMARY, SOURCE_KIND_IMAGE_CAPTION):
            assert chunk(source_kind=kind, text="") == []
            assert chunk(source_kind=kind, text="   \n\t ") == []

    def test_transcript_short_threshold_boundary(self):
        """Right at SHORT_TRANSCRIPT_SECONDS — should fall through to
        token-window (the < check, not <=)."""
        text = "lorem " * 800
        out = chunk(
            source_kind=SOURCE_KIND_TRANSCRIPT_SEGMENT,
            text=text,
            transcript_duration_seconds=float(SHORT_TRANSCRIPT_SECONDS),
        )
        # 120s exactly is NOT short, so this windowed.
        assert len(out) >= 2


# ---- Defaults sanity ------------------------------------------------

class TestDefaults:
    def test_overlap_smaller_than_window(self):
        # Otherwise we'd fall into the pathological-clamp path on
        # default usage. Guard against accidentally swapping the
        # constants.
        assert DEFAULT_OVERLAP_TOKENS < DEFAULT_WINDOW_TOKENS
