"""UserRepository — multi-tenant root.

Per docs/phase3-design.md B.5.4, `id=1` is reserved for Sabya
(`sabya.bisoyi@gmail.com`). The migration script seeds it; future
students get id=2, 3, ...

Methods:
    create(email, display_name) -> User       — register a new user
    get(user_id) -> User | None               — by primary key
    get_by_email(email) -> User | None        — by login email
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from backend.storage.models import User
from backend.storage.repositories.base import BaseRepository, DuplicateKeyError
from backend.storage.schema import users


def _row_to_user(row) -> User:
    """Map a DB row to the User dataclass. Tolerates Row, RowMapping,
    and the dict-like result objects SQLAlchemy returns from different
    statement types."""
    return User(
        id=row.id,
        email=row.email,
        display_name=row.display_name,
        created_at=row.created_at,
    )


class UserRepository(BaseRepository):
    async def create(
        self,
        *,
        email: str,
        display_name: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> User:
        """Create a new user.

        `user_id` is optional and only used by the migration script to
        seed Sabya at id=1 (B.5.4). Production signups should leave it
        None and let SQLite auto-assign.

        Raises DuplicateKeyError if `email` is already registered."""
        values = {
            "email": email,
            "display_name": display_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if user_id is not None:
            values["id"] = user_id
        try:
            result = await self.session.execute(insert(users).values(**values))
            await self.session.flush()
        except IntegrityError as e:
            raise DuplicateKeyError(f"email already registered: {email}") from e

        # Get back the (possibly auto-assigned) primary key.
        new_id = user_id if user_id is not None else result.inserted_primary_key[0]
        return User(
            id=new_id,
            email=email,
            display_name=display_name,
            created_at=values["created_at"],
        )

    async def get(self, user_id: int) -> Optional[User]:
        result = await self.session.execute(
            select(users).where(users.c.id == user_id)
        )
        row = result.first()
        return _row_to_user(row) if row else None

    async def get_by_email(self, email: str) -> Optional[User]:
        result = await self.session.execute(
            select(users).where(users.c.email == email)
        )
        row = result.first()
        return _row_to_user(row) if row else None
