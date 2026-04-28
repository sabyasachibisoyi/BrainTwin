"""End-to-end smoke test for the BrainTwin /capture endpoint.

Mimics exactly what extension/content.js POSTs to the backend, then reads
back /stats and tails data/captures.jsonl to confirm the round-trip.

Usage:
    # 1. Start the backend in one terminal:
    uvicorn backend.main:app --reload --port 8000

    # 2. Run this script in another:
    python scripts/mock_capture.py

    # Or hit a different host/port:
    python scripts/mock_capture.py --backend http://127.0.0.1:8000

Stdlib only — no install needed beyond Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


# A realistic payload — same shape extension/content.js builds in
# capturePageContent(). Edit the text/url to test different platforms.
SAMPLE_PAYLOAD = {
    "url": "https://en.wikipedia.org/wiki/Knowledge_graph",
    "title": "Knowledge graph - Wikipedia",
    "platform": "general",
    "content_type": "article",
    "text": (
        "In knowledge representation and reasoning, a knowledge graph is a "
        "knowledge base that uses a graph-structured data model or topology "
        "to represent and operate on data. Knowledge graphs are often used "
        "to store interlinked descriptions of entities — objects, events, "
        "situations or abstract concepts — while also encoding the free-form "
        "semantics or relationships underlying these entities.\n\n"
        "Since the development of the Semantic Web, knowledge graphs have "
        "often been associated with linked open data projects, focusing on "
        "the connections between concepts and entities. They are also "
        "prominently associated with and used by search engines such as "
        "Google, Bing, Yext and Yahoo; knowledge engines and question "
        "answering services such as WolframAlpha, Apple's Siri, and Amazon "
        "Alexa; and social networks such as LinkedIn and Facebook."
    ),
    "images": [],  # add a https://... image URL to exercise vision
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "dwell_time_seconds": 47,
    "metadata": {
        "description": "Wikipedia article on knowledge graphs",
        "author": "Wikipedia contributors",
    },
}


def http_json(method: str, url: str, body: dict | None = None, timeout: float = 10.0) -> tuple[int, dict]:
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
        "--captures-log",
        default="data/captures.jsonl",
        help="Path to JSONL capture log (relative to project root)",
    )
    args = ap.parse_args()

    print(f"→ Backend: {args.backend}")

    # 1. Health check
    print("\n[1/4] GET /health")
    status, body = http_json("GET", f"{args.backend}/health")
    if status == 0:
        print(f"  ✗ Could not reach backend ({body.get('error')})")
        print("  Is uvicorn running? `uvicorn backend.main:app --reload --port 8000`")
        return 1
    print(f"  ← {status} {body}")
    if status != 200:
        return 1

    # 2. Stats before
    print("\n[2/4] GET /stats (before)")
    _, before = http_json("GET", f"{args.backend}/stats")
    print(f"  ← {before}")
    before_total = before.get("total_captures", 0)

    # 3. Capture
    print("\n[3/4] POST /capture (mock payload)")
    print(f"  payload.url   = {SAMPLE_PAYLOAD['url']}")
    print(f"  payload.title = {SAMPLE_PAYLOAD['title']}")
    print(f"  payload.text  = {len(SAMPLE_PAYLOAD['text'])} chars")
    status, body = http_json("POST", f"{args.backend}/capture", SAMPLE_PAYLOAD)
    print(f"  ← {status} {body}")
    if status != 200:
        return 1

    # 4. Stats after + JSONL tail
    print("\n[4/4] GET /stats (after)")
    _, after = http_json("GET", f"{args.backend}/stats")
    print(f"  ← {after}")

    delta = after.get("total_captures", 0) - before_total
    print(f"\n  total_captures: {before_total} → {after.get('total_captures')}  (delta {delta:+})")

    log = Path(args.captures_log)
    if log.exists():
        last_line = log.read_text(encoding="utf-8").strip().splitlines()[-1]
        try:
            rec = json.loads(last_line)
            print(f"  last row in {log}: url={rec.get('url')!r} text_source={rec.get('text_source')!r}")
        except json.JSONDecodeError:
            print(f"  (couldn't parse last line of {log})")
    else:
        print(f"  (no {log} file yet — backend may be writing elsewhere)")

    if delta < 1:
        print("\n✗ Capture did not register. Check backend logs.")
        return 1

    print("\n✓ End-to-end capture path works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
