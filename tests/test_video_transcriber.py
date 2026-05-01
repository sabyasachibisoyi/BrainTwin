"""Tests for backend/capture/video_transcriber.py — Phase 2.5 Fix 3.

Run with: pytest tests/test_video_transcriber.py -v

What's covered with mocks (no network, no whisper binary required):
  - is_video_url() pattern matching (positive + negative cases)
  - transcribe_video() routing through the disabled / no-deps / too-long
    short-circuits and returning the right TranscriptionSkipped reasons
  - The kill-switch (settings.video_transcribe_enabled = False)

What's NOT covered here (requires real binaries):
  - The whisper-cli subprocess call itself
  - yt-dlp's actual extractor behaviour against live IG/FB URLs

Those land in the smoke test (docs/phase2.5-capture-hydration.md Fix 3
exit criteria).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.capture import video_transcriber as vt  # noqa: E402
from backend.capture.video_transcriber import (  # noqa: E402
    TranscriptionResult,
    TranscriptionSkipped,
    is_video_url,
    transcribe_video,
)


# ---- is_video_url -----------------------------------------------------

class TestIsVideoUrl:
    @pytest.mark.parametrize("url", [
        "https://www.instagram.com/reel/DXqmGydAAFx/",
        "https://instagram.com/reels/abc123/",
        "https://www.instagram.com/p/CXyz/",          # post (often has video)
        "https://www.instagram.com/tv/AbCdE/",        # IGTV
        "https://www.facebook.com/watch?v=12345",
        "https://www.facebook.com/share/v/abc/",
        "https://www.facebook.com/SomePage/videos/9999",
        "https://fb.watch/abc123",
        "https://www.tiktok.com/@user/video/1234567890",
        "https://vm.tiktok.com/ZSxxxxx/",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/abc123",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://twitter.com/user/status/12345",
        "https://x.com/user/status/12345",
    ])
    def test_known_video_hosts(self, url):
        assert is_video_url(url) is True

    @pytest.mark.parametrize("url", [
        "https://en.wikipedia.org/wiki/Knowledge_graph",
        "https://news.ycombinator.com/item?id=123",
        "https://substack.com/p/some-essay",
        "tg://message/12345/678",
        "",
        "not even a url",
    ])
    def test_non_video_urls(self, url):
        assert is_video_url(url) is False

    def test_platform_short_circuit(self):
        # Platform tag from the bot wins even when URL is unrecognised —
        # protects against IG share URLs we didn't anticipate.
        assert is_video_url("https://shortener.example.com/xyz", platform="instagram") is True
        assert is_video_url("https://shortener.example.com/xyz", platform="facebook_video") is True
        # Non-video platform doesn't promote a non-video URL.
        assert is_video_url("https://shortener.example.com/xyz", platform="general") is False

    def test_protocol_filter(self):
        assert is_video_url("ftp://example.com/file.mp4") is False
        assert is_video_url("file:///tmp/x.mp4") is False


# ---- transcribe_video routing ----------------------------------------

class TestTranscribeVideoShortCircuits:
    def test_kill_switch_off(self, monkeypatch):
        monkeypatch.setattr(vt.settings, "video_transcribe_enabled", False)
        result = asyncio.run(transcribe_video("https://www.instagram.com/reel/abc/"))
        assert isinstance(result, TranscriptionSkipped)
        assert result.reason == "video_transcribe_disabled"

    def test_ytdlp_missing(self, monkeypatch):
        monkeypatch.setattr(vt.settings, "video_transcribe_enabled", True)
        monkeypatch.setattr(vt, "_YTDLP_AVAILABLE", False)
        result = asyncio.run(transcribe_video("https://www.instagram.com/reel/abc/"))
        assert isinstance(result, TranscriptionSkipped)
        assert result.reason == "ytdlp_not_installed"

    def test_probe_returns_none_means_caller_falls_through(self, monkeypatch):
        # If yt-dlp can't extract anything (private/region/login-walled),
        # transcribe_video returns None — the orchestrator falls back to
        # the OG-only path.
        monkeypatch.setattr(vt.settings, "video_transcribe_enabled", True)
        monkeypatch.setattr(vt, "_YTDLP_AVAILABLE", True)
        monkeypatch.setattr(vt, "_probe_duration", lambda _url: None)
        result = asyncio.run(transcribe_video("https://www.instagram.com/reel/abc/"))
        assert result is None

    def test_too_long_returns_skipped(self, monkeypatch):
        monkeypatch.setattr(vt.settings, "video_transcribe_enabled", True)
        monkeypatch.setattr(vt.settings, "video_max_duration_seconds", 60)
        monkeypatch.setattr(vt, "_YTDLP_AVAILABLE", True)
        monkeypatch.setattr(
            vt, "_probe_duration",
            lambda _url: {"duration": 9999.0, "title": "Long talk", "extractor_key": "Generic"},
        )
        result = asyncio.run(transcribe_video("https://www.youtube.com/watch?v=long"))
        assert isinstance(result, TranscriptionSkipped)
        assert result.reason == "video_too_long"
        assert result.duration_seconds == 9999.0

    def test_download_fails_returns_none(self, monkeypatch, tmp_path):
        # Probe says short-enough video; download returns None (network
        # error mid-stream, codec issue, etc.). transcribe_video returns
        # None and the orchestrator falls back to OG.
        monkeypatch.setattr(vt.settings, "video_transcribe_enabled", True)
        monkeypatch.setattr(vt.settings, "video_max_duration_seconds", 600)
        monkeypatch.setattr(vt.settings, "video_temp_dir", str(tmp_path))
        monkeypatch.setattr(vt, "_YTDLP_AVAILABLE", True)
        monkeypatch.setattr(
            vt, "_probe_duration",
            lambda _url: {"duration": 30.0, "title": "Quick reel", "extractor_key": "Instagram"},
        )
        monkeypatch.setattr(vt, "_download_audio", lambda _url, _into: None)
        result = asyncio.run(transcribe_video("https://www.instagram.com/reel/abc/"))
        assert result is None

    def test_happy_path_with_mocked_subprocess(self, monkeypatch, tmp_path):
        # End-to-end with yt-dlp + whisper both stubbed: ensure we
        # construct a TranscriptionResult with the right fields when
        # everything works.
        monkeypatch.setattr(vt.settings, "video_transcribe_enabled", True)
        monkeypatch.setattr(vt.settings, "video_max_duration_seconds", 600)
        monkeypatch.setattr(vt.settings, "video_temp_dir", str(tmp_path))
        monkeypatch.setattr(vt, "_YTDLP_AVAILABLE", True)

        info = {"duration": 42.0, "title": "Reel by @user", "extractor_key": "Instagram"}
        monkeypatch.setattr(vt, "_probe_duration", lambda _url: info)

        # Pretend yt-dlp wrote an audio file to disk
        fake_audio = tmp_path / "fake.m4a"
        fake_audio.write_bytes(b"\x00\x01")
        monkeypatch.setattr(vt, "_download_audio", lambda _url, _into: fake_audio)

        async def fake_whisper(_audio: Path) -> str:
            return "Hello, this is the transcript of a reel."
        monkeypatch.setattr(vt, "_run_whisper", fake_whisper)

        result = asyncio.run(transcribe_video("https://www.instagram.com/reel/abc/"))
        assert isinstance(result, TranscriptionResult)
        assert "transcript of a reel" in result.transcript
        assert result.duration_seconds == 42.0
        assert result.title == "Reel by @user"
        assert result.extractor == "Instagram"

    def test_whisper_returns_empty_means_none(self, monkeypatch, tmp_path):
        # Whisper produced an empty file — treat as no usable transcript.
        monkeypatch.setattr(vt.settings, "video_transcribe_enabled", True)
        monkeypatch.setattr(vt.settings, "video_max_duration_seconds", 600)
        monkeypatch.setattr(vt.settings, "video_temp_dir", str(tmp_path))
        monkeypatch.setattr(vt, "_YTDLP_AVAILABLE", True)
        monkeypatch.setattr(
            vt, "_probe_duration",
            lambda _url: {"duration": 12.0, "title": "Silent clip", "extractor_key": "Instagram"},
        )
        fake_audio = tmp_path / "fake.m4a"
        fake_audio.write_bytes(b"\x00")
        monkeypatch.setattr(vt, "_download_audio", lambda _url, _into: fake_audio)

        async def empty_whisper(_audio: Path) -> str | None:
            return ""
        monkeypatch.setattr(vt, "_run_whisper", empty_whisper)

        result = asyncio.run(transcribe_video("https://www.instagram.com/reel/abc/"))
        assert result is None
