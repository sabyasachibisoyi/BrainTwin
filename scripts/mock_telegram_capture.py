"""Offline simulator for the Telegram → /capture path.

Builds the exact CapturePayload shapes that backend/telegram_bot/handlers.py
would produce for each content type (text-with-URL, image, image-album,
forwarded message), and POSTs them to the running backend. Useful when
you don't want to spam your real bot or when the bot itself is down.

Usage:
    # 1. Start backend in another terminal:
    uvicorn backend.main:app --reload --port 8000

    # 2. Run this script:
    python scripts/mock_telegram_capture.py

    # Pick which scenarios to run:
    python scripts/mock_telegram_capture.py --only text,image

Stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# A 1x1 transparent PNG, base64'd. Stand-in for "I forwarded a meme".
TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
TINY_DATA_URL = f"data:image/png;base64,{TINY_PNG_B64}"


SCENARIOS: dict[str, dict] = {
    "text": {
        "url": "https://www.bbc.com/news/world-asia-india-67890123",
        "title": "Telegram link",
        "platform": "general",
        "content_type": "article",
        "text": "",
        "images": [],
        "timestamp": _now_iso(),
        "dwell_time_seconds": 0,
        "metadata": {
            "source": "telegram",
            "telegram_message_id": 1001,
            "chat_id": 999999,
        },
    },
    "image": {
        "url": "tg://message/999999/1002",
        "title": "WhatsApp meme about Mumbai monsoon",
        "platform": "telegram_image",
        "content_type": "image",
        "text": "WhatsApp meme about Mumbai monsoon",
        "images": [TINY_DATA_URL],
        "timestamp": _now_iso(),
        "dwell_time_seconds": 0,
        "metadata": {
            "source": "telegram",
            "telegram_message_id": 1002,
            "chat_id": 999999,
            "image_count": 1,
        },
    },
    "album": {
        "url": "tg://message/999999/1003",
        "title": "Family photos from Diwali",
        "platform": "telegram_image",
        "content_type": "image",
        "text": "Family photos from Diwali",
        "images": [TINY_DATA_URL, TINY_DATA_URL, TINY_DATA_URL],
        "timestamp": _now_iso(),
        "dwell_time_seconds": 0,
        "metadata": {
            "source": "telegram",
            "telegram_message_id": 1003,
            "chat_id": 999999,
            "image_count": 3,
        },
    },
    "forward": {
        "url": "https://timesofindia.indiatimes.com/india/some-news-article",
        "title": "Telegram link",
        "platform": "general",
        "content_type": "article",
        "text": "",
        "images": [],
        "timestamp": _now_iso(),
        "dwell_time_seconds": 0,
        "metadata": {
            "source": "telegram",
            "telegram_message_id": 1004,
            "chat_id": 999999,
            "forwarded": True,
            "forward_sender_user": "Uncle Ramesh",
            "forward_date": _now_iso(),
        },
    },
}


def http_json(method: str, url: str, body: dict | None = None, timeout: float = 30.0) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
            return resp.status, json.loads(payload) if payload else {}
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.reason, "body": e.read().decode("utf-8", errors="replace")}
    except urllib.error.URLError as e:
        return 0, {"error": str(e.reason)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", default="http://127.0.0.1:8000", help="Backend base URL")
    ap.add_argument(
        "--only",
        default="",
        help=f"Comma-separated subset of scenarios to run. Available: {','.join(SCENARIOS)}",
    )
    args = ap.parse_args()

    print(f"→ Backend: {args.backend}\n")

    # Health check first
    status, body = http_json("GET", f"{args.backend}/health")
    if status == 0:
        print(f"✗ Could not reach backend ({body.get('error')})")
        print("  Is uvicorn running? `uvicorn backend.main:app --reload --port 8000`")
        return 1
    print(f"[health] {status} {body}\n")

    # Stats before
    _, before = http_json("GET", f"{args.backend}/stats")
    before_total = before.get("total_captures", 0)
    print(f"[stats before] total_captures = {before_total}\n")

    # Pick scenarios
    names = [n.strip() for n in args.only.split(",") if n.strip()] or list(SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        print(f"✗ Unknown scenario(s): {unknown}. Available: {list(SCENARIOS)}")
        return 1

    fails = 0
    for name in names:
        payload = SCENARIOS[name]
        # refresh timestamp on each so the rows look real
        payload = {**payload, "timestamp": _now_iso()}
        print(f"--- scenario: {name} ---")
        print(f"  url      = {payload['url']}")
        print(f"  platform = {payload['platform']}")
        print(f"  images   = {len(payload['images'])}")
        status, body = http_json("POST", f"{args.backend}/capture", payload)
        print(f"  ← {status} {body}\n")
        if status != 200:
            fails += 1

    _, after = http_json("GET", f"{args.backend}/stats")
    delta = after.get("total_captures", 0) - before_total
    print(f"[stats after]  total_captures = {after.get('total_captures')}  (delta {delta:+})")

    if fails or delta < len([n for n in names if n in SCENARIOS]):
        print("\n✗ Some scenarios did not register. Check backend logs.")
        return 1

    print("\n✓ All Telegram-shaped scenarios round-tripped through /capture.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
