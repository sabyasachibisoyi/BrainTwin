"""BrainTwin Configuration — loads settings from .env file."""

from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    # API Keys
    anthropic_api_key: str = ""
    telegram_bot_token: str = ""

    # Telegram allowlist — comma-separated Telegram user IDs that the bot
    # will accept messages from. Anything outside this list is silently
    # ignored. Send /start to the bot once and it'll DM you your own ID.
    allowed_telegram_user_ids: str = ""

    # Server
    backend_host: str = "127.0.0.1"
    backend_port: int = 8000

    # Capture
    dwell_time_threshold: int = 30

    # Telegram bot
    # URL the bot posts captures to. Same machine as the bot in Phase 1;
    # change this when you move the bot to cloud (see phase1-design.md Part 5).
    backend_capture_url: str = "http://127.0.0.1:8000/capture"
    # Throttle when draining a backlog after the bot reconnects, so a
    # weekend's worth of memes doesn't hammer the backend.
    telegram_post_min_interval_ms: int = 800
    # If we detect a gap larger than this between the last processed
    # message and the next one, send a "caught up on N messages" heads-up.
    telegram_catchup_gap_minutes: int = 720  # 12 hours

    # LLM Models
    enrichment_model: str = "claude-haiku-4-5-20251001"
    agent_model: str = "claude-sonnet-4-6"

    # Database Paths
    chroma_path: str = "./data/chroma"
    sqlite_path: str = "./data/braintwin.db"
    images_path: str = "./data/images"

    # Telegram bot state (pause flag, last-seen timestamp)
    telegram_state_path: str = "./data/telegram_state.json"
    # Persistent failure log (used by both Chrome and Telegram clients).
    # Phase 2 also writes to this file with `phase: "enrichment"` for
    # enrichment failures.
    capture_failures_path: str = "./data/capture_failures.jsonl"

    # ----- Phase 2 (LLM enrichment) -----
    # Sidecar to captures.jsonl — one row per successful enrichment, keyed
    # by capture_id. Joined at read time. See docs/phase2-design.md.
    enrichments_path: str = "./data/enrichments.jsonl"
    # Roughly the upper bound on what we'll send to Haiku in one shot.
    # Above this, enrichment skips with reason "content_too_long" (rare —
    # only multi-hour transcripts and books exceed this).
    enrichment_max_input_chars: int = 200_000  # ~50k tokens
    # The user's languages — fed verbatim into the enrichment system prompt
    # so Haiku knows to expect code-switching across these. Add/remove as
    # the user's consumption profile changes. Phase-5+ improvement: load
    # from .env per Decision D's portability note.
    user_languages: str = "English, Hindi, Odia, Telugu, German"

    # ----- Phase 2.5 (capture hydration) -----
    # Sidecar to captures.jsonl — one row per hydration that filled in
    # an empty capture from OG / Twitter Card / HTML metadata or (Fix 3)
    # video transcription. Original captures.jsonl row stays untouched
    # as audit trail; consumers join via capture_id. See
    # docs/phase2.5-capture-hydration.md.
    hydrations_path: str = "./data/hydrations.jsonl"
    # OG fetcher network budget. 5s is generous for the first byte and
    # tight enough that one slow site doesn't stall a worker.
    og_fetch_timeout_seconds: float = 5.0
    # Cap redirects so we don't get strung along chain-of-shorteners.
    og_fetch_max_redirects: int = 2
    # Toggle for the OG fetcher. Set false to fall back to pre-Phase-2.5
    # behaviour (empty captures land in enrichment_skipped immediately).
    # Useful if a particular site hangs the worker and we want to ship
    # the disable without a redeploy.
    og_fetch_enabled: bool = True

    # ----- Phase 2.5 Fix 3 (local video transcription) -----
    # Master kill-switch for yt-dlp + whisper.cpp pipeline. Set false to
    # fully bypass video transcription (useful when whisper-cli isn't
    # installed yet, or when shipping the code before the model is
    # downloaded). Default true — let the orchestrator decide per URL.
    video_transcribe_enabled: bool = True
    # Hard cap on video length in seconds. Anything longer is logged as
    # enrichment_skipped with reason "video_too_long" — most reels are
    # <90s, podcast clips <10 min; longer than that probably wants its
    # own handling (multi-chunk transcribe, summarize-then-merge).
    video_max_duration_seconds: int = 600  # 10 minutes
    # Where the whisper.cpp model lives. Downloaded once via
    # scripts/setup_whisper.sh after `brew install whisper-cpp`.
    # Gitignored — too big and per-machine.
    whisper_model_path: str = "./data/models/ggml-small.en.bin"
    # Path to the whisper-cli binary. Default matches `brew install
    # whisper-cpp` on Apple-silicon Macs (Intel Macs use /usr/local/bin).
    # Override via .env if your homebrew prefix differs.
    whisper_binary_path: str = "/usr/local/bin/whisper-cli"
    # Where yt-dlp drops the temp audio file before whisper consumes it.
    # System /tmp is fine; we delete after each transcription. Configurable
    # so a future on-disk-encrypted /tmp doesn't slow runs unexpectedly.
    video_temp_dir: str = "/tmp"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    # ----- Helpers -----

    def allowed_telegram_ids(self) -> set[int]:
        """Parse ALLOWED_TELEGRAM_USER_IDS into a set of ints. Empty = no one allowed."""
        if not self.allowed_telegram_user_ids:
            return set()
        out: set[int] = set()
        for part in self.allowed_telegram_user_ids.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.add(int(part))
            except ValueError:
                # Bad entry — skip rather than crash the bot on startup.
                continue
        return out


# Singleton settings instance
settings = Settings()

# Ensure data directories exist
Path(settings.chroma_path).mkdir(parents=True, exist_ok=True)
Path(settings.images_path).mkdir(parents=True, exist_ok=True)
Path(settings.telegram_state_path).parent.mkdir(parents=True, exist_ok=True)
