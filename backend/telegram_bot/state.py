"""Persistent state for the Telegram bot.

Tiny JSON file on disk — same idea as the Chrome extension's
chrome.storage.local. Survives bot restarts. Touched from a single
asyncio event loop so we don't bother with locks.

Schema:
    {
      "enabled": true,                          # /pause and /resume flip this
      "last_processed_message_at": "2026-04-27T18:42:11Z",  # for catch-up gap detection
      "media_groups_seen": ["1234567:567890", ...]          # de-dup window for albums
    }
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.config import settings


logger = logging.getLogger(__name__)
STATE_PATH = Path(settings.telegram_state_path)


_DEFAULT: dict[str, Any] = {
    "enabled": True,
    "last_processed_message_at": None,
    "media_groups_seen": [],
}


def _load() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return dict(_DEFAULT)
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        # Backfill any missing keys so future schema additions don't break us.
        for k, v in _DEFAULT.items():
            data.setdefault(k, v)
        return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not read %s (%s) — falling back to defaults", STATE_PATH, e)
        return dict(_DEFAULT)


def _save(data: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


# ---- Public API ----------------------------------------------------------


def is_enabled() -> bool:
    return bool(_load().get("enabled", True))


def set_enabled(value: bool) -> None:
    data = _load()
    data["enabled"] = bool(value)
    _save(data)


def get_last_processed_at() -> datetime | None:
    raw = _load().get("last_processed_message_at")
    if not raw:
        return None
    try:
        # tolerate both "...Z" and offset suffixes
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def mark_processed(when: datetime | None = None) -> None:
    when = when or datetime.now(timezone.utc)
    data = _load()
    data["last_processed_message_at"] = when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    _save(data)


def seen_media_group(media_group_id: str) -> bool:
    """Returns True if we've already opened a batch window for this album."""
    data = _load()
    seen = data.get("media_groups_seen") or []
    if media_group_id in seen:
        return True
    seen.append(media_group_id)
    # Keep a rolling window of recent IDs so the file doesn't grow forever.
    data["media_groups_seen"] = seen[-200:]
    _save(data)
    return False
