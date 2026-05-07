"""VectorStore abstraction for the Phase 3 storage layer.

Per docs/phase3-design.md A.7 / B.3: ChromaDB locally and in cloud
through Phase 5; pgvector swap is a Phase 6+ exercise. The Protocol
below is what the rest of the codebase calls; ChromaVectorStore is
the only impl in v1, but a future PgVectorStore would land alongside
it without touching call sites.

Three collections (per B.3):
  - "chunks"   — one vector per row in the SQL `chunks` table.
                 Tenant-filtered at query time via metadata `user_id`.
  - "topics"   — one vector per row in `topics`. Shared globally
                 (no user_id filter — vocabulary is shared per B.7).
  - "entities" — same shape as topics.

All collections use cosine similarity (per B.2 / A.6) — chosen because
all-MiniLM-L6-v2 is trained with cosine objective and cosine is
magnitude-invariant (good for chunks of varying length).

Async story: ChromaDB's persistent client is sync, so every method
wraps the underlying call in `asyncio.to_thread` to avoid blocking
the FastAPI event loop. The Protocol shape is async-first so the
eventual pgvector implementation (which uses asyncpg natively) drops
in cleanly.

Metadata convention (per B.2):
  - chunks: {user_id, capture_id, source_kind, captured_at}
  - topics, entities: {} (no per-vector metadata; shared vocab)

Caller is responsible for passing the right metadata for each
collection. VectorStore doesn't validate.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Protocol

import numpy as np

from backend.config import settings
from backend.storage.embedder import Embedder, get_embedder

if TYPE_CHECKING:
    import chromadb


logger = logging.getLogger(__name__)


# Collection names defined as constants so callers can't typo a name
# into a brand-new empty collection by accident.
COLLECTION_CHUNKS = "chunks"
COLLECTION_TOPICS = "topics"
COLLECTION_ENTITIES = "entities"
ALL_COLLECTIONS = (COLLECTION_CHUNKS, COLLECTION_TOPICS, COLLECTION_ENTITIES)


# ---- Result type -----------------------------------------------------

@dataclass(frozen=True)
class VectorHit:
    """One result from a similarity query.

    `distance` is the underlying space's distance metric — for cosine
    space, 0 means identical and larger means more different (max
    around 2 for opposite vectors). Caller can compute similarity as
    `1 - distance / 2` if a 0..1 score is needed.

    `metadata` carries whatever the caller stored at insert time.
    `document` is the original text if Chroma was given one (it stores
    the document alongside the embedding so we can do `where_document`
    keyword filters later — Phase 4 hybrid retrieval).
    """
    id: str
    distance: float
    metadata: dict[str, Any]
    document: Optional[str] = None


# ---- Protocol --------------------------------------------------------

class VectorStore(Protocol):
    """Contract for any vector backend. ChromaVectorStore is the only
    impl in Phase 3; future PgVectorStore lands here without changing
    call sites.

    All methods are async; sync impls (Chroma) wrap with `asyncio.to_thread`.
    Native-async impls (asyncpg + pgvector) will use real awaits.
    """

    async def add(
        self,
        collection: str,
        *,
        ids: list[str],
        embeddings: list[np.ndarray],
        metadatas: list[dict[str, Any]],
        documents: Optional[list[str]] = None,
    ) -> None: ...

    async def upsert(
        self,
        collection: str,
        *,
        ids: list[str],
        embeddings: list[np.ndarray],
        metadatas: list[dict[str, Any]],
        documents: Optional[list[str]] = None,
    ) -> None: ...

    async def query(
        self,
        collection: str,
        *,
        embedding: np.ndarray,
        where: Optional[dict[str, Any]] = None,
        top_k: int = 20,
    ) -> list[VectorHit]: ...

    async def query_text(
        self,
        collection: str,
        *,
        text: str,
        where: Optional[dict[str, Any]] = None,
        top_k: int = 20,
    ) -> list[VectorHit]: ...

    async def delete(
        self,
        collection: str,
        ids: list[str],
    ) -> None: ...

    async def count(self, collection: str) -> int: ...


# ---- ChromaDB implementation -----------------------------------------

class ChromaVectorStore:
    """File-backed ChromaDB implementation. All three collections share
    one PersistentClient pointing at `settings.chroma_path`.

    Constructor takes the embedder (used by `query_text`) and an
    optional path override (mostly for tests, which point at a tmp
    directory so test runs don't pollute `data/chroma/`).

    Collections are created lazily on first use with cosine similarity
    space. `get_or_create_collection` is idempotent across runs, so
    after the first capture lands the collection exists; subsequent
    runs just reuse it.
    """

    def __init__(
        self,
        *,
        embedder: Optional[Embedder] = None,
        path: Optional[str] = None,
    ):
        self._embedder = embedder if embedder is not None else get_embedder()
        self._path = path if path is not None else settings.chroma_path
        self._client: "chromadb.api.ClientAPI | None" = None
        self._collections: dict[str, Any] = {}

    # ---- Lazy client + collection access -----------------------------

    def _ensure_client(self) -> Any:
        if self._client is None:
            import chromadb
            logger.info("Opening ChromaDB persistent store at %s", self._path)
            self._client = chromadb.PersistentClient(path=self._path)
        return self._client

    def _get_collection(self, name: str) -> Any:
        """Return the chroma Collection object for `name`, creating it
        on first use. Cosine space is set at create time and never
        changes afterward — the underlying HNSW index is built for that
        metric."""
        if name not in self._collections:
            client = self._ensure_client()
            self._collections[name] = client.get_or_create_collection(
                name=name,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collections[name]

    # ---- Helpers -----------------------------------------------------

    @staticmethod
    def _embeddings_as_lists(arrs: list[np.ndarray]) -> list[list[float]]:
        """ChromaDB's API takes embeddings as list[list[float]] (or
        numpy 2-D, but lists are what the type stub documents). Convert
        in one place so callers can stay in numpy-land."""
        return [arr.astype(np.float32).tolist() for arr in arrs]

    @staticmethod
    def _parse_query_response(result: dict[str, Any]) -> list[VectorHit]:
        """Chroma returns nested lists keyed by query (we always pass
        one query). Pull out the first row, line up the parallel
        arrays, build VectorHit objects."""
        ids_list = result.get("ids") or [[]]
        distances_list = result.get("distances") or [[]]
        metadatas_list = result.get("metadatas") or [[]]
        documents_list = result.get("documents") or [[]]
        ids = ids_list[0] if ids_list else []
        distances = distances_list[0] if distances_list else []
        metadatas = metadatas_list[0] if metadatas_list else []
        documents = documents_list[0] if documents_list else []
        hits: list[VectorHit] = []
        for i, _id in enumerate(ids):
            hits.append(VectorHit(
                id=_id,
                distance=float(distances[i]) if i < len(distances) else 0.0,
                metadata=dict(metadatas[i]) if i < len(metadatas) and metadatas[i] else {},
                document=documents[i] if i < len(documents) else None,
            ))
        return hits

    # ---- Public API --------------------------------------------------

    async def add(
        self,
        collection: str,
        *,
        ids: list[str],
        embeddings: list[np.ndarray],
        metadatas: list[dict[str, Any]],
        documents: Optional[list[str]] = None,
    ) -> None:
        if not ids:
            return
        coll = self._get_collection(collection)
        embs = self._embeddings_as_lists(embeddings)
        await asyncio.to_thread(
            coll.add,
            ids=ids,
            embeddings=embs,
            metadatas=metadatas,
            documents=documents,
        )

    async def upsert(
        self,
        collection: str,
        *,
        ids: list[str],
        embeddings: list[np.ndarray],
        metadatas: list[dict[str, Any]],
        documents: Optional[list[str]] = None,
    ) -> None:
        """Idempotent variant of add — same id silently overwrites
        instead of raising. Used by the migration script (B.5) so
        re-running the script doesn't fail on existing rows."""
        if not ids:
            return
        coll = self._get_collection(collection)
        embs = self._embeddings_as_lists(embeddings)
        await asyncio.to_thread(
            coll.upsert,
            ids=ids,
            embeddings=embs,
            metadatas=metadatas,
            documents=documents,
        )

    async def query(
        self,
        collection: str,
        *,
        embedding: np.ndarray,
        where: Optional[dict[str, Any]] = None,
        top_k: int = 20,
    ) -> list[VectorHit]:
        """Top-K nearest neighbors by cosine distance.

        `where` is Chroma's metadata filter dict — for our common case
        of tenant isolation it's `{"user_id": user_id}`. More complex
        filters use Chroma's $and / $or / $in operators (see Chroma
        docs); we pass the dict through unchanged."""
        coll = self._get_collection(collection)
        emb = embedding.astype(np.float32).tolist()
        # Chroma raises if the collection is empty but n_results > 0
        # in some versions, so guard with a count check.
        existing = await asyncio.to_thread(coll.count)
        if existing == 0:
            return []
        result = await asyncio.to_thread(
            coll.query,
            query_embeddings=[emb],
            where=where,
            n_results=min(top_k, existing),
        )
        return self._parse_query_response(result)

    async def query_text(
        self,
        collection: str,
        *,
        text: str,
        where: Optional[dict[str, Any]] = None,
        top_k: int = 20,
    ) -> list[VectorHit]:
        """Convenience: embed `text` via the injected embedder, then
        query. Equivalent to embedder.embed(text) + self.query(...)
        but with one fewer round-trip in the call site."""
        embedding = await asyncio.to_thread(self._embedder.embed, text)
        return await self.query(collection, embedding=embedding, where=where, top_k=top_k)

    async def delete(self, collection: str, ids: list[str]) -> None:
        if not ids:
            return
        coll = self._get_collection(collection)
        await asyncio.to_thread(coll.delete, ids=ids)

    async def count(self, collection: str) -> int:
        coll = self._get_collection(collection)
        return int(await asyncio.to_thread(coll.count))


# ---- Module-level singleton ------------------------------------------

_default_store: Optional[ChromaVectorStore] = None


def get_vector_store() -> VectorStore:
    """Return the process-wide default VectorStore (Chroma-backed),
    creating on first call. Tests that want isolation should construct
    their own ChromaVectorStore with an explicit `path`."""
    global _default_store
    if _default_store is None:
        _default_store = ChromaVectorStore()
    return _default_store


def reset_vector_store() -> None:
    """Drop the cached singleton. Tests use this between cases when
    they want a fresh client (e.g. after pointing settings.chroma_path
    at a different tmp dir)."""
    global _default_store
    _default_store = None


__all__ = [
    "VectorStore",
    "VectorHit",
    "ChromaVectorStore",
    "get_vector_store",
    "reset_vector_store",
    "COLLECTION_CHUNKS",
    "COLLECTION_TOPICS",
    "COLLECTION_ENTITIES",
    "ALL_COLLECTIONS",
]
