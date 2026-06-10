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

from backend.agent.recaller import Recaller
from backend.agent.retrieval import RetrievalService
from backend.capture.processor import CaptureInput, process
from backend.config import settings
from backend.knowledge.enrichment_worker import (
    enqueue_enrichment,
    hydrate_processed_from_capture,
    iter_unenriched_captures,
)
from backend.knowledge.llm_client import LLMClient, PermanentLLMError
from backend.storage import (
    DEFAULT_USER_ID,
    CaptureRepository,
    EntityRepository,
    EnrichmentRepository,
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
    title="DigitalTwin",
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


# Phase 3.5 — captures + enrichments + hydrations now live in SQL +
# Chroma only. The three knowledge JSONLs (captures, enrichments,
# hydrations) were retired with the cutover; see docs/phase3.5-cutover.md.
# Only the failures log survives — it's an operational record, not part
# of the knowledge graph, and intentionally stays a flat JSONL.
FAILURES_LOG = Path(settings.capture_failures_path)


# ---- Phase 2: shared LLM client (constructed at startup) ------------
#
# Single AsyncAnthropic client is reused across requests so we don't
# pay TCP/TLS handshake on every capture. None when no API key is set
# (local dev without enrichment) — `/capture` will skip enrichment in
# that case rather than crash.
_llm_client: LLMClient | None = None


def _get_llm_client() -> LLMClient | None:
    return _llm_client


# ---- Phase 4 M.4: shared Recaller (constructed at startup) ----------
#
# The Recaller wraps RetrievalService + LLMClient + an in-memory
# ConversationStore. We construct one per process and reuse it across
# requests so the conversation store is shared (so a follow-up turn
# can find its prior). None when the LLM client is missing — `/recall`
# returns 503 in that case rather than crash with an unbound singleton.
_recaller: Recaller | None = None


def _get_recaller() -> Recaller | None:
    return _recaller


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


class RecallPayload(BaseModel):
    """Body for `POST /recall` — vague-recall search.

    - `query` is what the user typed (free-form natural language)
    - `conversation_id` is optional; when present, this turn is treated
      as a refinement on the prior turn's candidate pool instead of a
      fresh search (per docs/phase4-vague-recall-design.md U.3)
    """

    query: str
    conversation_id: str | None = None


# --- Routes ---

@app.get("/")
async def root():
    return {
        "name": "DigitalTwin",
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
    unenriched rows for the default user, which is small in normal
    operation.

    Phase 3.5: replaced the captures.jsonl scan with a SQL query
    (`captures LEFT JOIN enrichments`). The set of intentionally-skipped
    capture_ids (empty / oversized content) still lives in the
    operational failures log — same JSONL file as before, but now the
    only JSONL anything reads.
    """
    client = _get_llm_client()
    if client is None:
        return

    queued = 0
    async for capture in iter_unenriched_captures(
        user_id=DEFAULT_USER_ID,
        failures_path=FAILURES_LOG,
    ):
        processed = hydrate_processed_from_capture(capture)
        if processed is None:
            logger.debug(
                "Skipping recovery for %s — capture row has no content",
                capture.id,
            )
            continue
        asyncio.create_task(enqueue_enrichment(capture.id, processed, client))
        queued += 1
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
    # Phase 3.5 — SQL is the sole authoritative store. Init the schema
    # (idempotent CREATE TABLE IF NOT EXISTS + a narrow ADD COLUMN sweep
    # for the v1.5 content columns; see backend/storage/db.py) and seed
    # the default user. If init fails we still try the user seed — the
    # two try-blocks stay independent so a user-seed bug doesn't look
    # like a schema-init bug and vice versa.
    try:
        await init_storage_db()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "SQL schema init failed (captures will not persist this run): %s",
            e,
        )
    try:
        await _ensure_default_user()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Default-user seed failed "
            "(sync_capture writes will FK-violate on user_id=%s): %s",
            DEFAULT_USER_ID, e,
        )

    global _llm_client, _recaller
    if not settings.anthropic_api_key:
        logger.warning(
            "ANTHROPIC_API_KEY is empty — Phase 2 enrichment AND Phase 4 "
            "recall are DISABLED. Captures will still be persisted; set "
            "the key in .env to enable."
        )
        return
    try:
        _llm_client = LLMClient()
    except PermanentLLMError as e:
        logger.error("LLM client init failed: %s", e)
        return

    # Phase 4 M.4 — build the Recaller singleton on top of the shared
    # LLM client. RetrievalService is stateless; its embedder and vector
    # store are lazy singletons inside the storage layer, so we don't
    # need any wiring beyond the default constructor here. Recaller
    # owns the in-memory ConversationStore (lost on process restart by
    # design, U.3).
    try:
        _recaller = Recaller(
            retrieval=RetrievalService(),
            llm_client=_llm_client,
        )
        logger.info("Recaller initialized — /recall is live")
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Recaller init failed (/recall will return 503): %s", e,
        )

    # Crash recovery — re-queue work that was in flight when we died.
    await _recover_unenriched()


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _llm_client, _recaller
    # Drop the Recaller first — it holds the LLM client by reference.
    _recaller = None
    if _llm_client is not None:
        try:
            await _llm_client.aclose()
        except Exception:  # noqa: BLE001
            logger.exception("Error closing LLM client")
        _llm_client = None
    # Drain the SQL connection pool cleanly.
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

    Pipeline (Phase 1 + 2, post-3.5 cutover):
      1. Extract + vision (synchronous, in the request).
      2. Persist the processed row to SQL via `sync_capture` — captures
         table now stores clean_text/transcript/image_text alongside
         the metadata, so an in-flight enrichment can be replayed
         from SQL after a crash.
      3. Schedule enrichment in the background — never blocks the
         response. Decision H — async enrichment via BackgroundTasks.

    Phase 3.5 removed the captures.jsonl writer; the SQL row IS the
    authoritative capture. A SQL failure means the capture is logged
    to the failures log and `/capture` returns persisted=False so the
    extension/bot can decide what to do.
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

    # Phase 3.5 — sole persistence step. sync_capture writes the row
    # (metadata + content columns) to SQL; idempotent on duplicate
    # capture_id. Returns False on duplicate OR error — we treat both
    # the same way for the enrichment decision, but a False with no
    # prior row is the only failure mode that matters and gets surfaced
    # via the failures log so the operator notices.
    persisted = await sync_capture(
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
        clean_text=processed.clean_text or None,
        transcript=processed.transcript,
        image_text=processed.image_text or None,
        image_descriptions_json=(
            json.dumps(processed.image_descriptions, ensure_ascii=False)
            if processed.image_descriptions else None
        ),
        text_source=processed.text_source,
    )
    if not persisted:
        # Surface SQL persistence failures in the operational log so the
        # operator (and the bot's `/failures` command) can see them.
        # Idempotent-duplicate also returns False — that's expected when
        # a client retries a known capture_id; we log it but it's not a
        # real failure. Differentiated by checking SQL after the fact
        # would be a round-trip; cheaper to log and let `/failures`
        # de-noise via the reason field.
        _log_failure(
            source=source,
            reason="sql_persist_failed_or_duplicate",
            payload=payload.model_dump(mode="json"),
        )

    # Schedule enrichment. Skipped if:
    #   - persistence failed (no row to enrich against)
    #   - no LLM client (no API key in env)
    enrichment_scheduled = False
    client = _get_llm_client()
    if persisted and client is not None:
        background_tasks.add_task(enqueue_enrichment, capture_id, processed, client)
        enrichment_scheduled = True

    return {
        "status": "captured" if persisted else "rejected",
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
        # Phase 3.5 — SQL is the only persistence path. `persisted=False`
        # means the capture did NOT land anywhere durable; the caller
        # should treat it as a failure and retry.
        "persisted": persisted,
    }


@app.post("/recall")
async def recall(payload: RecallPayload):
    """Phase 4 M.4 — vague-recall search (use case B).

    Pipeline (delegated to Recaller — see backend/agent/recaller.py):
      1. Hybrid retrieval (Chroma vector + SQLite BM25) via the shared
         RetrievalService.
      2. Reciprocal-rank fusion + per-capture diversification.
      3. Sonnet re-rank pass that picks the most likely answer, attaches
         confidence + reasoning per candidate, composes the brief
         conversational answer.
      4. Confidence threshold (V.7) — top-1 below 0.6 → no_match
         response with the closest-miss courtesy framing.
      5. Conversation state — when `conversation_id` is present, the
         query is treated as a refinement on the prior candidate pool
         instead of a fresh search (U.3). A first call without
         `conversation_id` mints a uuid and returns it; subsequent
         calls with that uuid get layered as refinements.

    Returns the RecallResponse shape locked by docs/phase4-vague-recall-design.md
    S.3 (answer, confidence, results, conversation_id, no_match).

    Errors:
      - 503 if the Recaller wasn't initialized (no ANTHROPIC_API_KEY).
        The capture path stays available so the user can keep ingesting
        content while the agent layer is down.
      - 422 on missing `query` (FastAPI's default Pydantic validation).
      - 5xx if something raises out of the Recaller — which it
        shouldn't, since recall() catches and degrades. If it does
        happen, that's a bug worth seeing.

    Tenancy: single-user for v1 — every call lands at DEFAULT_USER_ID
    (Sabya per B.5.4). Multi-user auth lands with use case A (Phase 4.1+).
    """
    recaller = _get_recaller()
    if recaller is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Recall agent not initialized — set ANTHROPIC_API_KEY in "
                ".env and restart the backend."
            ),
        )
    response = await recaller.recall(
        query=payload.query,
        user_id=DEFAULT_USER_ID,
        conversation_id=payload.conversation_id,
    )
    return response.to_dict()


@app.get("/stats")
async def get_stats():
    """Knowledge base statistics, read from SQL.

    Phase 3.5: this used to scan `captures.jsonl` + `enrichments.jsonl`
    + `capture_failures.jsonl`. After the cutover, captures and
    enrichments live in SQL; only the skipped-set still comes from the
    failures log (it's an ops record, not part of the knowledge graph).
    """
    user_id = DEFAULT_USER_ID

    async with session_scope() as session:
        cap_repo = CaptureRepository(session)
        enr_repo = EnrichmentRepository(session)
        ent_repo = EntityRepository(session)

        total_captures = await cap_repo.count_by_user(user_id=user_id)
        platforms = await cap_repo.platform_counts(user_id=user_id)
        last_capture = await cap_repo.latest_captured_at(user_id=user_id)
        total_enriched = await enr_repo.count_enriched_captures_by_user(user_id=user_id)
        total_entities = await ent_repo.count_capture_mentions_by_user(user_id=user_id)

    # Skipped-set still comes from the ops failures log per the
    # Phase 3.5 decision to keep capture_failures.jsonl as a flat log
    # (no SQL counterpart). We use it to discount the pending metric
    # so it reflects real backlog, not "nothing to enrich" rows.
    skipped_count = _count_enrichment_skipped(FAILURES_LOG)

    pending_enrichment = max(0, total_captures - total_enriched - skipped_count)

    return {
        "total_captures": total_captures,
        "total_entities": total_entities,
        "platforms": platforms,
        "last_capture": last_capture,
        "enrichments": {
            "total": total_enriched,
            "pending": pending_enrichment,
            "skipped": skipped_count,
        },
    }


def _count_enrichment_skipped(failures_log: Path) -> int:
    """Count rows tagged `phase: enrichment_skipped` in the failures log.

    Lives here (not on the storage layer) because the failures log is
    intentionally NOT a SQL table — it's an operational record (see
    docs/phase3.5-cutover.md, decision 2). Small enough to scan on
    every `/stats` call; if it ever grows uncomfortably large, the
    failures log gets a rotation policy before it gets a SQL table.
    """
    if not failures_log.exists():
        return 0
    count = 0
    seen: set[str] = set()
    with failures_log.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("phase") != "enrichment_skipped":
                continue
            cid = row.get("capture_id")
            if isinstance(cid, str) and cid and cid not in seen:
                seen.add(cid)
                count += 1
    return count


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
