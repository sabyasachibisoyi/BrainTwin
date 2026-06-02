"""EntityRepository — shared global vocabulary (B.7).

Entities are NOT tenant-scoped. "Anthropic" coined while processing
Sabya's content is the same entity for every other student.

Shape mirrors TopicRepository — same controlled-vocabulary flow at
enrichment time, same `find_or_create()` workhorse, same slug
normalization rules. The only difference is the extra `entity_type`
field (person / place / company / concept) which constrains the LLM's
output during enrichment.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from backend.storage.models import Entity
from backend.storage.repositories.base import BaseRepository
from backend.storage.repositories.topic_repo import normalize_slug
from backend.storage.schema import (
    captures,
    chunk_entities,
    chunks,
    entities,
)


# Allowed entity_type values. Kept here rather than as a CHECK
# constraint at the DB level so we can extend the vocabulary without
# a schema migration. Validation happens application-side.
ENTITY_TYPES = frozenset({"person", "place", "company", "concept"})


def _row_to_entity(row) -> Entity:
    return Entity(
        id=row.id,
        slug=row.slug,
        label=row.label,
        entity_type=row.entity_type,
        embedding=row.embedding,
        coined_at=row.coined_at,
    )


class EntityRepository(BaseRepository):
    async def get_by_slug(self, slug: str) -> Optional[Entity]:
        result = await self.session.execute(
            select(entities).where(entities.c.slug == slug)
        )
        row = result.first()
        return _row_to_entity(row) if row else None

    async def get_by_id(self, entity_id: int) -> Optional[Entity]:
        result = await self.session.execute(
            select(entities).where(entities.c.id == entity_id)
        )
        row = result.first()
        return _row_to_entity(row) if row else None

    async def find_or_create(
        self,
        *,
        label: str,
        entity_type: str,
        embedding: Optional[bytes] = None,
        slug: Optional[str] = None,
    ) -> Entity:
        """Atomic insert-or-return. Same race-safe pattern as
        TopicRepository.find_or_create — see that docstring for the
        ordering rationale.

        Raises ValueError if `entity_type` isn't in `ENTITY_TYPES`.
        We could relax this to allow LLM-coined types, but for v1
        the four-way split is enough."""
        if entity_type not in ENTITY_TYPES:
            raise ValueError(
                f"unknown entity_type={entity_type!r}; allowed: {sorted(ENTITY_TYPES)}"
            )

        canonical = normalize_slug(slug if slug is not None else label)
        if not canonical:
            raise ValueError(f"slug normalized to empty for label={label!r}")

        existing = await self.get_by_slug(canonical)
        if existing is not None:
            return existing

        try:
            now = datetime.now(timezone.utc).isoformat()
            result = await self.session.execute(insert(entities).values(
                slug=canonical,
                label=label,
                entity_type=entity_type,
                embedding=embedding,
                coined_at=now,
            ))
            await self.session.flush()
            new_id = int(result.inserted_primary_key[0])
            return Entity(
                id=new_id,
                slug=canonical,
                label=label,
                entity_type=entity_type,
                embedding=embedding,
                coined_at=now,
            )
        except IntegrityError:
            await self.session.rollback()
            existing = await self.get_by_slug(canonical)
            if existing is None:
                raise
            return existing

    async def list_all(self, limit: int = 1000) -> list[Entity]:
        result = await self.session.execute(
            select(entities).order_by(entities.c.coined_at.asc()).limit(limit)
        )
        return [_row_to_entity(row) for row in result]

    async def count_capture_mentions_by_user(self, *, user_id: int) -> int:
        """Distinct (capture_id, entity_id) pairs for this user.

        Phase 3.5 — replaces the old `/stats` JSONL scan that summed
        `len(enrichment["entities"])` across enrichments.jsonl. Same
        intent: how many entity mentions did this user's corpus
        produce? Implementation walks chunk_entities → chunks →
        captures and de-duplicates per (capture, entity) so the same
        entity attached to N chunks of one capture still counts once.
        The DISTINCT-pair-via-subquery form works on both SQLite and
        Postgres without dialect-specific tricks.
        """
        sub = (
            select(chunks.c.capture_id, chunk_entities.c.entity_id)
            .select_from(chunk_entities)
            .join(chunks, chunk_entities.c.chunk_id == chunks.c.id)
            .join(captures, chunks.c.capture_id == captures.c.id)
            .where(captures.c.user_id == user_id)
            .distinct()
            .subquery()
        )
        result = await self.session.execute(
            select(func.count()).select_from(sub)
        )
        return int(result.scalar_one())


__all__ = ["EntityRepository", "ENTITY_TYPES"]
