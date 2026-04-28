"""Tests for the Phase 1 capture pipeline.

Run with: pytest tests/ -v

These tests deliberately avoid hitting the Claude API so they can run
offline. Vision calls are tested via the skip_api flag.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

# Make `backend.*` importable when running pytest from the project root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.capture import extractors, vision  # noqa: E402


# --- extractors.normalize_text --------------------------------------------


class TestNormalizeText:
    def test_empty_input(self):
        assert extractors.normalize_text("") == ""
        assert extractors.normalize_text(None) == ""

    def test_collapses_whitespace(self):
        out = extractors.normalize_text("hello    world\t\tfoo")
        assert out == "hello world foo"

    def test_collapses_multiple_blank_lines(self):
        out = extractors.normalize_text("line1\n\n\n\n\nline2")
        assert "\n\n\n" not in out
        assert "line1" in out and "line2" in out

    def test_strips_boilerplate(self):
        raw = (
            "This is a real article about AI.\n"
            "Accept all cookies\n"
            "Subscribe to our newsletter\n"
            "More real content here."
        )
        out = extractors.normalize_text(raw)
        assert "real article" in out
        assert "More real content" in out
        assert "Accept all cookies" not in out

    def test_caps_length(self):
        huge = "x" * (extractors.MAX_TEXT_LEN + 1000)
        out = extractors.normalize_text(huge)
        assert len(out) <= extractors.MAX_TEXT_LEN + 30  # +truncation marker
        assert "[...truncated...]" in out


# --- extractors.extract_youtube_video_id -----------------------------------


class TestYouTubeID:
    @pytest.mark.parametrize("url,expected", [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtube.com/watch?v=abc123&feature=share", "abc123"),
        ("https://youtu.be/xyz789", "xyz789"),
        ("https://www.youtube.com/shorts/shortID", "shortID"),
        ("https://www.youtube.com/embed/embedID", "embedID"),
        ("https://m.youtube.com/watch?v=mobileID", "mobileID"),
        ("https://example.com/watch?v=notyoutube", None),
        ("not a url at all", None),
        ("", None),
    ])
    def test_various_urls(self, url, expected):
        assert extractors.extract_youtube_video_id(url) == expected


# --- extractors.extract (top-level) ----------------------------------------


class TestExtract:
    def test_non_youtube_passes_through(self):
        result = extractors.extract(
            raw_text="Some article text.",
            url="https://example.com/post",
            platform="general",
        )
        assert result.source == "extension"
        assert result.transcript is None
        assert "article text" in result.clean_text

    def test_empty_text_marks_fallback(self):
        result = extractors.extract(raw_text="", url="", platform="general")
        assert result.source == "fallback"

    def test_youtube_url_attempts_transcript(self):
        # With no library installed the transcript will be None —
        # we're just verifying the branch is taken.
        result = extractors.extract(
            raw_text="video description text",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            platform="youtube",
        )
        # transcript is best-effort; if it succeeds, source flips
        assert result.source in ("extension", "youtube_transcript")


# --- vision._decode_data_url ----------------------------------------------


class TestDataURLDecode:
    def test_decodes_png(self):
        payload = b"\x89PNG\r\n\x1a\nfake image data"
        b64 = base64.b64encode(payload).decode()
        decoded = vision._decode_data_url(f"data:image/png;base64,{b64}")
        assert decoded is not None
        data, mime = decoded
        assert data == payload
        assert mime == "image/png"

    def test_jpg_normalized_to_jpeg(self):
        b64 = base64.b64encode(b"jpg bytes").decode()
        decoded = vision._decode_data_url(f"data:image/jpg;base64,{b64}")
        assert decoded is not None
        _, mime = decoded
        assert mime == "image/jpeg"

    def test_invalid_url_returns_none(self):
        assert vision._decode_data_url("not a data url") is None
        assert vision._decode_data_url("https://example.com/img.png") is None

    def test_non_image_mime_returns_none(self):
        b64 = base64.b64encode(b"x").decode()
        assert vision._decode_data_url(f"data:video/mp4;base64,{b64}") is None


# --- vision._parse_vision_response ----------------------------------------


class TestVisionResponseParser:
    def test_standard_response(self):
        raw = (
            "DESCRIPTION: A Bollywood meme featuring Deepika Padukone "
            "referencing the movie Tamasha.\n"
            "TEXT_IN_IMAGE: Kyun ki tum normal nahi ho!"
        )
        desc, text = vision._parse_vision_response(raw)
        assert "Bollywood" in desc
        assert "Tamasha" in desc
        assert "Kyun ki tum normal nahi ho" in text

    def test_none_text_becomes_empty(self):
        raw = "DESCRIPTION: A photo of a sunset.\nTEXT_IN_IMAGE: none"
        _, text = vision._parse_vision_response(raw)
        assert text == ""

    def test_multiline_description(self):
        raw = (
            "DESCRIPTION: A long description\n"
            "that spans multiple lines.\n"
            "TEXT_IN_IMAGE: caption"
        )
        desc, text = vision._parse_vision_response(raw)
        assert "long description" in desc
        assert "multiple lines" in desc
        assert text == "caption"

    def test_malformed_falls_back_to_whole_text(self):
        raw = "just some raw output without sections"
        desc, text = vision._parse_vision_response(raw)
        assert desc == raw
        assert text == ""


# --- vision.process_images (skip_api path) --------------------------------


class TestProcessImagesSkipAPI:
    def test_empty_list(self):
        result = vision.process_images([], skip_api=True)
        assert result.descriptions == []

    def test_data_url_is_loaded_and_saved(self, tmp_path, monkeypatch):
        # 1x1 transparent PNG
        tiny_png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNgYAAAAAMAAWgmWQ0AAAAASUVORK5CYII="
        )
        b64 = base64.b64encode(tiny_png).decode()
        data_url = f"data:image/png;base64,{b64}"

        # Point images_path at a temp dir
        monkeypatch.setattr(
            vision.settings, "images_path", str(tmp_path), raising=False
        )

        result = vision.process_images([data_url], skip_api=True)
        assert len(result.descriptions) == 1
        desc = result.descriptions[0]
        assert desc.error is None
        assert desc.local_path is not None
        assert Path(desc.local_path).exists()
        assert desc.description == "[vision API skipped]"

    def test_invalid_source_recorded_as_error(self):
        result = vision.process_images(["not-a-url"], skip_api=True)
        assert len(result.descriptions) == 1
        assert result.descriptions[0].error == "failed to load image"


# --- VisionResult.as_text -------------------------------------------------


class TestVisionResultAsText:
    def test_flattens_descriptions(self):
        vr = vision.VisionResult(
            descriptions=[
                vision.ImageDescription(
                    local_path="/tmp/a.jpg",
                    description="A meme about cricket.",
                    extracted_text="howzat",
                ),
                vision.ImageDescription(
                    local_path="/tmp/b.jpg",
                    description="A political cartoon.",
                    extracted_text="",
                ),
            ]
        )
        out = vr.as_text()
        assert "[IMAGE 1]" in out
        assert "cricket" in out
        assert "howzat" in out
        assert "[IMAGE 2]" in out
        assert "cartoon" in out

    def test_errors_are_excluded(self):
        vr = vision.VisionResult(
            descriptions=[
                vision.ImageDescription(
                    local_path=None,
                    description="",
                    error="failed to load",
                )
            ]
        )
        assert vr.as_text() == ""
