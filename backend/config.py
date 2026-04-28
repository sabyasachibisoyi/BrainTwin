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
    # Persistent failure log (used by both Chrome and Telegram clients)
    capture_failures_path: str = "./data/capture_failures.jsonl"

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
