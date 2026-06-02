"""SQLAlchemy Core table definitions for Phase 3 storage.

Per docs/phase3-design.md A.4. The schema is the source of truth — code
reads from these `Table` objects and uses them in `select()`, `insert()`,
`update()` expressions, not from raw SQL strings.

This is **Core**, not the Declarative ORM. We get typed query building
and dialect handling without the heavyweight ORM machinery (lazy
loading, identity maps, relationship traversal). Repositories operate
on dict-like rows, which Pydantic models can adopt for response shapes.

Compatibility:
  - Uses only SQL features that work in BOTH SQLite and PostgreSQL.
  - No SQLite-specific quirks (type-affinity tricks, WITHOUT ROWID).
  - No Postgres-specific features in v1 (no JSONB indexes, no LATERAL).
  - Migration to Postgres becomes a connection-string change in
    `database_url` plus enabling pgvector if/when we go that route.

Tenant isolation (per A.2): every domain row carries `user_id`, OR
descends from a row that does (e.g. `chunks` carries `capture_id`,
which carries `user_id`). Topics and entities are deliberately
**shared global vocabulary** (no `user_id`) per B.7 — "Kanban" coined
by one student is available for reuse by every other student.

Embedding columns on chunks / topics / entities (per B.7):
  - SQLite stores them as BLOB (we serialize the float32 numpy array
    ourselves; helper utilities live in `backend/storage/embedding_codec.py`
    when we need them in Step 1b).
  - When we eventually adopt pgvector (A.7 trigger), the column type
    becomes `VECTOR(384)` and the codec goes away.
"""

from __future__ import annotations

from sqlalchemy import (
    BLOB,
    INTEGER,
    REAL,
    TEXT,
    Column,
    ForeignKey,
    Index,
    MetaData,
    Table,
    UniqueConstraint,
)


# All tables share one MetaData so `create_all()` can build the whole
# schema in one shot. Foreign-key resolution and DDL ordering happen
# automatically based on dependency analysis.
metadata = MetaData()


# ---------------------------------------------------------------------
# Users — multi-tenant from day one (A.2)
# ---------------------------------------------------------------------
# id=1 is reserved for the original single-user (Sabya, B.5.4). Other
# students get id=2, 3, ... when use case A goes live.
users = Table(
    "users",
    metadata,
    Column("id", INTEGER, primary_key=True, autoincrement=True),
    Column("email", TEXT, unique=True, nullable=False),
    Column("display_name", TEXT),
    Column("created_at", TEXT, nullable=False),  # ISO 8601
)


# ---------------------------------------------------------------------
# Captures — formerly mirrored from data/captures.jsonl, now sole store
# ---------------------------------------------------------------------
# `id` stays a TEXT UUID (matches today's `capture_id`) so JSONL rows
# migrate cleanly with no re-keying in B.5.
#
# Phase 3.5 cutover: the processed content fields (clean_text, transcript,
# image_text, image_descriptions_json, text_source) used to live only in
# captures.jsonl. They now live on the captures row itself so the
# enrichment worker can rebuild ProcessedContent from SQL for crash
# recovery and manual retry. See docs/phase3.5-cutover.md.
captures = Table(
    "captures",
    metadata,
    Column("id", TEXT, primary_key=True),
    Column("user_id", INTEGER, ForeignKey("users.id"), nullable=False),
    Column("url", TEXT),
    Column("title", TEXT),
    Column("platform", TEXT),
    Column("content_type", TEXT),
    Column("captured_at", TEXT, nullable=False),  # ISO 8601
    Column("dwell_seconds", INTEGER, nullable=False, server_default="0"),
    # Full original payload from the bot/extension. Audit trail — useful
    # when we want to debug why a capture looks the way it does without
    # poking through git history of the source code.
    Column("raw_metadata_json", TEXT),
    # ---- Phase 3.5 content columns ------------------------------------
    # Processed payload, previously persisted only in captures.jsonl.
    # All nullable so historical rows (pre-cutover) and any capture
    # where the extractor returned nothing stay valid.
    Column("clean_text", TEXT),
    Column("transcript", TEXT),
    Column("image_text", TEXT),
    Column("image_descriptions_json", TEXT),  # JSON array of ImageDescription dicts
    Column("text_source", TEXT),              # "extension" | "youtube_transcript" | "fallback"
    Index("idx_captures_user_id", "user_id"),
    Index("idx_captures_captured_at", "captured_at"),
    Index("idx_captures_platform", "platform"),
)


# ---------------------------------------------------------------------
# Hydrations — lifted from data/hydrations.jsonl (Phase 2.5 Fix 2)
# ---------------------------------------------------------------------
# One row per hydration that filled in an empty capture (OG fetch or
# video transcription). Keeps the original captures.jsonl row immutable
# as audit trail; consumers join via capture_id.
hydrations = Table(
    "hydrations",
    metadata,
    Column("id", INTEGER, primary_key=True, autoincrement=True),
    Column("capture_id", TEXT, ForeignKey("captures.id"), nullable=False),
    Column("tier", TEXT, nullable=False),
    # Full original sidecar row from Phase 2.5 — `og`, `transcript`,
    # `tiers_used`, `transcript_skipped`, etc. Stored as JSON text so
    # we can evolve the inner shape without ALTER TABLE.
    Column("source_payload_json", TEXT),
    Column("hydrated_at", TEXT, nullable=False),
    Index("idx_hydrations_capture_id", "capture_id"),
)


