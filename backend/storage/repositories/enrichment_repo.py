"""EnrichmentRepository — Phase 2 enrichment records lifted into SQL.

Provides the SQL equivalent of Phase 2's `find_unenriched_capture_ids`
(via `enriched_capture_ids`) so the startup recovery hook can move
off JSONL scanning once dual-write completes (B.1).
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func, insert, select

from backend.storage.models import (
    Capture,
    CaptureWithEnrichment,
    Enrichment,
)
from backend.storage.repositories.base import BaseRepository
from backend.storage.schema import captures, enrichments


def _row_to_enrichment(row) -> Enrichment:
    return Enrichment(
        id=row.id,
        capture_id=row.capture_id,
        summary=row.summary,
        key_facts_json=row.key_facts_json,
        model=row.model,
        enriched_at=row.enriched_at,
    )


class EnrichmentRepository(BaseRepository):
    async def create(
        self,
        *,
        capture_id: str,
        summary: Optional[str],
        key_facts_json: Optional[str],
        model: Optional[str],
        enriched_at: str,
    ) -> int:
        """Insert an enrichment row. Caller has already verified that
        `capture_id` belongs to the right tenant (via
        CaptureRepository.get)."""
        result = await self.session.execute(insert(enrichments).values(
            capture_id=capture_id,
            summary=summary,
            key_facts_json=key_facts_json,
            model=model,
            enriched_at=enriched_at,
        ))
        return int(result.inserted_primary_key[0])

    async def get_by_capture(
        self,
        capture_id: str,
        *,
        user_id: int,
    ) -> Optional[Enrichment]:
        """Most recent enrichment for a capture, with tenant check.

        We expect at most one enrichment per capture in normal
        operation but ORDER BY enriched_at DESC LIMIT 1 means
        re-enrichments (Phase 5+ when the agent updates summaries)
        won't break this method."""
        result = await self.session.execute(
            select(enrichments)
            .join(captures, enrichments.c.capture_id == captures.c.id)
            .where(captures.c.user_id == user_id)
            .where(enrichments.c.capture_id == capture_id)
            .order_by(enrichments.c.enriched_at.desc())
            .limit(1)
        )
        row = result.first()
        return _row_to_enrichment(row) if row else None

    async def enriched_capture_ids(
        self,
        *,
        user_id: int,
    ) -> set[str]:
        """Set of capture_ids belonging to this user that have at least
        one enrichment row. Used by the migration script (B.5) and the
        startup recovery hook to find what still needs enrichment."""
        result = await self.session.execute(
            select(enrichments.c.capture_id)
            .join(captures, enrichments.c.capture_id == captures.c.id)
            .where(captures.c.user_id == user_id)
            .distinct()
        )
        return {row.capture_id for row in result}

    async def get_summaries_by_capture_ids(
        self,
        capture_ids: list[str],
        *,
        user_id: int,
    ) -> dict[str, Optional[str]]:
        """Bulk fetch the most-recent enrichment summary per capture_id,
        tenant-checked. Returns a mapping from capture_id to the summary
        string (or None when the enrichment exists but has no summary).
        Capture_ids without any enrichment row simply don't appear in
        the result.

        Phase 4 M.3 — Recaller calls this once per /recall to attach
        summaries to the candidate captures before the Sonnet rerank
        prompt and the response shape. One bulk SQL call is cheaper
        than N round-trips on the hot path.

        Multiple enrichments per capture (Phase 5+ re-enrichment) →
        we keep the most recent one by `enriched_at` desc. Python-side
        dedup is simpler than SQL's window functions here and the
        candidate count is small (~6).
        """
        if not capture_ids:
            return {}
        result = await self.session.execute(
            select(
                enrichments.c.capture_id,
                enrichments.c.summary,
                enrichments.c.enriched_at,
            )
            .join(captures, enrichments.c.capture_id == captures.c.id)
            .where(captures.c.user_id == user_id)
            .where(enrichments.c.capture_id.in_(capture_ids))
            .order_by(enrichments.c.enriched_at.desc())
        )
        out: dict[str, Optional[str]] = {}
        for row in result:
            # First row per capture_id wins (most-recent-first ordering).
            if row.capture_id not in out:
                out[row.capture_id] = row.summary
        return out

    async def count_enriched_captures_by_user(self, *, user_id: int) -> int:
        """Number of distinct captures the user has at least one
        enrichment row for. Used by /stats — replaces the JSONL scan
        that counted unique capture_ids in enrichments.jsonl."""
        result = await self.session.execute(
            select(func.count(func.distinct(enrichments.c.capture_id)))
            .select_from(enrichments)
            .join(captures, enrichments.c.capture_id == captures.c.id)
            .where(captures.c.user_id == user_id)
        )
        return int(result.scalar_one())

    async def list_by_user(
        self,
        *,
        user_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CaptureWithEnrichment]:
        """List a user's captures with their (most recent) enrichment.

        Captures without an enrichment row appear with `enrichment=None`.
        Newest captures first. Used by the agent layer in Phase 4 to
        build quizzes."""
        # Multi-table SELECT: ask for the mapping view so columns can
        # be indexed by Column object. Without `.mappings()` the rows
        # come back as positional tuples and `row[captures.c.id]`
        # fails with a TypeError.
        result = await self.session.execute(
            select(
                captures, enrichments,
            )
            .outerjoin(
                enrichments,
                enrichments.c.capture_id == captures.c.id,
            )
            .where(captures.c.user_id == user_id)
            .order_by(captures.c.captured_at.desc())
            .limit(limit)
            .offset(offset)
        )
        out: list[CaptureWithEnrichment] = []
        for row in result.mappings():
            cap = Capture(
                id=row[captures.c.id],
                user_id=row[captures.c.user_id],
                url=row[captures.c.url],
                title=row[captures.c.title],
                platform=row[captures.c.platform],
                content_type=row[captures.c.content_type],
                captured_at=row[captures.c.captured_at],
                dwell_seconds=row[captures.c.dwell_seconds],
                raw_metadata_json=row[captures.c.raw_metadata_json],
            )
            enr_id = row[enrichments.c.id]
            enr = (
                Enrichment(
                    id=enr_id,
                    capture_id=row[enrichments.c.capture_id],
                    summary=row[enrichments.c.summary],
                    key_facts_json=row[enrichments.c.key_facts_json],
                    model=row[enrichments.c.model],
                    enriched_at=row[enrichments.c.enriched_at],
                )
                if enr_id is not None else None
            )
            out.append(CaptureWithEnrichment(capture=cap, enrichment=enr))
        return out
