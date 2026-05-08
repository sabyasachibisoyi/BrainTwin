"""BrainTwin — FastAPI Backend Entry Point."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend.capture.processor import CaptureInput, process
from backend.config import settings
from backend.knowledge.enrichment_worker import (
    enqueue_enrichment,
    find_unenriched_capture_ids,
    hydrate_processed,
)
from backend.knowledge.llm_client import LLMClient, PermanentLLMError
from backend.storage import (
    DEFAULT_USER_ID,
    UserRepository,
    aclose as aclose_storage,
    init_db as init_storage_db,
    session_scope,
    sync_capture,
)


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
# Phase 3 will replace this with ChromaDB + SQLite.
CAPTURES_LOG = Path("./data/captures.jsonl")
FAILURES_LOG = Path(settings.capture_failures_path)
ENRICHMENTS_LOG = Path(settings.enrichments_path)


# ---- Phase 2: shared LLM client (constructed at startup) ------------
#
# Single AsyncAnthropic client is reused across requests so we don't
# pay TCP/TLS handshake on every capture. None when no API key is set
# (local dev without enrichment) — `/capture` will skip enrichment in
# that case rather than crash.
_llm_client: LLMClient | None = None


def _get_llm_client() -> LLMClient | None:
    return _llm_client


def _log_failure(*, source: str, reason: str, payload: dict[str, Any]) -> None:
    """Append a structured capture-phase failure row to data/capture_failures.jsonl.

    Best-effort — never raises. Used by both Chrome and Telegram clients;
    the `source` field tells them apart. Enrichment-phase failures are
    written by `enrichment_worker._log_enrichment_failure` with
    `phase: "enrichment"`. Phase 2's `/failures` endpoint groups by phase.
    """
    try:
        FAILURES_LOG.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "phase": "capture",              # Decision C — phase tag for grouping
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
    # Phase 2 join key. Optional from clients (extension/bot don't need
    # to generate one); we'll mint a UUID4 if omitted. Persisted in the
    # captures.jsonl row and used to join against enrichments.jsonl.
    capture_id: str | None = None


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


# ---- Startup / shutdown hooks --------------------------------------

async def _recover_unenriched() -> None:
    """At startup, find captures with no enrichment and re-queue them.

    Decision H — crash recovery. Runs once per process boot; emits one
    asyncio task per unenriched capture. Bounded by the number of
    unenriched rows in captures.jsonl, which is small in normal operation.
    """
    client = _get_llm_client()
    if client is None:
        return
    if not CAPTURES_LOG.exists():
        return

    unenriched_ids = set(find_unenriched_capture_ids(
        captures_path=CAPTURES_LOG,
        enrichments_path=ENRICHMENTS_LOG,
    ))
    if not unenriched_ids:
        return

    logger.info("Recovering %d unenriched captures from previous runs", len(unenriched_ids))
    queued = 0
    with CAPTURES_LOG.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = row.get("capture_id")
            if not cid or cid not in unenriched_ids:
                continue
            processed = hydrate_processed(row)
            if processed is None:
                logger.debug("Skipping recovery for %s — could not hydrate row", cid)
                continue
            asyncio.create_task(enqueue_enrichment(cid, processed, client))
            queued += 1
            unenriched_ids.discard(cid)
    if queued:
        logger.info("Re-queued %d unenriched captures", queued)


async def _ensure_default_user() -> None:
    """Phase 3 Step 4b — seed user_id=1 (Sabya, per B.5.4) on first
    startup if missing. Idempotent: silently no-op if already present.
    Required because the dual-write path stamps every capture with
    user_id=1 and chunks join through users via captures."""
    async with session_scope() as session:
        repo = UserRepository(session)
        existing = await repo.get(DEFAULT_USER_ID)
        if existing is None:
            await repo.create(
                email="sabya.bisoyi@gmail.com",
                display_name="Sabya",
                user_id=DEFAULT_USER_ID,
            )
            logger.info("Seeded default user_id=%s (Sabya)", DEFAULT_USER_ID)
        else:
            logger.debug("Default user_id=%s already present", DEFAULT_USER_ID)


@app.on_event("startup")
async def _startup() -> None:
    # Phase 3 Step 4b — initialize SQL schema + seed default user.
    # Runs even when ANTHROPIC_API_KEY is empty because /capture's
    # dual-write path mirrors into SQL regardless of enrichment status.
    # Best-effort: a SQL hiccup must not block app startup; the JSONL
    # path still works for the dual-write window.
    #
    # The flag short-circuit means "we are not writing to SQL this
    # run" — so don't even create the schema or seed users (avoids
    # touching the DB file at all when the operator is debugging a
    # broken SQL setup with dual_write off).
    if settings.storage_dual_write:
        # Independent try-blocks: a user-seed failure must not look
        # like an init failure, and vice versa. If init fails the
        # seed will too — that's fine, both get logged.
        try:
            await init_storage_db()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Phase 3 SQL schema init failed "
                "(dual-write to SQL effectively disabled this run): %s", e,
            )
        try:
            await _ensure_default_user()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Phase 3 default-user seed failed "
                "(sync_capture writes will FK-violate on user_id=%s): %s",
                DEFAULT_USER_ID, e,
            )

    global _llm_client
    if not settings.anthropic_api_key:
        logger.warning(
            "ANTHROPIC_API_KEY is empty — Phase 2 enrichment is DISABLED. "
            "Captures will still be persisted; set the key in .env to enable."
        )
        return
    try:
        _llm_client = LLMClient()
    except PermanentLLMError as e:
        logger.error("LLM client init failed: %s", e)
        return
    # Crash recovery — re-queue work that was in flight when we died.
    await _recover_unenriched()


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _llm_client
    if _llm_client is not None:
        try:
            await _llm_client.aclose()
        except Exception:  # noqa: BLE001
            logger.exception("Error closing LLM client")
        _llm_client = None
    # Phase 3 Step 4b — drain the SQL connection pool cleanly.
    try:
        await aclose_storage()
    except Exception:  # noqa: BLE001
        logger.exception("Error closing storage layer")


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
async def capture_content(payload: CapturePayload, background_tasks: BackgroundTasks):
    """Receive captured content from Chrome extension or Telegram bot.

    Pipeline (Phase 1 + Phase 2):
      1. Extract + vision (synchronous, in the request).
      2. Append the raw row to data/captures.jsonl with a capture_id.
      3. Schedule enrichment in the background — never blocks the
         response. Decision H — async enrichment via BackgroundTasks.

    Phase 3 will swap step 2 for ChromaDB + SQLite writes; the
    capture_id stays the join key.
    """
    source = _detect_source(payload)
    capture_id = payload.capture_id or str(uuid.uuid4())
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

    # Append to JSONL log. Phase 2: include capture_id so enrichments.jsonl
    # can be joined back to the raw row. Phase 3 dual-write also mirrors
    # into SQL via sync_capture below; once the dual-write window closes
    # (Phase 3.5) the JSONL writer goes away and SQL becomes the sole path.
    persisted = False
    try:
        CAPTURES_LOG.parent.mkdir(parents=True, exist_ok=True)
        row = {"capture_id": capture_id, **processed.to_dict()}
        with CAPTURES_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        persisted = True
    except OSError as e:
        logger.warning("Failed to persist capture: %s", e)
        _log_failure(source=source, reason=f"persist: {e}", payload=payload.model_dump(mode="json"))

    # Phase 3 Step 4b — dual-write the capture row into SQL. Best-effort
    # by design (sync_capture catches all errors internally) — a SQL
    # outage MUST NOT break the JSONL path during the dual-write window.
    # Idempotent on duplicate capture_id (silent no-op).
    #
    # Gated on `persisted`: JSONL is the authoritative store during
    # the dual-write window, so a JSONL failure means the capture
    # is logged-as-failed and we don't mirror an orphaned row into
    # SQL. Once the window closes (Phase 3.5) this gate goes away.
    sql_synced = False
    if persisted:
        sql_synced = await sync_capture(
            capture_id=capture_id,
            url=processed.url,
            title=processed.title,
            platform=processed.platform,
            content_type=processed.content_type,
            captured_at=processed.timestamp,
            dwell_seconds=processed.dwell_time_seconds,
            raw_metadata_json=(
                json.dumps(processed.metadata, ensure_ascii=False)
                if processed.metadata else None
            ),
        )

    # Phase 2 — schedule enrichment. Skipped if:
    #   - persistence failed (no row to enrich against)
    #   - no LLM client (no API key in env)
    enrichment_scheduled = False
    client = _get_llm_client()
    if persisted and client is not None:
        background_tasks.add_task(enqueue_enrichment, capture_id, processed, client)
        enrichment_scheduled = True

    return {
        "status": "captured",
        "capture_id": capture_id,
        "url": processed.url,
        "title": processed.title,
        "platform": processed.platform,
        "text_length": len(processed.clean_text),
        "text_source": processed.text_source,
        "has_transcript": bool(processed.transcript),
        "images_processed": len(processed.image_descriptions),
        "vision_skipped": skip_vision,
        "enrichment_scheduled": enrichment_scheduled,
        # Phase 3 Step 4b — dual-write status. False during the window
        # is non-fatal (capture is in JSONL); persistent False signals
        # SQL/Chroma trouble worth investigating.
        "sql_synced": sql_synced,
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
    capture_ids: set[str] = set()
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
                cid = rec.get("capture_id")
                if isinstance(cid, str) and cid:
                    capture_ids.add(cid)

    # Phase 2 — enrichment counts.
    enriched_ids: set[str] = set()
    total_entities = 0
    if ENRICHMENTS_LOG.exists():
        with ENRICHMENTS_LOG.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cid = rec.get("capture_id")
                if isinstance(cid, str) and cid:
                    enriched_ids.add(cid)
                ents = (rec.get("enrichment") or {}).get("entities") or []
                if isinstance(ents, list):
                    total_entities += len(ents)

    # Phase 2.5 Fix 1 — capture_ids deliberately skipped (empty / oversized
    # content). Subtract these from `pending` so the metric reflects real
    # backlog, not nothing-to-do work. Surface separately as `skipped`.
    skipped_ids: set[str] = set()
    if FAILURES_LOG.exists():
        with FAILURES_LOG.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("phase") != "enrichment_skipped":
                    continue
                cid = rec.get("capture_id")
                if isinstance(cid, str) and cid:
                    skipped_ids.add(cid)
    pending_enrichment = max(0, len(capture_ids - enriched_ids - skipped_ids))

    return {
        "total_captures": total,
        "total_entities": total_entities,
        "platforms": platforms,
        "last_capture": last_capture,
        "enrichments": {
            "total": len(enriched_ids),
            "pending": pending_enrichment,
            "skipped": len(skipped_ids),
        },
    }


@app.get("/failures")
async def get_failures(
    limit: int = 10,
    phase: str | None = None,
    include_skipped: bool = False,
):
    """Last N capture failures from data/capture_failures.jsonl.

    Read by the bot's `/failures` command and (later) by the digest agent.
    Phase tags (Decision C + Phase 2.5 Fix 1):
      - `capture`             — capture-side failure (default for legacy rows
                                that pre-date the phase field).
      - `enrichment`          — real enrichment failure (network, auth,
                                malformed JSON after retry, etc.).
      - `enrichment_skipped`  — nothing-to-enrich case (empty / oversized).
                                Excluded from the default response so the
                                failure metric reflects real failures only.

    Pass `?include_skipped=true` to see them in the breakdown, or
    `?phase=enrichment_skipped` to see only those rows.
    """
    rows: list[dict[str, Any]] = []
    if FAILURES_LOG.exists():
        with FAILURES_LOG.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Default older rows to phase=capture so the breakdown
                # always sums to the total.
                row.setdefault("phase", "capture")
                rows.append(row)

    if phase:
        # Explicit filter wins — caller asked for one phase.
        rows = [r for r in rows if r.get("phase") == phase]
    elif not include_skipped:
        # Phase 2.5 Fix 1 — by default, hide skipped rows from the
        # failures view so they don't pollute the real failure metric.
        rows = [r for r in rows if r.get("phase") != "enrichment_skipped"]

    by_phase: dict[str, int] = {}
    for r in rows:
        p = r.get("phase", "capture")
        by_phase[p] = by_phase.get(p, 0) + 1

    return {
        "total": len(rows),
        "by_phase": by_phase,
        "recent": rows[-limit:],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=True)
