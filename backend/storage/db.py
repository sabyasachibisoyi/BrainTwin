"""Async SQLAlchemy engine + session factory for Phase 3 storage.

Per docs/phase3-design.md A.1: SQLite locally, Postgres-compatible
schema, single `database_url` connection string controls which engine
backs the storage layer. Migration to cloud is a connection-string
change with no code edits to the storage package.

Usage from the rest of the codebase:

    from backend.storage import session_scope, captures
    from sqlalchemy import select

    async with session_scope() as session:
        result = await session.execute(
            select(captures).where(captures.c.user_id == user_id)
        )
        rows = result.fetchall()

For one-shot writes:

    async with session_scope() as session:
        await session.execute(insert(captures).values(...))
        # commit happens automatically on exit unless an exception fires.

Engine + session factory are lazy-initialized — first call wins. Tests
can monkey-patch `_engine` / `_session_factory` to a fresh in-memory
SQLite without touching the real `data/braintwin.db`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.config import settings


logger = logging.getLogger(__name__)


# Lazy globals — first call to get_engine() creates them, every subsequent
# call returns the same instance. Tests reach in to swap these for an
# in-memory engine so they don't pollute the real `data/` directory.
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


# ---- URL translation -------------------------------------------------

# Standard SQLAlchemy URLs use sync driver names by default. We auto-translate
# to async drivers so callers don't have to know about the +driver suffix.
# Translations match what we'd see in production:
#   sqlite:///path.db          → sqlite+aiosqlite:///path.db
#   postgresql://...           → postgresql+asyncpg://...
#   postgresql+psycopg2://...  → left alone (caller knows what they want)
def _to_async_url(url: str) -> str:
    if url.startswith("sqlite:///") and "+" not in url.split("://", 1)[0]:
        return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    if url.startswith("postgresql://") and "+" not in url.split("://", 1)[0]:
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


# Mask credentials in URLs before logging — `postgresql://user:secret@host`
# becomes `postgresql://user:****@host`. SQLite URLs have no credentials.
def _safe_url(url: str) -> str:
    if "@" not in url:
        return url
    scheme_creds, host = url.split("@", 1)
    if "://" not in scheme_creds:
        return url
    scheme, creds = scheme_creds.split("://", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:****@{host}"
    return url


# ---- Engine + session factory ---------------------------------------

def get_engine() -> AsyncEngine:
    """Return the singleton AsyncEngine, creating it on first call.

    Honours `settings.database_url` and `settings.database_echo`. Auto-
    translates sync driver URLs to their async equivalents (aiosqlite
    for sqlite://, asyncpg for postgresql://).
    """
    global _engine
    if _engine is None:
        url = _to_async_url(settings.database_url)
        # For SQLite, ensure the parent directory exists. SQLite happily
        # creates the .db file if missing, but it doesn't create dirs.
        if url.startswith("sqlite+aiosqlite:///"):
            db_path = url.replace("sqlite+aiosqlite:///", "", 1)
            # ":memory:" is special — no parent directory.
            if db_path and db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(
            url,
            echo=settings.database_echo,
            future=True,
        )
        logger.info("Created SQL engine: %s", _safe_url(url))
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the singleton async_sessionmaker, creating it on first call."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,  # we use Core, not ORM — no need to re-fetch on commit
        )
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield a session that auto-commits on success and rolls back on error.

    The standard pattern for everything in the storage layer. Handles
    transaction lifecycle so callers don't have to remember to commit.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---- Lifecycle hooks -------------------------------------------------

async def init_db() -> None:
    """Create all tables from `metadata`. Idempotent — safe to call on
    every startup. CREATE TABLE IF NOT EXISTS semantics under the hood.

    This is the only schema-management entry point in v1. When the
    schema evolves and we need migrations beyond "add a new table,"
    we'll bring in alembic — see Phase 3.5/4 doc for that decision.
    """
    # Local import to avoid circular imports at module load time:
    # backend.storage.__init__ imports both schema and db, and we want
    # db to be importable before metadata is fully built.
    from backend.storage.schema import metadata
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    logger.info("Database schema initialized (tables: %d)", len(metadata.tables))


async def aclose() -> None:
    """Dispose of the engine and reset module state.

    Call from FastAPI's shutdown event so the connection pool drains
    cleanly. Tests also call this between cases to ensure a clean
    slate when they swap in a different `database_url`.
    """
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
    _session_factory = None
    logger.info("SQL engine closed")


__all__ = [
    "get_engine",
    "get_session_factory",
    "session_scope",
    "init_db",
    "aclose",
]
