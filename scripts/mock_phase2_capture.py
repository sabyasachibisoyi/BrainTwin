"""End-to-end smoke test for the BrainTwin Phase 2 enrichment path.

Sends a real-shaped capture payload, polls `/stats` until the
enrichment count rises (or a matching failure row appears in
capture_failures.jsonl), and prints the result. Proves the path:
extension/bot → /capture → SQL captures row → FastAPI BackgroundTasks
→ enrichment_worker → Haiku → SQL enrichments + Chroma chunks.

Phase 3.5 update: this used to poll `data/enrichments.jsonl` for the
matching capture_id. After the cutover, enrichments live in SQL only.
The script now polls the `/stats` enrichments counter; the failures
log is still consulted for a fast-fail signal because that JSONL
survived the cutover (it's an ops record, not part of the knowledge
graph).

Requires:
  - Backend running with ANTHROPIC_API_KEY set (else enrichment is skipped
    and this script will time out — that's a useful signal).

Usage:
    # 1. Start the backend in one terminal:
    uvicorn backend.main:app --reload --port 8000

    # 2. Run this in another terminal:
    python scripts/mock_phase2_capture.py

    # Tweak timeout if your network is slow:
    python scripts/mock_phase2_capture.py --timeout 60

    # Hit a different host/port:
    python scripts/mock_phase2_capture.py --backend http://127.0.0.1:8000

Stdlib only — no install needed beyond Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path


# A realistic Indian-context payload — exercises Decision D (multi-language
# transliteration). Picks Bengaluru rent so we can sanity-check that the
# "₹" / "HSR Layout" / "1BHK" survive into key_facts.
SAMPLE_PAYLOAD = {
    "url": "https://www.hindustantimes.com/cities/bengaluru-news/hsr-layout-rent-crisis-phase2-test",
    "title": "HSR Layout 1BHK rents jump as supply tightens",
    "platform": "general",
    "content_type": "article",
    "text": (
        "Bengaluru's HSR Layout has become unaffordable for young renters, "
        "a viral X post claimed this week. The author, a 28-year-old "
        "marketing professional, said her budget was ₹15,000 for a 1BHK "
        "but every listing in HSR started at ₹25,000. Hindustan Times "
        "picked up the post on Friday. Real estate agents quoted in the "
        "article say HSR rents have climbed roughly 40% in the past two "
        "years, driven by tech-sector demand and a shortage of new "
        "construction in the area. One agent said it's classic jugaad — "
        "people are splitting 2BHKs three ways to make the math work."
    ),
    "images": [],
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "dwell_time_seconds": 47,
    "metadata": {
        "description": "Phase 2 smoke test — Bengaluru rent article",
        "author": "BrainTwin smoke test",
    },
}


def http_json(
    method: str, url: str, body: dict | None = None, timeout: float = 10.0,
) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
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


def find_failure(path: Path, capture_id: str) -> dict | None:
    """Scan capture_failures.jsonl for an enrichment failure for this id.

    The failures log survives the Phase 3.5 cutover as an ops record."""
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("phase") == "enrichment" and row.get("capture_id") == capture_id:
                return row
    return None


def _enriched_total(backend: str) -> int:
    _, body = http_json("GET", f"{backend}/stats")
    return int((body.get("enrichments") or {}).get("total", 0))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--backend", default="http://127.0.0.1:8000")
    ap.add_argument("--failures-log", default="data/capture_failures.jsonl")
    ap.add_argument(
        "--timeout", type=int, default=30,
        help="Seconds to wait for enrichment to appear (default 30)",
    )
    args = ap.parse_args()

    print(f"→ Backend: {args.backend}")

    # 1. Health check
    print("\n[1/5] GET /health")
    status, body = http_json("GET", f"{args.backend}/health")
    if status == 0:
        print(f"  ✗ Could not reach backend ({body.get('error')})")
        print("  Start it with: uvicorn backend.main:app --reload --port 8000")
        return 1
    print(f"  ← {status} {body}")
    if not body.get("vision_api_configured"):
        print(
            "  ⚠ ANTHROPIC_API_KEY is empty — enrichment will be SKIPPED. "
            "This script will time out. Set the key in .env and restart "
            "the backend to actually exercise Phase 2."
        )

    # 2. Stats before
    print("\n[2/5] GET /stats (before)")
    _, before = http_json("GET", f"{args.backend}/stats")
    print(f"  ← {before}")
    before_total = before.get("total_captures", 0)
    before_enriched = (before.get("enrichments") or {}).get("total", 0)

    # 3. Capture (mint a deterministic-looking capture_id so we can spot
    # it in the failures log during debugging)
    capture_id = str(uuid.uuid4())
    payload = {**SAMPLE_PAYLOAD, "capture_id": capture_id}
    print("\n[3/5] POST /capture")
    print(f"  capture_id    = {capture_id}")
    print(f"  payload.url   = {payload['url']}")
    print(f"  payload.title = {payload['title']}")
    print(f"  payload.text  = {len(payload['text'])} chars")
    status, body = http_json("POST", f"{args.backend}/capture", payload)
    print(f"  ← {status} {body}")
    if status != 200:
        return 1
    if not body.get("enrichment_scheduled"):
        print(
            "  ⚠ enrichment_scheduled=False — backend chose not to run "
            "enrichment (likely no API key or capture rejected). Bailing."
        )
        return 1

    # 4. Wait for enrichments counter to advance (or a failure row)
    print(f"\n[4/5] Polling /stats for enrichment delta (timeout {args.timeout}s)")
    fail_path = Path(args.failures_log)
    deadline = time.time() + args.timeout
    failure_row: dict | None = None
    succeeded = False
    while time.time() < deadline:
        try:
            after_enriched_now = _enriched_total(args.backend)
        except Exception:
            after_enriched_now = before_enriched
        if after_enriched_now > before_enriched:
            succeeded = True
            break
        failure_row = find_failure(fail_path, capture_id)
        if failure_row is not None:
            break
        time.sleep(1)

    if not succeeded and failure_row is None:
        elapsed = int(args.timeout)
        print(f"  ✗ Timed out after {elapsed}s — enrichments counter never moved.")
        print("  Check backend logs for traceback. Possible causes:")
        print("    - ANTHROPIC_API_KEY not set")
        print("    - Network blocked from reaching api.anthropic.com")
        print("    - Backend is single-worker but worker is hung")
        return 1

    if failure_row is not None:
        print("  ✗ Enrichment failure recorded:")
        print(f"    reason = {failure_row.get('reason')}")
        print(f"    {json.dumps(failure_row, indent=2, ensure_ascii=False)}")
        return 1

    print("  ✓ Enrichment counter advanced.")

    # 5. Stats after
    print("\n[5/5] GET /stats (after)")
    _, after = http_json("GET", f"{args.backend}/stats")
    print(f"  ← {after}")
    after_total = after.get("total_captures", 0)
    after_enriched = (after.get("enrichments") or {}).get("total", 0)
    print(
        f"\n  total_captures:    {before_total} → {after_total}  "
        f"(delta {after_total - before_total:+})"
    )
    print(
        f"  enriched captures: {before_enriched} → {after_enriched}  "
        f"(delta {after_enriched - before_enriched:+})"
    )

    print(
        "\n  --- Note ---\n"
        "  Phase 3.5: the per-capture enrichment block lives in SQL only.\n"
        "  Use `python scripts/inspect_storage.py --capture-id "
        f"{capture_id}` for the full enrichment + chunks view."
    )

    print("\n✓ Phase 2 enrichment path works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
