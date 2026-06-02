"""Domain dataclasses for the Phase 3 storage layer.

These are the public types repository methods accept and return —
plain frozen dataclasses, no Pydantic, no SQLAlchemy ORM, no DB
state attached to them. They match the existing project style
(`ProcessedContent`, `OGMetadata`, `TranscriptionResult`).

Translation between dataclass and SQL row happens inside each
repository (see `_row_to_X` helpers). Callers don't see SQLAlchemy
types — they get these dataclasses back.

The `embedding` field on chunks/topics/entities is `bytes | None` —
a serialized float32 array. Numpy <-> bytes conversion lives in
`backend/storage/embedding_codec.py` (Step 1c) so repositories don't
import numpy. Empty embedding (`None`) is allowed for the rare case
where embedding generation failed; the row is still useful for
keyword retrieval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


# ---- Users ------------------------------------------------------------

@dataclass(frozen=True)
class User:
    """Multi-tenant root. id=1 is reserved for Sabya per B.5.4."""
    id: int
    email: str
    display_name: Optional[str]
    created_at: str  # ISO 8601


# ---- Captures ---------------------------------------------------------

@dataclass(frozen=True)
class Capture:
    """One row per capture from the Chrome extension or Telegram bot.
    `id` is the UUID4 the client minted; we use it as both PK and
    join key to enrichments / hydrations / chunks (per Phase 2's
    `capture_id` design).

    Phase 3.5 adds the processed-content fields (clean_text, transcript,
    image_text, image_descriptions_json, text_source). They were only
    in captures.jsonl before; now they live on the row so the enrichment
    worker can rebuild ProcessedContent from SQL after a crash. All
    nullable for backwards compatibility with rows that pre-date the
    cutover or where the extractor returned nothing."""
    id: str
    user_id: int
    url: Optional[str]
    title: Optional[str]
    platform: Optional[str]
    content_type: Optional[str]
    captured_at: str
    dwell_seconds: int
    raw_metadata_json: Optional[str]
    # ---- Phase 3.5 content fields ------------------------------------
    clean_text: Optional[str] = None
    transcript: Optional[str] = None
    image_text: Optional[str] = None
    image_descriptions_json: Optional[str] = None
    text_source: Optional[str] = None


# ---- Hydrations -------------------------------------------------------

@dataclass(frozen=True)
class Hydration:
    """Phase 2.5 sidecar — recorded when an empty capture got filled in
    via OG fetch or video transcription. Tier-tagged so we can audit
    which path produced the content."""
    id: int
    capture_id: str
    tier: str  # "og_metadata" | "video_transcript"
    source_payload_json: Optional[str]
    hydrated_at: str


# ---- Enrichments ------------------------------------------------------

@dataclass(frozen=True)
class Enrichment:
    """Phase 2 enrichment record — one per successfully enriched capture."""
    id: int
    capture_id: str
    summary: Optional[str]
    key_facts_json: Optional[str]
    model: Optional[str]
    enriched_at: str


# ---- Chunks (the retrieval unit, β) ----------------------------------

@dataclass(frozen=True)
class Chunk:
    """Slice of capture content that gets its own embedding. The
    `embedding` field carries serialized bytes; callers needing a
    numpy array go through `backend.storage.embedding_codec`."""
    id: int
    capture_id: str
    chunk_index: int
    text: str
    source_kind: str  # "article_paragraph" | "transcript_segment" | "image_caption" | "summary"
    embedding: Optional[bytes]


@dataclass(frozen=True)
class ChunkInsert:
    """Fields the caller supplies when creating a chunk. `id` is
    DB-assigned, so it isn't here. Separate type from `Chunk` to make
    the create vs. read distinction explicit at type-check time."""
    capture_id: str
    chunk_index: int
    text: str
    source_kind: str
    embedding: Optional[bytes] = None


# ---- Topics + Entities (shared vocabulary, B.7) ----------------------

@dataclass(frozen=True)
class Topic:
    """Shared global vocabulary — no user_id. The controlled-vocabulary
    flow (B.7) reuses topics across users; tenant isolation flows
    through chunk_topics (which connects to chunks → captures →
    user_id)."""
    id: int
    slug: str
    label: str
    description: Optional[str]
    embedding: Optional[bytes]
    coined_at: str


@dataclass(frozen=True)
class Entity:
    id: int
    slug: str
    label: str
    entity_type: str  # "person" | "place" | "company" | "concept"
    embedding: Optional[bytes]
    coined_at: str


# ---- Composite read shapes -------------------------------------------

@dataclass(frozen=True)
class CaptureWithEnrichment:
    """Convenience shape for "list captures with their enrichment if
    any" — the agent layer in Phase 4 will consume this to build
    quizzes."""
    capture: Capture
    enrichment: Optional[Enrichment]


@dataclass(frozen=True)
class ChunkAttachment:
    """Used by ChunkRepository.attach_entities() to record one mention
    of an entity inside a chunk, with confidence and offset.

    Multiple attachments for the same (chunk, entity) are allowed —
    distinguished by mention_position — to capture multiple mentions
    of the same entity in the same chunk."""
    entity_id: int
    confidence: Optional[float]
    mention_position: int = 0


__all__ = [
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
]
