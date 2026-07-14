"""TelegramBindingRepository — telegram_user_id ↔ user_id.

Phase 4.1 M.M.1.a. Replaces the shared `ALLOWED_TELEGRAM_USER_IDS`
env var (M.7.5) — a broken model where every allowed Telegram user
implicitly wrote to Sabya's corpus. From here on, every Telegram
sender maps to exactly one BrainTwin user; unlinked senders get told
to `/link <code>` and never reach the backend (Fable §4.5.1).

M.M.1.a scope: data access only. The `/link` bot command + web-side
code minting land in M.M.4. This repo just gives that milestone the
API it needs.

Methods:
    get_by_telegram(id) -> TelegramBinding | None
    get_by_user(user_id) -> TelegramBinding | None
    set_binding(telegram_user_id, user_id) -> TelegramBinding
    delete_by_telegram(telegram_user_id) -> None
    delete_by_user(user_id) -> None

`set_binding` is UPSERT semantics in BOTH directions: one Telegram
account binds to at most one user, and one user holds at most one
binding. Re-linking either side replaces the old row (as opposed to
raising DuplicateKeyError) — matches the UX of "I connected to the
wrong account, let me redo /link" and "I switched Telegram accounts."
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, insert, or_, select

from backend.storage.models import TelegramBinding
from backend.storage.repositories.base import BaseRepository
from backend.storage.schema import telegram_bindings


def _row_to_binding(row) -> TelegramBinding:
    return TelegramBinding(
        telegram_user_id=row.telegram_user_id,
        user_id=row.user_id,
        linked_at=row.linked_at,
    )


class TelegramBindingRepository(BaseRepository):
    async def get_by_telegram(
        self, telegram_user_id: int
    ) -> Optional[TelegramBinding]:
        """Primary lookup path for the bot: on every incoming message,
        resolve `msg.from_user.id → user_id`. None → tell the sender
        to /link before doing anything backend-side."""
        result = await self.session.execute(
            select(telegram_bindings).where(
                telegram_bindings.c.telegram_user_id == telegram_user_id
            )
        )
        row = result.first()
        return _row_to_binding(row) if row else None

    async def get_by_user(self, user_id: int) -> Optional[TelegramBinding]:
        """Reverse lookup — mostly for the admin dashboard ("which
        Telegram account is linked to this user?"). Also useful in the
        web UI to render "connected as @<handle>" after sign-in."""
        result = await self.session.execute(
            select(telegram_bindings).where(
                telegram_bindings.c.user_id == user_id
            )
        )
        row = result.first()
        return _row_to_binding(row) if row else None

    async def set_binding(
        self, *, telegram_user_id: int, user_id: int
    ) -> TelegramBinding:
        """UPSERT the binding — 1:1 between Telegram accounts and users.

        Replaces any existing row on EITHER side: the Telegram account
        was linked to a different user (friend types /link with the
        wrong code, notices, retypes — the second /link wins, not
        raises), or the user was linked from a different Telegram
        account (switched phones/accounts). `linked_at` reflects the
        most recent binding time either way.

        Delete-then-insert in the caller's transaction rather than a
        dialect-specific ON CONFLICT: an upsert keyed on the PK alone
        can't clear a conflicting row on the user_id side, and this
        form is portable to Postgres unchanged. The unique index on
        user_id backstops the invariant if two set_bindings race."""
        now = datetime.now(timezone.utc).isoformat()
        await self.session.execute(
            delete(telegram_bindings).where(
                or_(
                    telegram_bindings.c.telegram_user_id == telegram_user_id,
                    telegram_bindings.c.user_id == user_id,
                )
            )
        )
        await self.session.execute(
            insert(telegram_bindings).values(
                telegram_user_id=telegram_user_id,
                user_id=user_id,
                linked_at=now,
            )
        )
        await self.session.flush()
        return TelegramBinding(
            telegram_user_id=telegram_user_id,
            user_id=user_id,
            linked_at=now,
        )

    async def delete_by_telegram(self, telegram_user_id: int) -> None:
        """Delete a binding by Telegram user id. Idempotent — deleting
        a nonexistent binding is a no-op. Called from the bot's
        `/unlink` command."""
        await self.session.execute(
            delete(telegram_bindings).where(
                telegram_bindings.c.telegram_user_id == telegram_user_id
            )
        )

    async def delete_by_user(self, user_id: int) -> None:
        """Delete all bindings for a user (should be at most one, but
        we don't rely on that). Called from `UserRepository.delete_user`
        as part of the §5.3 privacy cascade — but that helper does the
        delete inline for FK-safe ordering, so this method is mostly
        for admin tooling."""
        await self.session.execute(
            delete(telegram_bindings).where(
                telegram_bindings.c.user_id == user_id
            )
        )
