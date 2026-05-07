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
from backend.storage.chunking import (
    ALL_SOURCE_KINDS,
    DEFAULT_MAX_CHAPTER_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_WINDOW_TOKENS,
    SHORT_TRANSCRIPT_SECONDS,
    SOURCE_KIND_ARTICLE_PARAGRAPH,
    SOURCE_KIND_IMAGE_CAPTION,
    SOURCE_KIND_SUMMARY,
    SOURCE_KIND_TRANSCRIPT_SEGMENT,
    chunk,
)
from backend.storage.embedder import (
    DEFAULT_MODEL as DEFAULT_EMBEDDING_MODEL,
    EMBEDDING_BYTES,
    EMBEDDING_DIM,
    Embedder,
    get_embedder,
)
from backend.storage.vector_store import (
    ALL_COLLECTIONS,
    COLLECTION_CHUNKS,
    COLLECTION_ENTITIES,
    COLLECTION_TOPICS,
    ChromaVectorStore,
    VectorHit,
    VectorStore,
    get_vector_store,
    reset_vector_store,
)
from backend.storage.models import (
    Capture,
    CaptureWithEnrichment,
    Chunk,
    ChunkAttachment,
    ChunkInsert,
    Entity,
    Enrichment,
    Hydration,
    Topic,
    User,
)
from backend.storage.repositories import (
    ENTITY_TYPES,
    BaseRepository,
    CaptureRepository,
    ChunkRepository,
    DuplicateKeyError,
    EntityRepository,
    EnrichmentRepository,
    HydrationRepository,
    RepositoryError,
    TopicRepository,
    UserRepository,
    normalize_slug,
)
from backend.storage.sync import (
    DEFAULT_USER_ID,
    sync_capture,
    sync_enrichment,
    sync_hydration,
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
    # domain models
    "User",
    "Capture",
    "Hydration",
    "Enrichment",
    "Chunk",
    "ChunkInsert",
    "Topic",
    "Entity",
    "CaptureWithEnrichment",
    "ChunkAttachment",
    # repositories
    "BaseRepository",
    "RepositoryError",
    "DuplicateKeyError",
    "UserRepository",
    "CaptureRepository",
    "HydrationRepository",
    "EnrichmentRepository",
    "ChunkRepository",
    "TopicRepository",
    "EntityRepository",
    "normalize_slug",
    "ENTITY_TYPES",
    # embedder
    "Embedder",
    "DEFAULT_EMBEDDING_MODEL",
    "EMBEDDING_DIM",
    "EMBEDDING_BYTES",
    "get_embedder",
    # vector store
    "VectorStore",
    "ChromaVectorStore",
    "VectorHit",
    "get_vector_store",
    "reset_vector_store",
    "COLLECTION_CHUNKS",
    "COLLECTION_TOPICS",
    "COLLECTION_ENTITIES",
    "ALL_COLLECTIONS",
    # chunking
    "chunk",
    "SOURCE_KIND_ARTICLE_PARAGRAPH",
    "SOURCE_KIND_TRANSCRIPT_SEGMENT",
    "SOURCE_KIND_IMAGE_CAPTION",
    "SOURCE_KIND_SUMMARY",
    "ALL_SOURCE_KINDS",
    "DEFAULT_WINDOW_TOKENS",
    "DEFAULT_OVERLAP_TOKENS",
    "DEFAULT_MAX_CHAPTER_TOKENS",
    "SHORT_TRANSCRIPT_SECONDS",
    # sync (Step 4 dual-write seam)
    "sync_capture",
    "sync_hydration",
    "sync_enrichment",
    "DEFAULT_USER_ID",
]
