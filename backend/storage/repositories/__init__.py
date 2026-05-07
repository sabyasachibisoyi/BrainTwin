"""Repository pattern for the Phase 3 storage layer.

Each repository encapsulates one domain table (or one table plus its
junctions). Repositories take an `AsyncSession` in their constructor;
the standard usage from elsewhere in the codebase is:

    from backend.storage import session_scope
    from backend.storage.repositories import (
        CaptureRepository, EnrichmentRepository,
    )

    async with session_scope() as session:
        captures_repo = CaptureRepository(session)
        capture = await captures_repo.get(capture_id, user_id=1)

Tenant safety rule: every read / write on a user-scoped table takes
`user_id` as a REQUIRED keyword argument. Tenant violations return
None / empty (not raise). Topic and Entity repositories are
deliberately exempt — they're shared global vocabulary per B.7.

Public exports below are the surface the rest of the codebase
depends on.
"""

from backend.storage.repositories.base import (
    BaseRepository,
    DuplicateKeyError,
    RepositoryError,
)
from backend.storage.repositories.capture_repo import CaptureRepository
from backend.storage.repositories.chunk_repo import ChunkRepository
from backend.storage.repositories.entity_repo import (
    ENTITY_TYPES,
    EntityRepository,
)
from backend.storage.repositories.enrichment_repo import EnrichmentRepository
from backend.storage.repositories.hydration_repo import HydrationRepository
from backend.storage.repositories.topic_repo import (
    TopicRepository,
    normalize_slug,
)
from backend.storage.repositories.user_repo import UserRepository


__all__ = [
    # base
    "BaseRepository",
    "RepositoryError",
    "DuplicateKeyError",
    # repositories
    "UserRepository",
    "CaptureRepository",
    "HydrationRepository",
    "EnrichmentRepository",
    "ChunkRepository",
    "TopicRepository",
    "EntityRepository",
    # helpers
    "normalize_slug",
    "ENTITY_TYPES",
]
