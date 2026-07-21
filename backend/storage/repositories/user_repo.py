"""UserRepository — multi-tenant root.

Per docs/phase3-design.md B.5.4, `id=1` is reserved for Sabya
(`sabya.bisoyi@gmail.com`). The migration script seeds it; future
students get id=2, 3, ...

Methods:
    create(email, display_name, ...) -> User    — register a new user
    get(user_id) -> User | None                 — by primary key
    get_by_email(email) -> User | None          — by login email
    get_by_oauth_sub(sub) -> User | None        — by Google's stable subject
    bump_token_version(user_id) -> int          — revoke all live JWTs
    delete_user(user_id) -> None                — cascade-delete everything

Phase 4.1 M.M.1.a additions:
  - `oauth_google_sub`, `added_at`, `is_admin`, `token_version`,
    `is_eval` accepted on `create()` for the OAuth callback + the
    seed-eval-user script.
  - `get_by_oauth_sub()` — the OAuth callback's primary lookup: same
    email might be re-issued by Google, but `sub` is stable.
  - `bump_token_version()` — Codex Fix 3. Invalidates every live JWT
    for a user in one write; `get_current_user` compares against the
    `tv` claim.
  - `delete_user()` — the §5.3 privacy promise. Walks every
    user-owned table in FK-safe order (children before parents) since
    existing FKs don't carry ON DELETE CASCADE. The foreign_keys=ON
    pragma acts as a defense-in-depth guard: if we forget a child
    table here, the final DELETE FROM users raises IntegrityError
    instead of silently leaving orphans.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from backend.storage.models import User
from backend.storage.repositories.base import BaseRepository, DuplicateKeyError
from backend.storage.schema import (
    captures,
    chunk_entities,
    chunk_topics,
    chunks,
    enrichments,
    hydrations,
    telegram_bindings,
    usage_counters,
    users,
)


def _row_to_user(row) -> User:
    """Map a DB row to the User dataclass. Tolerates Row, RowMapping,
    and the dict-like result objects SQLAlchemy returns from different
    statement types.

    Phase 4.1 M.M.1.a: the 5 new fields default to None / 0 in the
    dataclass, so pre-4.1 rows (which might return None for is_admin
    etc. before the migration sweep populates the server_default) map
    cleanly with `or 0` guards on the INTEGER columns."""
    return User(
        id=row.id,
        email=row.email,
        display_name=row.display_name,
        created_at=row.created_at,
        # Phase 4.1 fields — server_default of "0" means the row-adds
        # sweep backfills existing rows with 0 automatically, so the
        # `or 0` here is belt-and-braces for rows that were somehow
        # created before the sweep completed on first startup.
        oauth_google_sub=row.oauth_google_sub,
        added_at=row.added_at,
        is_admin=bool(row.is_admin or 0),
        token_version=int(row.token_version or 0),
        is_eval=bool(row.is_eval or 0),
    )


class UserRepository(BaseRepository):
    async def create(
        self,
        *,
        email: str,
        display_name: Optional[str] = None,
        user_id: Optional[int] = None,
        oauth_google_sub: Optional[str] = None,
        added_at: Optional[str] = None,
        is_admin: bool = False,
        is_eval: bool = False,
    ) -> User:
        """Create a new user.

        `user_id` is optional and only used by the migration script to
        seed Sabya at id=1 (B.5.4). Production signups should leave it
        None and let SQLite auto-assign.

        Raises DuplicateKeyError if `email` OR `oauth_google_sub` is
        already registered — both are unique. `token_version` starts
        at 0 (server_default); the caller doesn't set it.

        Raises DuplicateKeyError if `email` is already registered."""
        now = datetime.now(timezone.utc).isoformat()
        values = {
            "email": email,
            "display_name": display_name,
            "created_at": now,
            "oauth_google_sub": oauth_google_sub,
            "added_at": added_at or now,
            "is_admin": 1 if is_admin else 0,
            "is_eval": 1 if is_eval else 0,
        }
        if user_id is not None:
            values["id"] = user_id
        try:
            result = await self.session.execute(insert(users).values(**values))
            await self.session.flush()
        except IntegrityError as e:
            raise DuplicateKeyError(
                f"email or oauth_google_sub already registered: {email}"
            ) from e

        # Get back the (possibly auto-assigned) primary key.
        new_id = user_id if user_id is not None else result.inserted_primary_key[0]
        return User(
            id=new_id,
            email=email,
            display_name=display_name,
            created_at=now,
            oauth_google_sub=oauth_google_sub,
            added_at=values["added_at"],
            is_admin=is_admin,
            token_version=0,
            is_eval=is_eval,
        )

    async def get(self, user_id: int) -> Optional[User]:
        result = await self.session.execute(
            select(users).where(users.c.id == user_id)
        )
        row = result.first()
        return _row_to_user(row) if row else None

    async def get_by_email(self, email: str) -> Optional[User]:
        """Look up by login email, case-insensitively.

        Email addresses are case-insensitive in the local-part per
        practice (and Google always returns lowercase), but an admin
        seeding the allowlist by hand might type `Friend@Gmail.com`.
        Comparing `lower(column) == lower(input)` means a casing mismatch
        between the seeded row and Google's returned email doesn't produce
        a spurious "not on the allowlist" 403. The allowlist table is
        tiny, so not using the raw-email unique index here is free."""
        result = await self.session.execute(
            select(users).where(
                func.lower(users.c.email) == email.strip().lower()
            )
        )
        row = result.first()
        return _row_to_user(row) if row else None

    async def get_by_oauth_sub(self, oauth_google_sub: str) -> Optional[User]:
        """Look up by Google's stable `sub` claim.

        The OAuth callback's primary lookup: Google can (rarely) re-
        issue an email to a different real person, but `sub` is
        stable for the lifetime of the account. Preferring sub over
        email at lookup time protects against that edge case."""
        result = await self.session.execute(
            select(users).where(users.c.oauth_google_sub == oauth_google_sub)
        )
        row = result.first()
        return _row_to_user(row) if row else None

    async def bump_token_version(self, user_id: int) -> int:
        """Increment `token_version` for the user; returns the new value.

        Codex Fix 3 — this is the ONLY way to invalidate live JWTs
        without carrying a server-side token blocklist (which stateless
        JWTs otherwise force us into). `get_current_user` compares this
        value against the JWT's `tv` claim and 401s on mismatch.

        Atomic — uses `UPDATE ... SET x = x + 1` at the DB (never
        read-modify-write in Python; Fable §6 pattern). Returns the
        new value so callers can log it or mint a fresh token that
        matches immediately if they want to keep the user logged in
        elsewhere (e.g., after a password change on a hypothetical
        future non-OAuth signup path).

        Raises ValueError if the user doesn't exist (post-condition:
        exactly one row updated)."""
        result = await self.session.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(token_version=users.c.token_version + 1)
            .returning(users.c.token_version)
        )
        row = result.first()
        if row is None:
            raise ValueError(f"user_id={user_id} does not exist")
        await self.session.flush()
        return int(row.token_version)

    async def set_oauth_sub(self, user_id: int, oauth_google_sub: str) -> None:
        """Bind Google's stable `sub` to a user row.

        The OAuth callback's first-sign-in backfill: a user allowlisted
        by email (no `sub` yet) gets their `sub` written on the first
        successful Google sign-in, so subsequent sign-ins short-circuit
        to `get_by_oauth_sub`. The callback only calls this when the
        existing `sub` is NULL — rebinding an already-bound row to a
        different `sub` is a rejected security event handled upstream,
        not a write this method performs.

        Raises ValueError if the user doesn't exist (post-condition:
        exactly one row updated)."""
        result = await self.session.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(oauth_google_sub=oauth_google_sub)
        )
        if result.rowcount == 0:
            raise ValueError(f"user_id={user_id} does not exist")
        await self.session.flush()

    async def set_admin(self, user_id: int, is_admin: bool) -> None:
        """Set the admin flag for a user.

        Used by the startup seed to grant is_admin to the default
        user (design doc §M.M.1: user_id=1 is the admin) — including
        backfilling a pre-4.1 row that predates the column, which the
        ADD COLUMN sweep initializes to 0.

        Raises ValueError if the user doesn't exist (post-condition:
        exactly one row updated)."""
        result = await self.session.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(is_admin=1 if is_admin else 0)
        )
        if result.rowcount == 0:
            raise ValueError(f"user_id={user_id} does not exist")
        await self.session.flush()

    async def delete_user(self, user_id: int) -> None:
        """Cascade-delete a user and every row they own.

        §5.3 privacy promise. Walks children before parents because
        existing FKs (from the Phase 3 schema) don't carry ON DELETE
        CASCADE and SQLite can't ALTER an existing FK to add it
        without table recreation. The foreign_keys=ON pragma (see
        db.py `_configure_sqlite_pragmas`) makes any forgotten child
        table fail loudly with IntegrityError on the final
        `DELETE FROM users` — a defense-in-depth guard against a
        future migration adding a new user-scoped table without
        remembering to walk it here.

        Order (verified against schema.py FK graph):
          1. chunk_topics / chunk_entities — reference chunks
          2. chunks — reference captures
          3. hydrations / enrichments — reference captures
          4. captures — reference users
          5. usage_counters / telegram_bindings — reference users
          6. users — the root

        NB: topics + entities are shared vocabulary (no user_id) —
        NOT deleted. Deleting a user does NOT unwind the vocabulary
        they contributed; that's by design (B.7) — the vocabulary
        outlives any individual user.

        Idempotent — deleting a nonexistent user_id is a no-op
        (returns silently). The caller wanting a distinction between
        "deleted" vs "wasn't there" should `get()` first."""
        session = self.session
        # 1. chunk_topics + chunk_entities — reach via chunks → captures
        #    → user_id. Executed as raw text so we can use a scalar
        #    subquery that references the chunks table (SQLAlchemy Core
        #    delete().where(x.in_(select(...))) also works but the raw
        #    text form is easier to read for a permission-critical
        #    delete cascade.
        await session.execute(text(
            "DELETE FROM chunk_topics WHERE chunk_id IN ("
            "  SELECT c.id FROM chunks c "
            "  JOIN captures ca ON c.capture_id = ca.id "
            "  WHERE ca.user_id = :uid"
            ")"
        ), {"uid": user_id})
        await session.execute(text(
            "DELETE FROM chunk_entities WHERE chunk_id IN ("
            "  SELECT c.id FROM chunks c "
            "  JOIN captures ca ON c.capture_id = ca.id "
            "  WHERE ca.user_id = :uid"
            ")"
        ), {"uid": user_id})
        # 2. chunks — reach via captures.
        await session.execute(
            delete(chunks).where(
                chunks.c.capture_id.in_(
                    select(captures.c.id).where(captures.c.user_id == user_id)
                )
            )
        )
        # 3. hydrations + enrichments — same pattern.
        await session.execute(
            delete(hydrations).where(
                hydrations.c.capture_id.in_(
                    select(captures.c.id).where(captures.c.user_id == user_id)
                )
            )
        )
        await session.execute(
            delete(enrichments).where(
                enrichments.c.capture_id.in_(
                    select(captures.c.id).where(captures.c.user_id == user_id)
                )
            )
        )
        # 4. captures.
        await session.execute(
            delete(captures).where(captures.c.user_id == user_id)
        )
        # 5. usage_counters + telegram_bindings.
        await session.execute(
            delete(usage_counters).where(usage_counters.c.user_id == user_id)
        )
        await session.execute(
            delete(telegram_bindings).where(
                telegram_bindings.c.user_id == user_id
            )
        )
        # 6. users — the root. If we forgot a child table, this raises
        # IntegrityError under foreign_keys=ON. That's the safety net.
        await session.execute(delete(users).where(users.c.id == user_id))
        await session.flush()
