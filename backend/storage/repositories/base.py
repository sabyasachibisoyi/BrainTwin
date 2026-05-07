"""Shared repository helpers + exception types.

All repositories take an `AsyncSession` in their constructor and use it
for every query. Sessions come from `backend.storage.session_scope()` —
the standard pattern is:

    async with session_scope() as session:
        repo = CaptureRepository(session)
        capture = await repo.get(capture_id, user_id=1)

Tenant isolation rule: every read / write on a user-scoped table
(captures, hydrations, enrichments, chunks, junctions) takes
`user_id` as a REQUIRED keyword argument. The repo enforces tenant
match at the SQL level — a tenant violation returns None / empty
list rather than raising, so the caller can't distinguish "doesn't
exist" from "not yours" (standard multi-tenant security posture).

Topic and Entity repositories deliberately do NOT take `user_id`
because they're shared global vocabulary (per docs/phase3-design.md
B.7).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class RepositoryError(Exception):
    """Base class for repository-level errors that aren't simple "not
    found" cases. Use sparingly — returning None / empty is preferred
    for normal misses."""


class DuplicateKeyError(RepositoryError):
    """A unique constraint was violated. Used by repositories whose
    create() method makes the conflict-vs-success distinction
    semantically meaningful (e.g. UserRepository.create with an
    already-registered email)."""


class BaseRepository:
    """Tiny base — just holds the session. Repositories aren't an OO
    hierarchy in any meaningful sense; this is purely to share the
    constructor and avoid copy-paste."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession):
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session
