"""CaptureRepository — the tenant-scoped root domain table.

Every read takes `user_id` as a required keyword argument. Reads return
None / empty lists when the row exists but belongs to a different
tenant (we don't distinguish missing-from-not-yours, see base.py
docstring).
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func, insert, select

from backend.storage.models import Capture
from backend.storage.repositories.base import BaseRepository
from backend.storage.schema import captures, enrichments


def _row_to_capture(row) -> Capture:
    return Capture(
        id=row.id,
        user_id=row.user_id,
        url=row.url,
        title=row.title,
        platform=row.platform,
        content_type=row.content_type,
        captured_at=row.captured_at,
        dwell_seconds=row.dwell_seconds,
        raw_metadata_json=row.raw_metadata_json,
        clean_text=row.clean_text,
        transcript=row.transcript,
        image_text=row.image_text,
        image_descriptions_json=row.image_descriptions_json,
        text_source=row.text_source,
    )


class CaptureRepository(BaseRepository):
    async def create(self, capture: Capture) -> None:
        """Insert a capture row. Caller assigns `id` (the UUID4 from
        the extension/bot) and `user_id`. No DB-side defaults besides
        `dwell_seconds`."""
        await self.session.execute(insert(captures).values(
            id=capture.id,
            user_id=capture.user_id,
            url=capture.url,
            title=capture.title,
            platform=capture.platform,
            content_type=capture.content_type,
            captured_at=capture.captured_at,
            dwell_seconds=capture.dwell_seconds,
            raw_metadata_json=capture.raw_metadata_json,
            clean_text=capture.clean_text,
            transcript=capture.transcript,
            image_text=capture.image_text,
            image_descriptions_json=capture.image_descriptions_json,
            text_source=capture.text_source,
        ))

    async def get(
        self,
        capture_id: str,
        *,
        user_id: int,
    ) -> Optional[Capture]:
        """Look up by ID, with tenant check. Returns None if the row
        doesn't exist OR belongs to a different user."""
        result = await self.session.execute(
            select(captures).where(
                captures.c.id == capture_id,
                captures.c.user_id == user_id,
            )
        )
        row = result.first()
        return _row_to_capture(row) if row else None

    async def exists(self, capture_id: str) -> bool:
        """Existence check ignoring tenant. Used ONLY by the migration
        script (B.5) for idempotency — never call this from
        application code; it leaks existence across tenants."""
        result = await self.session.execute(
            select(captures.c.id).where(captures.c.id == capture_id)
        )
        return result.first() is not None

    async def list_by_user(
        self,
        *,
        user_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Capture]:
        """List a user's captures, newest first."""
        result = await self.session.execute(
            select(captures)
            .where(captures.c.user_id == user_id)
            .order_by(captures.c.captured_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [_row_to_capture(row) for row in result]

    async def count_by_user(self, *, user_id: int) -> int:
        """Total captures owned by a user. Useful for /stats."""
        result = await self.session.execute(
            select(func.count())
            .select_from(captures)
            .where(captures.c.user_id == user_id)
        )
        return int(result.scalar_one())

    # ---- Phase 3.5 — /stats and recovery helpers --------------------

    async def latest_captured_at(self, *, user_id: int) -> Optional[str]:
        """Most recent `captured_at` for this user (ISO 8601 string),
        or None if the user has no captures yet. Used by /stats."""
        result = await self.session.execute(
            select(func.max(captures.c.captured_at))
            .where(captures.c.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def platform_counts(self, *, user_id: int) -> dict[str, int]:
        """Capture counts grouped by platform. Used by /stats."""
        result = await self.session.execute(
            select(captures.c.platform, func.count())
            .where(captures.c.user_id == user_id)
            .group_by(captures.c.platform)
        )
        return {(row[0] or "unknown"): int(row[1]) for row in result}

    async def unenriched(
        self,
        *,
        user_id: int,
        exclude_capture_ids: Optional[set[str]] = None,
    ) -> list[Capture]:
        """Captures for `user_id` that have no enrichment row, oldest
        first (so retries process in capture order). The optional
        `exclude_capture_ids` set lets the caller filter out ids that
        were intentionally skipped (empty / oversized content tagged
        in capture_failures.jsonl) — we don't have a SQL skip table,
        so the failures log is still consulted for that classification.

        Replaces `find_unenriched_capture_ids` + the JSONL re-hydration
        loop from Phase 2. Phase 3.5 startup recovery uses this.
        """
        exclude_capture_ids = exclude_capture_ids or set()
        stmt = (
            select(captures)
            .outerjoin(enrichments, enrichments.c.capture_id == captures.c.id)
            .where(captures.c.user_id == user_id)
            .where(enrichments.c.id.is_(None))
            .order_by(captures.c.captured_at.asc())
        )
        result = await self.session.execute(stmt)
        out: list[Capture] = []
        for row in result:
            if row.id in exclude_capture_ids:
                continue
            out.append(_row_to_capture(row))
        return out
