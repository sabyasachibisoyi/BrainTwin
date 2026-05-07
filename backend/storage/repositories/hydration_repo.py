"""HydrationRepository — Phase 2.5 sidecar lifted into SQL.

One row per hydration that filled in an empty capture (OG fetch or
video transcript). Tenant isolation comes through the join to
captures — every read takes `user_id` and joins captures to verify
the tenant owns the parent row.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import insert, select

from backend.storage.models import Hydration
from backend.storage.repositories.base import BaseRepository
from backend.storage.schema import captures, hydrations


def _row_to_hydration(row) -> Hydration:
    return Hydration(
        id=row.id,
        capture_id=row.capture_id,
        tier=row.tier,
        source_payload_json=row.source_payload_json,
        hydrated_at=row.hydrated_at,
    )


class HydrationRepository(BaseRepository):
    async def create(
        self,
        *,
        capture_id: str,
        tier: str,
        source_payload_json: Optional[str],
        hydrated_at: str,
    ) -> int:
        """Insert a hydration row. Returns the new id.

        Caller is responsible for tenant verification before calling —
        typically by having loaded the parent capture via
        CaptureRepository.get(capture_id, user_id=...). This avoids a
        redundant tenant check on the hot dual-write path."""
        result = await self.session.execute(insert(hydrations).values(
            capture_id=capture_id,
            tier=tier,
            source_payload_json=source_payload_json,
            hydrated_at=hydrated_at,
        ))
        return int(result.inserted_primary_key[0])

    async def list_by_capture(
        self,
        capture_id: str,
        *,
        user_id: int,
    ) -> list[Hydration]:
        """All hydration rows for one capture, oldest first. Joins to
        captures so a tenant can't read another tenant's hydrations
        even if they somehow guess a capture_id."""
        result = await self.session.execute(
            select(hydrations)
            .join(captures, hydrations.c.capture_id == captures.c.id)
            .where(captures.c.user_id == user_id)
            .where(hydrations.c.capture_id == capture_id)
            .order_by(hydrations.c.hydrated_at.asc())
        )
        return [_row_to_hydration(row) for row in result]
