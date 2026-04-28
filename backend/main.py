"""BrainTwin — FastAPI Backend Entry Point."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend.capture.processor import CaptureInput, process
from backend.config import settings


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


app = FastAPI(
    title="BrainTwin",
    description="Your Knowledge Twin Agent",
    version="0.1.0",
)

# Allow Chrome extension to talk to local backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Phase 1 persistence: append each processed capture to a JSONL file.
# Phase 2 will replace this with ChromaDB + SQLite.
CAPTURES_LOG = Path("./data/captures.jsonl")
FAILURES_LOG = Path(settings.capture_failures_path)


def _log_failure(*, source: str, reason: str, payload: dict[str, Any]) -> None:
    """Append a structured failure row to data/capture_failures.jsonl.

    Best-effort — never raises. Used by both Chrome and Telegram clients;
    the `source` field tells them apart. Phase 2's failure-summary agent
    reads this file.
    """
    try:
        FAILURES_LOG.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "source": source,                # "chrome" | "telegram" | "unknown"
            "url": payload.get("url"),
            "title": payload.get("title"),
            "platform": payload.get("platform"),
            "reason": reason,
            "text_preview": (payload.get("text") or "")[:200],
        }
        with FAILURES_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        logger.exception("Could not write to capture_failures.jsonl")


# --- Models ---

class CapturePayload(BaseModel):
    url: str
    title: str
    platform: str = "general"
    content_type: str = "article"
    text: str
    images: list[str] = []  # URLs or base64 data URLs
    timestamp: datetime | None = None
    dwell_time_seconds: int = 0
    metadata: dict[str, Any] = {}


class QuestionPayload(BaseModel):
    question: str


# --- Routes ---

@app.get("/")
async def root():
    return {
        "name": "BrainTwin",
        "status": "running",
        "version": "0.1.0",
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "vision_api_configured": bool(settings.anthropic_api_key),
    }


def _detect_source(payload: CapturePayload) -> str:
    """Best-effort guess at which client sent this capture, for failure logs."""
    plat = (payload.platform or "").lower()
    if plat.startswith("telegram"):
        return "telegram"
    if (payload.url or "").startswith("tg://"):
        return "telegram"
    if payload.url and payload.url.startswith(("http://", "https://")):
        return "chrome"
    return "unknown"


@app.post("/capture")
async def capture_content(payload: CapturePayload):
    """Receive captured content from Chrome extension or Telegram bot.

    Phase 1: runs the extraction + vision pipeline and appends the
    processed result to data/captures.jsonl. Phase 2 will add enrichment
    and proper storage (ChromaDB + SQLite).
    """
    source = _detect_source(payload)
    try:
        capture = CaptureInput(
            url=payload.url,
            title=payload.title,
            platform=payload.platform,
            content_type=payload.content_type,
            text=payload.text,
            images=payload.images,
            timestamp=payload.timestamp,
            dwell_time_seconds=payload.dwell_time_seconds,
            metadata=payload.metadata,
        )
        # If no API key is set, skip the Vision call so the pipeline still
        # works end-to-end for local testing.
        skip_vision = not bool(settings.anthropic_api_key)
        processed = process(capture, skip_vision_api=skip_vision)
    except Exception as e:
        logger.exception("Capture processing failed")
        _log_failure(source=source, reason=f"processing: {e}", payload=payload.model_dump(mode="json"))
        raise HTTPException(status_code=500, detail=f"processing failed: {e}")

    # Append to JSONL log (Phase 2 will migrate this into ChromaDB/SQLite)
    try:
        CAPTURES_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CAPTURES_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(processed.to_dict(), ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("Failed to persist capture: %s", e)
        _log_failure(source=source, reason=f"persist: {e}", payload=payload.model_dump(mode="json"))

    return {
        "status": "captured",
        "url": processed.url,
        "title": processed.title,
        "platform": processed.platform,
        "text_length": len(processed.clean_text),
        "text_source": processed.text_source,
        "has_transcript": bool(processed.transcript),
        "images_processed": len(processed.image_descriptions),
        "vision_skipped": skip_vision,
    }


@app.post("/ask")
async def ask_agent(payload: QuestionPayload):
    """Ask the BrainTwin agent a question."""
    # TODO: Phase 4 — wire up agent
    # 1. Semantic search in ChromaDB
    # 2. Entity search in SQLite
    # 3. Merge and deduplicate results
    # 4. Build RAG prompt with knowledge context
    # 5. Call Claude API
    # 6. Return answer

    return {
        "question": payload.question,
        "answer": "Agent not yet connected. Build Phase 4 to enable this.",
        "sources": [],
    }


@app.get("/stats")
async def get_stats():
    """Get knowledge base statistics."""
    total = 0
    platforms: dict[str, int] = {}
    last_capture: str | None = None
    if CAPTURES_LOG.exists():
        with CAPTURES_LOG.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total += 1
                plat = rec.get("platform", "unknown")
                platforms[plat] = platforms.get(plat, 0) + 1
                last_capture = rec.get("timestamp") or last_capture

    return {
        "total_captures": total,
        "total_entities": 0,  # Populated once Phase 2 enrichment lands.
        "platforms": platforms,
        "last_capture": last_capture,
    }


@app.get("/failures")
async def get_failures(limit: int = 10):
    """Last N capture failures from data/capture_failures.jsonl.

    Read by the bot's `/failures` command and (later) by the digest agent.
    """
    rows: list[dict[str, Any]] = []
    if FAILURES_LOG.exists():
        with FAILURES_LOG.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return {
        "total": len(rows),
        "recent": rows[-limit:],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=True)
