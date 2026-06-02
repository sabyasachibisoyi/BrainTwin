"""Persistence seam — SQL + Chroma writes for captures, hydrations,
enrichments.

History: this module started as the **dual-write seam** during the
Phase 3 → Phase 3.5 cutover window. Per docs/phase3-design.md B.1 every
`/capture` POST wrote to BOTH the legacy JSONL files AND SQL/Chroma so
no capture could be lost if the new storage layer broke. The
`storage_dual_write` flag gated each function so the operator could
disable SQL writes during debugging.

After Phase 3.5 (docs/phase3.5-cutover.md) the JSONL writers were
removed and SQL is the sole authoritative store. The `storage_dual_write`
gate went with them — these functions are no longer best-effort
side-channels, they are the primary persistence path. Errors are
still logged and swallowed so a SQL hiccup doesn't crash the request,
but a False return now means the capture is not persisted anywhere.

Tenant simplification (Step 4):
  All captures land at user_id=1 per B.5.4. Bot and extension don't
  pass user_id today; multi-user auth lands when use case A goes live.

What gets chunked + embedded (per A.5):
  - clean_text → article_paragraph chunks (paragraph-split)
  - transcript → transcript_segment chunks (chapter-aware if provided,
    token-window otherwise)
  - image_text → image_caption chunks (single, whole-thing)
  - enrichment summary → summary chunk (single)

For B.7's controlled-vocabulary flow we use slug-level dedup via
TopicRepository.find_or_create / EntityRepository.find_or_create. The
full embedding-similarity reuse (LLM sees top-K existing topics and
prefers them over coining new ones) is wired in Phase 4 alongside
agent prompt changes — this module just makes sure the topics +
entities tables get populated so Phase 4 has data to work with.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Optional

from backend.storage.chunking import (
    SOURCE_KIND_ARTICLE_PARAGRAPH,
    SOURCE_KIND_IMAGE_CAPTION,
    SOURCE_KIND_SUMMARY,
    SOURCE_KIND_TRANSCRIPT_SEGMENT,
    chunk,
)
from backend.storage.db import session_scope
from backend.storage.embedder import Embedder, get_embedder
from backend.storage.models import Capture, ChunkAttachment, ChunkInsert
from backend.storage.repositories import (
    CaptureRepository,
    ChunkRepository,
    EntityRepository,
    EnrichmentRepository,
    HydrationRepository,
    TopicRepository,
)
from backend.storage.vector_store import (
    COLLECTION_CHUNKS,
    COLLECTION_ENTITIES,
    COLLECTION_TOPICS,
    VectorStore,
    get_vector_store,
)

if TYPE_CHECKING:
    from backend.capture.processor import ProcessedContent


logger = logging.getLogger(__name__)


# ---- Multi-tenant default --------------------------------------------

# Step 4 hard-codes user_id=1 (Sabya, per B.5.4). Multi-user auth
# arrives with use case A. Until then, every capture lives in the
# single-user namespace. The migration script (B.5) will use the same
# constant when seeding historical data.
DEFAULT_USER_ID = 1


# ---- Public API ------------------------------------------------------

async def sync_capture(
    *,
    capture_id: str,
    url: Optional[str],
    title: Optional[str],
    platform: Optional[str],
    content_type: Optional[str],
    captured_at: str,
    dwell_seconds: int = 0,
    raw_metadata_json: Optional[str] = None,
    clean_text: Optional[str] = None,
    transcript: Optional[str] = None,
    image_text: Optional[str] = None,
    image_descriptions_json: Optional[str] = None,
    text_source: Optional[str] = None,
    user_id: int = DEFAULT_USER_ID,
) -> bool:
    """Persist a capture row to SQL.

    Idempotent: if `capture_id` already exists in the captures table,
    this is a silent no-op. Best-effort: never raises — any SQL error
    is caught and logged so the caller (the /capture handler) can
    decide whether to surface it.

    Returns True on a successful insert, False on duplicate or error.

    Phase 3.5: this is the ONLY persistence step for a capture (the
    JSONL writer has been retired). The processed-content fields are
    now stored on the captures row itself so the enrichment worker
    can rebuild ProcessedContent from SQL after a crash.
    """
    try:
        async with session_scope() as session:
            cap_repo = CaptureRepository(session)
            # Tenant-scoped check. CaptureRepository.exists() is a
            # cross-tenant existence probe (per its docstring, B.5
            # migration use only) and would leak existence between
            # users if used here. Use get() instead — None either
            # means truly new OR owned by another tenant; the global
            # TEXT PK on captures.id will surface a cross-tenant
            # collision as an IntegrityError, caught below.
            if await cap_repo.get(capture_id, user_id=user_id) is not None:
                logger.debug("sync_capture: %s already in SQL, skipping", capture_id)
                return False
            await cap_repo.create(Capture(
                id=capture_id,
                user_id=user_id,
                url=url,
                title=title,
                platform=platform,
                content_type=content_type,
                captured_at=captured_at,
                dwell_seconds=dwell_seconds,
                raw_metadata_json=raw_metadata_json,
                clean_text=clean_text,
                transcript=transcript,
                image_text=image_text,
                image_descriptions_json=image_descriptions_json,
                text_source=text_source,
            ))
        logger.debug("sync_capture: inserted %s", capture_id)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("sync_capture failed for %s: %s", capture_id, e)
        return False


async def sync_hydration(
    *,
    capture_id: str,
    tier: str,
    source_payload_json: Optional[str],
    hydrated_at: str,
) -> bool:
    """Persist a hydration row (formerly the Phase 2.5 sidecar JSONL).

    Best-effort like `sync_capture` — never raises, returns False on
    error. Caller is responsible for verifying the parent capture
    exists; the FK constraint will catch ordering bugs (and we log
    the failure instead of raising)."""
    try:
        async with session_scope() as session:
            hyd_repo = HydrationRepository(session)
            await hyd_repo.create(
                capture_id=capture_id,
                tier=tier,
                source_payload_json=source_payload_json,
                hydrated_at=hydrated_at,
            )
        logger.debug("sync_hydration: inserted %s tier=%s", capture_id, tier)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "sync_hydration failed for capture=%s tier=%s: %s",
            capture_id, tier, e,
        )
        return False


async def sync_enrichment(
    *,
    capture_id: str,
    summary: Optional[str],
    key_facts_json: Optional[str],
    topics: Optional[list[str]] = None,
    entities: Optional[list[dict[str, Any]]] = None,
    model: Optional[str] = None,
    enriched_at: str,
    processed: Optional["ProcessedContent"] = None,
    transcript_duration_seconds: Optional[float] = None,
    chapter_texts: Optional[list[str]] = None,
    user_id: int = DEFAULT_USER_ID,
    embedder: Optional[Embedder] = None,
    vector_store: Optional[VectorStore] = None,
) -> bool:
    """Mirror an enrichment + its derived chunks/topics/entities.

    Pipeline (best-effort throughout):
      1. Insert enrichment row in SQL.
      2. Chunk processed content per A.5 by source kind.
      3. Embed each chunk (single batched call to embedder).
      4. Insert chunks into SQL with embedding bytes.
      5. Add chunk vectors to Chroma `chunks` collection with metadata.
      6. For each topic: TopicRepository.find_or_create (slug-normalized,
         with the topic-label embedding so Phase 4's controlled-vocab
         flow has data to work with). Add to Chroma `topics` collection.
      7. Same for entities.
      8. Attach topics + entities to chunks via junction tables.

    `processed` is optional. When None, only steps 1, 6, 7 run — the
    chunk-derived rows are skipped. Useful for testing the enrichment
    metadata path separately from the chunking path.

    `entities` shape: list of dicts with keys {label, entity_type,
    confidence?}. We accept dicts because the existing enrichment
    pipeline emits them this way.

    `topics` shape: list of strings (labels). Slug-normalized internally.
    """
    embedder = embedder if embedder is not None else get_embedder()
    vector_store = vector_store if vector_store is not None else get_vector_store()

    # ---- Step 1: enrichment row ---------------------------------------
    try:
        async with session_scope() as session:
            await EnrichmentRepository(session).create(
                capture_id=capture_id,
                summary=summary,
                key_facts_json=key_facts_json,
                model=model,
                enriched_at=enriched_at,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "sync_enrichment: enrichment-row insert failed for %s: %s",
            capture_id, e,
        )
        # Stop here — without an enrichment row, the chunks/topics
        # below would orphan. Better to bail than half-write.
        return False

    # ---- Steps 2-5: chunks + embeddings + vector store ---------------
    if processed is not None:
        try:
            await _sync_chunks_and_vectors(
                capture_id=capture_id,
                user_id=user_id,
                captured_at=processed.timestamp,
                processed=processed,
                summary=summary,
                transcript_duration_seconds=transcript_duration_seconds,
                chapter_texts=chapter_texts,
                embedder=embedder,
                vector_store=vector_store,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "sync_enrichment: chunk-and-vector path failed for %s: %s",
                capture_id, e,
            )

    # ---- Steps 6-8: topics, entities, junctions ----------------------
    try:
        await _sync_topics_and_entities(
            capture_id=capture_id,
            user_id=user_id,
            topics=topics or [],
            entities=entities or [],
            embedder=embedder,
            vector_store=vector_store,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "sync_enrichment: topic/entity path failed for %s: %s",
            capture_id, e,
        )

    return True


# ---- Internal: chunk + embed + vector-store ---------------------------

async def _sync_chunks_and_vectors(
    *,
    capture_id: str,
    user_id: int,
    captured_at: str,
    processed: "ProcessedContent",
    summary: Optional[str],
    transcript_duration_seconds: Optional[float],
    chapter_texts: Optional[list[str]],
    embedder: Embedder,
    vector_store: VectorStore,
) -> None:
    """Generate chunks per A.5, embed them, persist to SQL + Chroma.

    Each source kind contributes 0..N chunks; we collect all of them,
    embed in one batch (faster than per-chunk), then write atomically
    to SQL followed by Chroma. If SQL succeeds and Chroma fails, the
    SQL chunks remain (with embedding bytes); a future retry / repair
    job can replay the Chroma write.

    Re-enrichment safety: if chunks already exist for this capture
    (Phase 5+ when the agent updates summaries triggers a second
    sync_enrichment call), this path is a no-op. chunks have a
    UNIQUE(capture_id, chunk_index) constraint and chunk_index here
    restarts at 0 — re-running would integrity-error and cascade-roll
    back the whole batch. Preserving the existing chunks is the
    pragmatic v1 behavior; targeted refresh of summary chunks is a
    Phase 5 follow-up when re-enrichment actually goes live."""
    async with session_scope() as session:
        existing = await ChunkRepository(session).list_by_capture(
            capture_id, user_id=user_id,
        )
    if existing:
        logger.debug(
            "_sync_chunks_and_vectors: %s already has %d chunks, skipping",
            capture_id, len(existing),
        )
        return

    # Collect (source_kind, chunk_text) pairs in order.
    pending: list[tuple[str, str]] = []

    if processed.clean_text and processed.clean_text.strip():
        for chunk_text in chunk(
            source_kind=SOURCE_KIND_ARTICLE_PARAGRAPH,
            text=processed.clean_text,
        ):
            pending.append((SOURCE_KIND_ARTICLE_PARAGRAPH, chunk_text))

    if processed.transcript and processed.transcript.strip():
        for chunk_text in chunk(
            source_kind=SOURCE_KIND_TRANSCRIPT_SEGMENT,
            text=processed.transcript,
            chapter_texts=chapter_texts,
            transcript_duration_seconds=transcript_duration_seconds,
        ):
            pending.append((SOURCE_KIND_TRANSCRIPT_SEGMENT, chunk_text))

    if processed.image_text and processed.image_text.strip():
        for chunk_text in chunk(
            source_kind=SOURCE_KIND_IMAGE_CAPTION,
            text=processed.image_text,
        ):
            pending.append((SOURCE_KIND_IMAGE_CAPTION, chunk_text))

    if summary and summary.strip():
        for chunk_text in chunk(
            source_kind=SOURCE_KIND_SUMMARY,
            text=summary,
        ):
            pending.append((SOURCE_KIND_SUMMARY, chunk_text))

    if not pending:
        logger.debug("_sync_chunks_and_vectors: no chunks for %s", capture_id)
        return

    # Embed everything in one batched call.
    texts = [p[1] for p in pending]
    embeddings = embedder.embed_many(texts)

    # Persist to SQL with embedding bytes; collect new ids in order.
    chunk_inserts = [
        ChunkInsert(
            capture_id=capture_id,
            chunk_index=i,
            text=text,
            source_kind=source_kind,
            embedding=Embedder.to_bytes(embeddings[i]),
        )
        for i, (source_kind, text) in enumerate(pending)
    ]
    async with session_scope() as session:
        chunk_ids = await ChunkRepository(session).create_many(chunk_inserts)

    # Mirror into Chroma. metadata carries everything Phase 4 retrieval
    # needs to filter by tenant + source kind + time without a SQL
    # round-trip.
    metadatas = [
        {
            "user_id": user_id,
            "capture_id": capture_id,
            "source_kind": source_kind,
            "captured_at": captured_at,
        }
        for source_kind, _text in pending
    ]
    await vector_store.upsert(
        COLLECTION_CHUNKS,
        ids=[str(chunk_id) for chunk_id in chunk_ids],
        embeddings=embeddings,
        metadatas=metadatas,
        documents=texts,
    )
    logger.debug(
        "_sync_chunks_and_vectors: %s → %d chunks across %s",
        capture_id, len(chunk_ids),
        sorted(set(p[0] for p in pending)),
    )


# ---- Internal: topics + entities + junctions -------------------------

async def _sync_topics_and_entities(
    *,
    capture_id: str,
    user_id: int,
    topics: list[str],
    entities: list[dict[str, Any]],
    embedder: Embedder,
    vector_store: VectorStore,
) -> None:
    """Run topics + entities through find_or_create, ensuring each one
    has a Chroma vector for the future controlled-vocabulary lookup.
    Then attach to every chunk on the parent capture (the LLM doesn't
    tell us WHICH chunk a topic applies to in v1 — it tags the whole
    enrichment — so we apply each topic uniformly to all chunks of
    the capture).

    Future Phase 4 work: per-chunk topic/entity tagging from a
    chunk-level LLM pass. For now, capture-level → all-chunks is the
    correct, pragmatic shape."""
    if not topics and not entities:
        return

    async with session_scope() as session:
        topic_repo = TopicRepository(session)
        entity_repo = EntityRepository(session)
        chunk_repo = ChunkRepository(session)

        topic_ids: list[int] = []
        topic_id_set: set[int] = set()
        new_topic_payloads: list[tuple[int, str, str]] = []  # (id, slug, label)
        for label in topics:
            if not isinstance(label, str) or not label.strip():
                continue
            embedding_bytes = Embedder.to_bytes(embedder.embed(label))
            topic = await topic_repo.find_or_create(
                label=label.strip(),
                embedding=embedding_bytes,
            )
            if topic.id not in topic_id_set:
                topic_id_set.add(topic.id)
                topic_ids.append(topic.id)
                # Track new ones we likely just inserted; the upsert
                # below is idempotent so no harm if it was already there.
                new_topic_payloads.append((topic.id, topic.slug, topic.label))

        entity_ids_with_meta: list[tuple[int, str, str]] = []  # (id, slug, label)
        entity_id_set: set[int] = set()
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            label = ent.get("label") or ent.get("name") or ""
            ent_type = ent.get("entity_type") or ent.get("type") or "concept"
            if not isinstance(label, str) or not label.strip():
                continue
            embedding_bytes = Embedder.to_bytes(embedder.embed(label))
            try:
                entity = await entity_repo.find_or_create(
                    label=label.strip(),
                    entity_type=ent_type,
                    embedding=embedding_bytes,
                )
            except ValueError:
                # Unknown entity_type — fall back to "concept".
                entity = await entity_repo.find_or_create(
                    label=label.strip(),
                    entity_type="concept",
                    embedding=embedding_bytes,
                )
            if entity.id not in entity_id_set:
                entity_id_set.add(entity.id)
                entity_ids_with_meta.append((entity.id, entity.slug, entity.label))

        # Attach to every chunk of this capture.
        chunks_for_capture = await chunk_repo.list_by_capture(
            capture_id, user_id=user_id,
        )
        for c in chunks_for_capture:
            if topic_ids:
                await chunk_repo.attach_topics(
                    c.id,
                    [(tid, None) for tid in topic_ids],
                )
            if entity_ids_with_meta:
                await chunk_repo.attach_entities(
                    c.id,
                    [
                        ChunkAttachment(entity_id=eid, confidence=None, mention_position=0)
                        for eid, _slug, _label in entity_ids_with_meta
                    ],
                )

    # Mirror topics + entities to Chroma OUTSIDE the SQL transaction —
    # Chroma is a separate store and we don't want a Chroma hiccup to
    # roll back the SQL writes.
    if new_topic_payloads:
        labels = [p[2] for p in new_topic_payloads]
        topic_embeddings = embedder.embed_many(labels)
        await vector_store.upsert(
            COLLECTION_TOPICS,
            ids=[str(tid) for tid, _slug, _label in new_topic_payloads],
            embeddings=topic_embeddings,
            metadatas=[{"slug": slug, "label": label}
                       for _id, slug, label in new_topic_payloads],
            documents=labels,
        )

    if entity_ids_with_meta:
        labels = [p[2] for p in entity_ids_with_meta]
        entity_embeddings = embedder.embed_many(labels)
        await vector_store.upsert(
            COLLECTION_ENTITIES,
            ids=[str(eid) for eid, _slug, _label in entity_ids_with_meta],
            embeddings=entity_embeddings,
            metadatas=[{"slug": slug, "label": label}
                       for _id, slug, label in entity_ids_with_meta],
            documents=labels,
        )


__all__ = [
    "sync_capture",
    "sync_hydration",
    "sync_enrichment",
    "DEFAULT_USER_ID",
]
