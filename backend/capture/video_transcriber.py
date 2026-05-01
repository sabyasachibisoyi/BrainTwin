"""Local video transcription — Phase 2.5 Fix 3.

Pulls audio from a video URL with yt-dlp, transcribes it locally with
whisper.cpp `small.en`. The resulting transcript is the only thing that
captures **what was said in a reel** — OG metadata gives us the post
caption, but the actual content of the spoken video is unique to this
path.

Pipeline per video URL:

    1. yt-dlp.extract_info(download=False) → metadata probe (cheap, no media)
    2. Reject if duration > video_max_duration_seconds  → caller logs skip
    3. yt-dlp.download() → temp .m4a in video_temp_dir
    4. whisper-cli subprocess → transcript text
    5. Delete temp audio
    6. Return TranscriptionResult or None

Design notes (per docs/phase2.5-capture-hydration.md Fix 3, user sign-off
2026-04-29):

  - Whisper model: small.en. ~244 MB, ~5s per 30s reel on M-series CPU.
  - Install path: `brew install whisper-cpp` → `/opt/homebrew/bin/whisper-cli`.
    Model downloaded once into `data/models/` via `scripts/setup_whisper.sh`.
    Both gitignored.
  - yt-dlp Python lib (not subprocess) — easier to test, cleaner errors.
  - Single guard: video_max_duration_seconds. Other guards (file size,
    concurrency) deliberately omitted at sign-off. yt-dlp errors fall
    through to the orchestrator which routes to enrichment_skipped.
  - This module is pure — no JSONL writes, no failure-log writes. The
    hydration orchestrator owns persistence so all hydration tiers go
    through one path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from backend.config import settings


logger = logging.getLogger(__name__)


# yt-dlp imported lazily so a missing install doesn't break the import
# graph during partial deploys (matches the selectolax pattern in
# og_fetcher.py).
try:
    import yt_dlp  # type: ignore
    _YTDLP_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised when dep is missing
    yt_dlp = None  # type: ignore[assignment]
    _YTDLP_AVAILABLE = False


# --- URL pattern matching --------------------------------------------------

# yt-dlp recognises ~1500 sites; matching its full extractor list is
# overkill. We only fire transcription when the URL is *probably* a
# short video, where transcription gives content the OG path can't.
# False negatives (treating a video URL as non-video) cost us a
# transcript we could have had — but the caller still tries OG, so the
# capture isn't lost. False positives (treating a non-video URL as a
# video) cost us one wasted yt-dlp probe; that's caught by the
# extract_info() metadata check before we download anything.
_VIDEO_URL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Instagram reels (the original failure case from Phase 2 smoke test)
    re.compile(r"^https?://(?:www\.)?instagram\.com/(?:reel|reels|p|tv)/", re.IGNORECASE),
    # Facebook video / share / watch links
    re.compile(r"^https?://(?:www\.|m\.|web\.)?facebook\.com/(?:watch|share|reel|video|.+/videos/)", re.IGNORECASE),
    re.compile(r"^https?://fb\.watch/", re.IGNORECASE),
    # TikTok
    re.compile(r"^https?://(?:www\.|vm\.|vt\.)?tiktok\.com/", re.IGNORECASE),
    # YouTube shorts / videos / mobile / short-link
    re.compile(r"^https?://(?:www\.|m\.|music\.)?youtube\.com/(?:watch|shorts|live)", re.IGNORECASE),
    re.compile(r"^https?://youtu\.be/", re.IGNORECASE),
    # X / Twitter video posts (status URLs that contain video are guessed
    # from path; yt-dlp will tell us if it's actually video at probe time)
    re.compile(r"^https?://(?:www\.|mobile\.)?(?:twitter|x)\.com/[^/]+/status/", re.IGNORECASE),
)

# Platforms (the bot's heuristic platform tag) that almost always carry
# video content — short-circuits the URL regex so weird IG/FB share URLs
# we didn't anticipate still flow into transcription.
_VIDEO_PLATFORMS: frozenset[str] = frozenset({
    "instagram", "instagram_reel",
    "facebook", "facebook_video",
    "tiktok",
    "youtube", "youtube_short",
})


def is_video_url(url: str, platform: str | None = None) -> bool:
    """Decide whether to attempt video transcription for this URL.

    Heuristic — pattern-matches the URL against known short-video hosts
    and falls back to the bot's `platform` label. We deliberately err
    toward over-firing here: the duration probe in `transcribe_video`
    is cheap (one HTTP HEAD-equivalent via yt-dlp) and rejects anything
    that isn't really a video.
    """
    if not url or not url.startswith(("http://", "https://")):
        return False
    if platform and platform.lower() in _VIDEO_PLATFORMS:
        return True
    return any(p.search(url) for p in _VIDEO_URL_PATTERNS)


# --- Result types ---------------------------------------------------------

@dataclass(frozen=True)
class TranscriptionResult:
    """What the pipeline produced. `transcript` is non-empty by
    construction — callers receive `None` instead when there's nothing
    useful to attach."""

    transcript: str
    duration_seconds: Optional[float]
    title: Optional[str]            # video title from yt-dlp (may be useful as fallback)
    extractor: Optional[str]        # which yt-dlp extractor handled it ("Instagram", "TikTok", ...)


@dataclass(frozen=True)
class TranscriptionSkipped:
    """Returned when we deliberately skipped transcription. The
    orchestrator turns this into a hydration sidecar note + (when
    nothing else worked) an enrichment_skipped row with the reason."""

    reason: str
    duration_seconds: Optional[float] = None


TranscriptionOutcome = TranscriptionResult | TranscriptionSkipped | None


# --- Pipeline -------------------------------------------------------------

# Default yt-dlp options. Audio-only download to keep the temp file
# small and the transcription latency low — Whisper only needs audio.
def _ydl_opts(temp_dir: Path, *, download: bool) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "noplaylist": True,
        # Fail fast on private / login-required URLs instead of prompting.
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
    }
    if download:
        opts.update({
            "outtmpl": str(temp_dir / "%(id)s.%(ext)s"),
            # Ask for an audio-only stream when one's available; falls
            # back to muxed video if not (whisper just ignores video).
            "format": "bestaudio/best",
        })
    return opts


def _probe_duration(url: str) -> dict[str, Any] | None:
    """Run yt-dlp's metadata probe (no download). Returns the info-dict
    or None on extractor error. Synchronous — yt-dlp is not async."""
    if not _YTDLP_AVAILABLE:
        return None
    with tempfile.TemporaryDirectory(dir=settings.video_temp_dir) as td:
        try:
            with yt_dlp.YoutubeDL(_ydl_opts(Path(td), download=False)) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:  # noqa: BLE001 — yt-dlp throws many things
            logger.info("yt-dlp probe failed for %s: %s", url, e)
            return None


def _download_audio(url: str, into: Path) -> Path | None:
    """Run yt-dlp download into `into`. Returns the path of the
    downloaded file or None on failure."""
    if not _YTDLP_AVAILABLE:
        return None
    try:
        with yt_dlp.YoutubeDL(_ydl_opts(into, download=True)) as ydl:
            info = ydl.extract_info(url, download=True)
        # yt-dlp resolves the actual filename from the info dict.
        if isinstance(info, dict):
            filename = info.get("requested_downloads", [{}])[0].get("filepath")
            if not filename:
                # Fall back to the templated path
                filename = ydl.prepare_filename(info)  # type: ignore[union-attr]
            if filename and Path(filename).exists():
                return Path(filename)
    except Exception as e:  # noqa: BLE001
        logger.info("yt-dlp download failed for %s: %s", url, e)
    return None


async def _convert_to_wav(src: Path) -> Path | None:
    """Convert any audio container (m4a / webm / opus / mp3 / etc.) to
    16 kHz mono signed-16-bit PCM WAV — the format whisper.cpp expects
    natively.

    Why this exists: whisper.cpp does NOT decode compressed audio
    formats. If you hand it an .m4a, it silently exits with returncode
    0, processes zero frames, writes no transcript, and leaves you
    debugging a "succeeded but no output" mystery. ffmpeg is the
    standard pre-step (it ships as a Homebrew dependency of
    whisper-cpp, so it's on PATH wherever the binary is).

    Returns the path to the produced .wav (alongside `src`) or None on
    failure. Caller should treat None as "transcription not possible
    for this clip" and fall through.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        logger.warning(
            "ffmpeg not on PATH — needed to convert %s for whisper. "
            "Install with `brew install ffmpeg`.", src.suffix,
        )
        return None
    wav_path = src.with_suffix(".wav")
    cmd = [
        ffmpeg,
        "-y",                  # overwrite if exists
        "-loglevel", "error",  # quiet — only complain on real errors
        "-i", str(src),
        "-ac", "1",            # mono
        "-ar", "16000",        # 16 kHz — whisper's native sample rate
        "-c:a", "pcm_s16le",   # signed 16-bit little-endian PCM
        str(wav_path),
    ]
    logger.debug("running ffmpeg: %s", " ".join(shlex.quote(c) for c in cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
    except Exception as e:  # noqa: BLE001
        logger.warning("ffmpeg launch failed: %s", e)
        return None
    if proc.returncode != 0:
        logger.warning(
            "ffmpeg exited %d converting %s: %s",
            proc.returncode, src.name,
            stderr.decode("utf-8", errors="replace")[-300:],
        )
        return None
    if not wav_path.exists() or wav_path.stat().st_size < 1024:
        logger.warning(
            "ffmpeg produced no/empty WAV at %s (size=%d bytes)",
            wav_path, wav_path.stat().st_size if wav_path.exists() else 0,
        )
        return None
    return wav_path


async def _run_whisper(audio_path: Path) -> str | None:
    """Run whisper-cli subprocess on `audio_path`. Returns transcript
    text or None on failure.

    Pre-step: if the input isn't already a 16 kHz mono PCM WAV, convert
    it via ffmpeg first. whisper.cpp will silently no-op on encoded
    formats like .m4a / .webm / .opus, so the conversion is mandatory
    not optional.

    Output discovery is version-tolerant: whisper.cpp's `-of <prefix>`
    flag means different things across releases (some write
    `<prefix>.txt`, some `<prefix>.<lang>.txt`, some ignore `-of` for
    txt and write `<input>.txt` next to the audio file, some print to
    stdout). We try the expected location, fall back to globbing the
    audio's parent dir for any `.txt`, and finally fall back to stdout."""
    binary = Path(settings.whisper_binary_path)
    model = Path(settings.whisper_model_path)
    if not binary.exists():
        logger.warning(
            "whisper-cli not found at %s — install with `brew install whisper-cpp`. "
            "Skipping transcription.", binary,
        )
        return None
    if not model.exists():
        logger.warning(
            "Whisper model not found at %s — run scripts/setup_whisper.sh "
            "to download. Skipping transcription.", model,
        )
        return None

    # ---- Convert to WAV if needed (whisper.cpp can't read m4a/etc.) --
    if audio_path.suffix.lower() != ".wav":
        wav_path = await _convert_to_wav(audio_path)
        if wav_path is None:
            return None
        # Use the WAV from here on. The original audio (and the WAV) get
        # cleaned up by the caller's tempdir rmtree.
        audio_path = wav_path

    # `-of <prefix>` — whisper writes `<prefix>.txt` (or some variant).
    # Strip the audio extension to give whisper a clean stem to append to.
    out_base = audio_path.with_suffix("")
    cmd = [
        str(binary),
        "-m", str(model),
        "-f", str(audio_path),
        "-otxt",                # text-only output (no JSON / SRT / VTT)
        "-of", str(out_base),
        "--no-timestamps",
        "-l", "en",             # small.en is English-only; explicit silences a warning
    ]
    logger.debug("running whisper: %s", " ".join(shlex.quote(c) for c in cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
    except Exception as e:  # noqa: BLE001
        logger.warning("whisper-cli launch failed: %s", e)
        return None

    if proc.returncode != 0:
        logger.warning(
            "whisper-cli exited %d: %s",
            proc.returncode, stderr_bytes.decode("utf-8", errors="replace")[:400],
        )
        return None

    # ---- Locate the transcript file (version-tolerant) ----------------
    # 1. Expected: <out_base>.txt  (e.g. /tmp/.../DHapcsgT7zC.txt)
    # 2. Lang-suffixed: <out_base>.en.txt  (some whisper.cpp builds)
    # 3. Anywhere in the audio's parent dir ending in .txt
    candidate_paths: list[Path] = [
        out_base.with_suffix(".txt"),
        out_base.with_suffix(".en.txt"),
        out_base.with_name(out_base.name + ".en.txt"),
    ]
    txt_path: Path | None = next((p for p in candidate_paths if p.exists()), None)
    if txt_path is None:
        # Glob the parent dir — catches any naming variant we didn't list.
        glob_hits = sorted(audio_path.parent.glob("*.txt"))
        if glob_hits:
            if len(glob_hits) > 1:
                logger.info(
                    "whisper-cli produced %d .txt files; using first: %s",
                    len(glob_hits), glob_hits[0],
                )
            txt_path = glob_hits[0]

    text: str | None = None
    if txt_path is not None and txt_path.exists():
        try:
            text = txt_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as e:
            logger.warning("Could not read whisper output %s: %s", txt_path, e)
        finally:
            try:
                txt_path.unlink()
            except OSError:
                pass
    else:
        # 4. Fall back to stdout — some whisper.cpp builds (or wrappers)
        # ignore -otxt for file output and just print to stdout.
        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        if stdout_text and len(stdout_text) > 8:
            logger.info(
                "whisper-cli produced no .txt file; using %d-char stdout instead.",
                len(stdout_text),
            )
            text = stdout_text
        else:
            # Diagnostics — list the directory so future-us can see what
            # whisper actually wrote, and dump a tail of stderr in case
            # whisper logged a warning we missed.
            try:
                listing = sorted(p.name for p in audio_path.parent.iterdir())
            except OSError:
                listing = []
            stderr_tail = stderr_bytes.decode("utf-8", errors="replace")[-300:]
            logger.warning(
                "whisper-cli succeeded but produced no readable transcript. "
                "Expected one of %s. Parent dir: %s. stderr tail: %s",
                [str(p.name) for p in candidate_paths],
                listing, stderr_tail,
            )
            return None

    return text or None


async def transcribe_video(url: str) -> TranscriptionOutcome:
    """Top-level entry point — probe, optionally download, transcribe.

    Returns:
      - TranscriptionResult — got a non-empty transcript.
      - TranscriptionSkipped — deliberately skipped (too long, wrong
        type, missing tooling). Caller decides whether to surface this
        to the user / failure log.
      - None — yt-dlp couldn't extract anything (private, region-locked,
        broken URL, missing dep). Hydration orchestrator falls through
        to the OG-only path.
    """
    if not settings.video_transcribe_enabled:
        return TranscriptionSkipped(reason="video_transcribe_disabled")
    if not _YTDLP_AVAILABLE:
        logger.info("yt-dlp not installed; transcription unavailable")
        return TranscriptionSkipped(reason="ytdlp_not_installed")

    # Step 1 — duration probe. Avoid downloading multi-GB streams.
    info = await asyncio.to_thread(_probe_duration, url)
    if info is None:
        return None
    duration = info.get("duration")
    if isinstance(duration, (int, float)) and duration > settings.video_max_duration_seconds:
        logger.info(
            "Skipping transcription for %s — duration %ds > cap %ds",
            url, int(duration), settings.video_max_duration_seconds,
        )
        return TranscriptionSkipped(reason="video_too_long", duration_seconds=float(duration))

    title = info.get("title") if isinstance(info, dict) else None
    extractor = info.get("extractor_key") if isinstance(info, dict) else None

    # Step 2 — download into a fresh temp dir we control, so cleanup is
    # one rmtree regardless of what yt-dlp / whisper leave behind.
    download_dir = Path(tempfile.mkdtemp(prefix="braintwin_yt_", dir=settings.video_temp_dir))
    try:
        audio_path = await asyncio.to_thread(_download_audio, url, download_dir)
        if audio_path is None:
            return None
        # Step 3 — transcribe.
        transcript = await _run_whisper(audio_path)
        if not transcript:
            return None
        return TranscriptionResult(
            transcript=transcript,
            duration_seconds=float(duration) if isinstance(duration, (int, float)) else None,
            title=title if isinstance(title, str) else None,
            extractor=extractor if isinstance(extractor, str) else None,
        )
    finally:
        # Best-effort cleanup. shutil.rmtree handles "directory still
        # has whisper-side files we don't know about".
        try:
            shutil.rmtree(download_dir, ignore_errors=True)
        except OSError as e:
            logger.debug("Could not remove temp dir %s: %s", download_dir, e)
        # Belt-and-braces: if anything escaped the temp dir, log but
        # don't crash. (Should never happen given we use mkdtemp.)
        if download_dir.exists():
            for entry in download_dir.iterdir():
                try:
                    os.remove(entry)
                except OSError:
                    pass