# ---------------------------------------------------------------------
# Enrichments — lifted from data/enrichments.jsonl (Phase 2)
# ---------------------------------------------------------------------
# One row per successfully enriched capture (per Phase 2 Decision I,
# `related_captures` was deferred — not in the Phase 3 schema either,
# revisit in Phase 5+).
enrichments = Table(
    "enrichments",
    metadata,
    Column("id", INTEGER, primary_key=True, autoincrement=True),
    Column("capture_id", TEXT, ForeignKey("captures.id"), nullable=False),
    Column("summary", TEXT),
    Column("key_facts_json", TEXT),  # JSON array of facts
    Column("model", TEXT),           # which Haiku/Sonnet model produced this
    Column("enriched_at", TEXT, nullable=False),
    Index("idx_enrichments_capture_id", "capture_id"),
)


# ---------------------------------------------------------------------
# Chunks — the retrieval unit (β, A.3)
# ---------------------------------------------------------------------
# Each chunk has a 1:1 mirror in the `chunks` ChromaDB collection,
# joined by chunk.id. Source kinds:
#   "article_paragraph"  — body text from Chrome extension, paragraph-split
#   "transcript_segment" — body text from yt-dlp + whisper, chapter or token-window split
#   "image_caption"      — short OG description / image alt text
#   "summary"            — the enrichment summary itself, embedded for "find similar captures"
chunks = Table(
    "chunks",
    metadata,
    Column("id", INTEGER, primary_key=True, autoincrement=True),
    Column("capture_id", TEXT, ForeignKey("captures.id"), nullable=False),
    Column("chunk_index", INTEGER, nullable=False),  # 0-based ordering within capture
    Column("text", TEXT, nullable=False),
    Column("source_kind", TEXT, nullable=False),
    # Float32 array of length 384 (matches all-MiniLM-L6-v2). Serialized
    # with numpy.tobytes() in SQLite; becomes VECTOR(384) when we move
    # to pgvector. Kept as nullable for the rare case where embedding
    # generation fails — the chunk text is still useful for keyword
    # retrieval even without a vector.
    Column("embedding", BLOB),
    UniqueConstraint("capture_id", "chunk_index", name="uq_chunks_capture_chunk"),
    Index("idx_chunks_capture_id", "capture_id"),
    Index("idx_chunks_source_kind", "source_kind"),
)


# ---------------------------------------------------------------------
# Topics — shared global vocabulary (γ flavor, B.7 controlled vocab)
# ---------------------------------------------------------------------
# NO user_id column. Vocabulary is shared across users — "Kanban" coined
# by Alice is the same topic for Bob. Tenant isolation flows through
# the chunk_topics junction (chunks carry user_id via captures).
#
# `embedding` is mirrored into the `topics` ChromaDB collection. When
# enrichment processes a new capture, we query the topics collection
# for the K most similar existing topics and pass them to the LLM as
# candidate matches; the LLM may only coin new topics for content that
# scores below `settings.vocabulary_match_threshold`.
topics = Table(
    "topics",
    metadata,
    Column("id", INTEGER, primary_key=True, autoincrement=True),
    Column("slug", TEXT, unique=True, nullable=False),  # "kanban", "machine-learning"
    Column("label", TEXT, nullable=False),              # "Kanban", "Machine Learning"
    Column("description", TEXT),                        # one-line gloss, embed alongside label
    Column("embedding", BLOB),                          # of (label + " " + description)
    Column("coined_at", TEXT, nullable=False),
)


# ---------------------------------------------------------------------
# Entities — shared global vocabulary (B.7)
# ---------------------------------------------------------------------
# Same shared-vocabulary semantics as topics. `entity_type` distinguishes
# people from places from companies from concepts so the LLM can ground
# its references at retrieval time.
entities = Table(
    "entities",
    metadata,
    Column("id", INTEGER, primary_key=True, autoincrement=True),
    Column("slug", TEXT, unique=True, nullable=False),  # "anthropic", "deepika-padukone"
    Column("label", TEXT, nullable=False),
    Column("entity_type", TEXT, nullable=False),        # person|place|company|concept
    Column("embedding", BLOB),
    Column("coined_at", TEXT, nullable=False),
)


# ---------------------------------------------------------------------
# chunk_topics — junction (chunk-level tagging)
# ---------------------------------------------------------------------
# Tenant-isolated by virtue of joining to chunks (which join to captures
# which carry user_id). One chunk can carry multiple topics; one topic
# can be carried by many chunks across users.
chunk_topics = Table(
    "chunk_topics",
    metadata,
    Column("chunk_id", INTEGER, ForeignKey("chunks.id"), primary_key=True),
    Column("topic_id", INTEGER, ForeignKey("topics.id"), primary_key=True),
    Column("confidence", REAL),  # 0-1, set by the LLM at enrichment time
    Index("idx_chunk_topics_topic_id", "topic_id"),
)


# ---------------------------------------------------------------------
# chunk_entities — junction
# ---------------------------------------------------------------------
# `mention_position` is part of the primary key so the same entity
# mentioned multiple times in the same chunk gets multiple rows
# (different positions). Useful for UI highlighting later; harmless
# for v1 retrieval.
chunk_entities = Table(
    "chunk_entities",
    metadata,
    Column("chunk_id", INTEGER, ForeignKey("chunks.id"), primary_key=True),
    Column("entity_id", INTEGER, ForeignKey("entities.id"), primary_key=True),
    Column("mention_position", INTEGER, primary_key=True, server_default="0"),
    Column("confidence", REAL),
    Index("idx_chunk_entities_entity_id", "entity_id"),
)


__all__ = [
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
