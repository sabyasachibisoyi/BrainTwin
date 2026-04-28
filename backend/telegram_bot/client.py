"""Async HTTP client for posting captures to the BrainTwin backend.

Same wire shape the Chrome extension uses — backend doesn't need to know
which client it came from. Wraps httpx so we get connection pooling and
sane timeouts. Also enforces a small rate-limit so a backlog drain
doesn't hammer the backend.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from backend.config import settings


logger = logging.getLogger(__name__)


class CaptureClient:
    """Single httpx.AsyncClient + a 1-token-bucket rate limiter."""

    def __init__(
        self,
        base_url: str | None = None,
        min_interval_ms: int | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self.url = base_url or settings.backend_capture_url
        self._min_interval_s = (min_interval_ms or settings.telegram_post_min_interval_ms) / 1000.0
        self._lock = asyncio.Lock()
        self._last_post_at: float = 0.0
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def post_capture(self, payload: dict[str, Any]) -> tuple[bool, dict[str, Any] | str]:
        """POST a CapturePayload-shaped dict to /capture.

        Returns (ok, body_or_reason). On non-2xx or transport error,
        returns a short human-readable reason that the bot uses verbatim
        in its "⚠️ Couldn't process" reply.
        """
        async with self._lock:
            # Throttle drains
            since = time.monotonic() - self._last_post_at
            if since < self._min_interval_s:
                await asyncio.sleep(self._min_interval_s - since)

            try:
                resp = await self._client.post(self.url, json=payload)
            except httpx.RequestError as e:
                self._last_post_at = time.monotonic()
                logger.warning("POST /capture transport error: %s", e)
                return False, _shorten_reason(f"backend unreachable ({e.__class__.__name__})")

            self._last_post_at = time.monotonic()

            if resp.status_code >= 400:
                body_preview = resp.text[:200] if resp.text else ""
                logger.warning("POST /capture HTTP %s: %s", resp.status_code, body_preview)
                return False, _shorten_reason(f"backend HTTP {resp.status_code}: {body_preview}")

            try:
                return True, resp.json()
            except ValueError:
                return True, {"status": "captured"}


def _shorten_reason(s: str, n: int = 140) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"
