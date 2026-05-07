"""TopicRepository — shared global vocabulary (B.7).

Topics are NOT tenant-scoped. "Kanban" coined by one student is the
same topic for every other student. Tenant isolation flows through
the chunk_topics junction (which connects to chunks → captures →
user_id), not through the topics table itself.

The controlled-vocabulary flow at enrichment time (per B.7):
  1. Compute the embedding of the new capture's summary.
  2. Query the `topics` ChromaDB collection for top-K most similar
     existing topics. (That call lives in `vector_store.py` Step 2,
     not here — this repo only owns the SQL side.)
  3. Pass the candidate shortlist to the enrichment LLM.
  4. LLM returns existing matches + any new coinages.
  5. For each match: link via chunk_topics.
  6. For each coinage: call `find_or_create()` here, which inserts
     the new topic (with its label-embedding) and returns the row.

`find_or_create()` is the workhorse — atomic insert-or-return pattern
that keeps slug normalization in one place.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from backend.storage.models import Topic
from backend.storage.repositories.base import BaseRepository
from backend.storage.schema import topics


# ---- Slug normalization (B.7.1) -------------------------------------

_SLUG_NON_ALPHANUM = re.compile(r"[^a-z0-9-]+")
_SLUG_DASH_RUNS = re.compile(r"-{2,}")
_MAX_SLUG_LEN = 64


def normalize_slug(raw: str) -> str:
    """Convert any string to a canonical topic/entity slug.

    Rules per B.7.1:
      - Lowercase
      - Strip leading/trailing whitespace
      - Replace whitespace runs with single hyphen
      - Strip everything that isn't [a-z0-9-]
      - Collapse multiple hyphens to one
      - Trim leading/trailing hyphens
      - Cap at 64 characters

    Examples:
      "Kanban Method"         -> "kanban-method"
      "  ML / AI  "           -> "ml-ai"
      "Mañana"                -> "maana" (diacritics dropped)
    """
    s = raw.strip().lower()
    # whitespace -> hyphen, then non-alphanum -> empty
    s = re.sub(r"\s+", "-", s)
    s = _SLUG_NON_ALPHANUM.sub("", s)
    s = _SLUG_DASH_RUNS.sub("-", s)
    s = s.strip("-")
    return s[:_MAX_SLUG_LEN]


def _row_to_topic(row) -> Topic:
    return Topic(
        id=row.id,
        slug=row.slug,
        label=row.label,
        description=row.description,
        embedding=row.embedding,
        coined_at=row.coined_at,
    )


class TopicRepository(BaseRepository):
    async def get_by_slug(self, slug: str) -> Optional[Topic]:
        """Look up by canonical slug. Caller is responsible for
        normalizing the slug first if it came from user input;
        normalize_slug() is exported above."""
        result = await self.session.execute(
            select(topics).where(topics.c.slug == slug)
        )
        row = result.first()
        return _row_to_topic(row) if row else None

    async def get_by_id(self, topic_id: int) -> Optional[Topic]:
        result = await self.session.execute(
            select(topics).where(topics.c.id == topic_id)
        )
        row = result.first()
        return _row_to_topic(row) if row else None

    async def find_or_create(
        self,
        *,
        label: str,
        description: Optional[str] = None,
        embedding: Optional[bytes] = None,
        slug: Optional[str] = None,
    ) -> Topic:
        """Atomic insert-or-return. The race-safe pattern:
          1. Normalize the slug (or use the explicit one provided).
          2. Try INSERT.
          3. On IntegrityError (slug already exists), SELECT the
             existing row and return it.

        This ordering is correct under concurrent writes — at most
        one INSERT wins, the other side's IntegrityError fires the
        SELECT path."""
        canonical = normalize_slug(slug if slug is not None else label)
        if not canonical:
            raise ValueError(f"slug normalized to empty for label={label!r}")

        existing = await self.get_by_slug(canonical)
        if existing is not None:
            return existing

        try:
            now = datetime.now(timezone.utc).isoformat()
            result = await self.session.execute(insert(topics).values(
                slug=canonical,
                label=label,
                description=description,
                embedding=embedding,
                coined_at=now,
            ))
            await self.session.flush()
            new_id = int(result.inserted_primary_key[0])
            return Topic(
                id=new_id,
                slug=canonical,
                label=label,
                description=description,
                embedding=embedding,
                coined_at=now,
            )
        except IntegrityError:
            # Another writer raced us. Roll back the failed insert
            # within this session, then re-fetch.
            await self.session.rollback()
            existing = await self.get_by_slug(canonical)
            if existing is None:
                # Shouldn't happen — IntegrityError means the row exists.
                raise
            return existing

    async def list_all(self, limit: int = 1000) -> list[Topic]:
        """Dump every topic. Used by migration scripts and admin
        tooling; not for hot-path application code (which uses
        embedding-similarity lookup via the vector store)."""
        result = await self.session.execute(
            select(topics).order_by(topics.c.coined_at.asc()).limit(limit)
        )
        return [_row_to_topic(row) for row in result]


__all__ = ["TopicRepository", "normalize_slug"]
