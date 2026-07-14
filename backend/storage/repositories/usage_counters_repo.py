"""UsageCountersRepository — daily per-user rate-limit + spend accounting.

Phase 4.1 M.M.1.a. Backs the quota gate in `check_quota()` (design
doc §6). Not yet wired into any route — that's M.M.2. This substep
only ships the data-access layer.

Concurrency model:
    - `get_or_create()` inserts a zero row if the (user_id, date_utc)
      row doesn't exist yet. `INSERT OR IGNORE` semantics; a
      concurrent create loses cleanly.
    - `bump()` does the increment at the DB via
      `INSERT ... ON CONFLICT DO UPDATE SET x = x + ?`, creating the
      row if it doesn't exist yet. Never read-modify-write in Python
      (Fable §6 — the naive form undercounts under concurrent
      captures on the same day, exactly the pattern we care about).
    - `get()` is used by `check_quota()` BEFORE bumping to check the
      cap. Under contention, two captures could both read below-cap
      and both proceed — a ±1 overshoot on the cap. That's acceptable
      (design doc §6: caps are soft under concurrent bursts by
      design; the hard cap is the Anthropic Console monthly spend cap
      §6.1 which our AWS-side counters can't see anyway).

Retention: rows for the current day are always kept; older rows get
swept nightly by a job that isn't part of M.M.1.a. Keep the
retention window in mind if you add reporting queries on top later.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.storage.models import UsageCounters
from backend.storage.repositories.base import BaseRepository
from backend.storage.schema import usage_counters


def _row_to_counters(row) -> UsageCounters:
    return UsageCounters(
        user_id=row.user_id,
        date_utc=row.date_utc,
        captures=row.captures,
        recalls=row.recalls,
        tokens=row.tokens,
    )


class UsageCountersRepository(BaseRepository):
    async def get(
        self, *, user_id: int, date_utc: str
    ) -> Optional[UsageCounters]:
        """Read the counter row for one (user, day). Returns None if
        the row hasn't been created yet — the caller should treat
        that as "all counts are 0"."""
        result = await self.session.execute(
            select(usage_counters).where(
                usage_counters.c.user_id == user_id,
                usage_counters.c.date_utc == date_utc,
            )
        )
        row = result.first()
        return _row_to_counters(row) if row else None

    async def get_or_create(
        self, *, user_id: int, date_utc: str
    ) -> UsageCounters:
        """Return the counter row for one (user, day), creating it at
        zero if it doesn't exist yet.

        Uses SQLite's `INSERT ... ON CONFLICT DO NOTHING` (RFC-standard
        `INSERT OR IGNORE` behavior). Two concurrent workers hitting
        this for the same (user, day) both get a valid row back — the
        loser's INSERT is a no-op, and both callers proceed on the
        winner's row.

        On Postgres the same pattern is `INSERT ... ON CONFLICT DO
        NOTHING` (built into SQLAlchemy Core; we'd swap the import).
        Keeping the dialect-specific import for now — this is SQLite-
        only until B.5's Postgres migration."""
        stmt = (
            sqlite_insert(usage_counters)
            .values(
                user_id=user_id,
                date_utc=date_utc,
                captures=0,
                recalls=0,
                tokens=0,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    usage_counters.c.user_id,
                    usage_counters.c.date_utc,
                ],
            )
        )
        await self.session.execute(stmt)
        # Re-read: works whether we inserted or lost the race.
        row = await self.get(user_id=user_id, date_utc=date_utc)
        # get() can't be None post-insert-or-ignore + re-read on the
        # same session (we just wrote or someone else did). Belt +
        # braces for typing.
        assert row is not None
        return row

    async def bump(
        self,
        *,
        user_id: int,
        date_utc: str,
        captures: int = 0,
        recalls: int = 0,
        tokens: int = 0,
    ) -> None:
        """Atomically add to one or more counter fields.

        Any field left at 0 is a no-op — same query, no penalty. The
        caller's typical shape is `bump(captures=1)` after a
        successful /capture, or `bump(tokens=response.usage.total)`
        after each Anthropic call.

        Upsert-increment: creates the row (at the bumped values) if it
        doesn't exist yet. A plain UPDATE would match 0 rows and
        silently drop the increment whenever the (user, day) row is
        missing — e.g. a request that ran `get_or_create()` just
        before UTC midnight and bumps with the new day's `date_utc`
        after the Anthropic call returns."""
        await self.session.execute(
            sqlite_insert(usage_counters)
            .values(
                user_id=user_id,
                date_utc=date_utc,
                captures=captures,
                recalls=recalls,
                tokens=tokens,
            )
            .on_conflict_do_update(
                index_elements=[
                    usage_counters.c.user_id,
                    usage_counters.c.date_utc,
                ],
                set_={
                    "captures": usage_counters.c.captures + captures,
                    "recalls": usage_counters.c.recalls + recalls,
                    "tokens": usage_counters.c.tokens + tokens,
                },
            )
        )
