"""Phase 3 storage layer — SQL + Vector.

Public API. Other modules import from here:

    from backend.storage import session_scope, init_db, captures, chunks
    from sqlalchemy import select

    async with session_scope() as session:
        rows = (await session.execute(
            select(captures).where(captures.c.user_id == 1)
        )).fetchall()

Lifecycle (called from backend/main.py):
    init_db()  — creates schema on startup (idempotent)
    aclose()   — disposes engine on shutdown

Tables (defined in schema.py, re-exported here for ergonomics):
    users, captures, hydrations, enrichments,
    chunks, topics, entities,
    chunk_topics, chunk_entities

Repositories (Step 1b — built next, exposed here once they exist):
    CaptureRepository, ChunkRepository, EnrichmentRepository,
    HydrationRepository, TopicRepository, EntityRepository

Vector store (Step 2 — built after repositories):
    VectorStore (interface), ChromaVectorStore (impl)

See docs/phase3-design.md for the full design.
"""

from backend.storage.db import (
    aclose,
    get_engine,
    get_session_factory,
    init_db,
    session_scope,
)
from backend.storage.schema import (
    captures,
    chunk_entities,
    chunk_topics,
    chunks,
    entities,
    enrichments,
    hydrations,
    metadata,
    topics,
    users,
)


__all__ = [
    # lifecycle
    "init_db",
    "aclose",
    "session_scope",
    "get_engine",
    "get_session_factory",
    # tables
    "metadata",
    "users",
    "captures",
    "hydrations",
    "enrichments",
    "chunks",
    "topics",
    "entities",
    "chunk_topics",
    "chunk_entities",
]
