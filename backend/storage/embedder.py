"""Embedding generation — wraps `sentence-transformers/all-MiniLM-L6-v2`.

Per docs/phase3-design.md A.6:
  - 384-dim float32 vectors
  - English-leaning (good enough for use cases A and B; C's reasoning
    happens at Claude, not the embedding layer)
  - Free, local, no per-request cost, no vendor lock-in
  - Future upgrade path is BAAI/bge-m3 (multilingual) or Voyage AI

The model loader is **lazy** — it doesn't run on import. First call to
`embed()` / `embed_many()` triggers a one-time download (~80 MB on
first run, cached locally afterward) and a few seconds of model
warmup. That keeps app startup fast even when the embedder is never
exercised in a given process.

Two storage forms supported:
  - `np.ndarray` — canonical in-memory form. Used by VectorStore.add(),
    in-memory similarity computations, and as the immediate output of
    embed().
  - `bytes` — serialized float32. Used for the BLOB columns on chunks /
    topics / entities (per A.4) and the eventual pgvector migration
    (where the column type becomes VECTOR(384)).

Codec rule: always round-trip through float32 little-endian (numpy's
default on x86 / ARM). Cross-platform consistent. 1536 bytes per vector.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


logger = logging.getLogger(__name__)


# Default model per A.6. Override-able via constructor for tests or
# future swaps; settings.enrichment_model points at the LLM, not the
# embedder, so we don't pull from there.
DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384
EMBEDDING_BYTES = EMBEDDING_DIM * 4   # float32 = 4 bytes per dimension


class Embedder:
    """Lazy-loaded sentence-transformers wrapper.

    Construct once at app startup; share across the process. Thread-safe
    enough for our use case — sentence-transformers' encode() is
    GIL-bound but releases the GIL during the underlying torch matmul,
    so concurrent calls from asyncio's executor work without deadlock.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL):
        self._model_name = model_name
        self._model: "SentenceTransformer | None" = None

    # ---- Lazy model loading ------------------------------------------

    def _ensure_loaded(self) -> "SentenceTransformer":
        """Load the model on first use. Imports happen here, not at
        module import, so test environments without sentence-transformers
        installed don't blow up just by importing this file."""
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            logger.info("Loading embedding model: %s", self._model_name)
            self._model = SentenceTransformer(self._model_name)
            logger.info("Embedding model loaded.")
        return self._model

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dim(self) -> int:
        return EMBEDDING_DIM

    # ---- Embedding generation ----------------------------------------

    def embed(self, text: str) -> np.ndarray:
        """Embed a single string. Returns a 1-D float32 array of length
        EMBEDDING_DIM. Empty / whitespace-only text gets a zero vector
        — caller can decide to skip."""
        if not text or not text.strip():
            return np.zeros(EMBEDDING_DIM, dtype=np.float32)
        model = self._ensure_loaded()
        # convert_to_numpy=True returns a numpy array (not a torch
        # tensor). normalize_embeddings=False — we don't normalize at
        # encode time because:
        #   1. Chroma's hnsw:cosine handles normalization internally
        #      for distance computation.
        #   2. Storing un-normalized preserves the original vector;
        #      caller can renormalize later if needed.
        vec = model.encode(
            text,
            convert_to_numpy=True,
            normalize_embeddings=False,
            show_progress_bar=False,
        )
        return vec.astype(np.float32)

    def embed_many(
        self,
        texts: list[str],
        *,
        batch_size: int = 32,
    ) -> list[np.ndarray]:
        """Embed a batch of strings. Returns a list of 1-D float32
        arrays, one per input, in input order. Empty strings get zero
        vectors (matching `embed()` semantics).

        Batching saves wall-clock time substantially — ~3-5x faster
        than calling embed() per string for typical chunk counts.
        """
        if not texts:
            return []
        model = self._ensure_loaded()
        # Mark empty strings so we can substitute zero vectors after.
        # sentence-transformers handles empty input but we want the
        # same semantics as embed().
        substitutes = [(i, t) for i, t in enumerate(texts) if t and t.strip()]
        if not substitutes:
            return [np.zeros(EMBEDDING_DIM, dtype=np.float32) for _ in texts]
        indices, real_texts = zip(*substitutes)
        matrix = model.encode(
            list(real_texts),
            batch_size=batch_size,
            convert_to_numpy=True,
            normalize_embeddings=False,
            show_progress_bar=False,
        ).astype(np.float32)
        out: list[np.ndarray] = [
            np.zeros(EMBEDDING_DIM, dtype=np.float32) for _ in texts
        ]
        for k, idx in enumerate(indices):
            out[idx] = matrix[k]
        return out

    # ---- Codec: numpy <-> BLOB ---------------------------------------

    @staticmethod
    def to_bytes(arr: np.ndarray) -> bytes:
        """Serialize a float32 array to bytes for BLOB storage. Always
        casts to float32 first so a caller passing float64 by accident
        doesn't double the storage cost or break the round trip."""
        return arr.astype(np.float32).tobytes()

    @staticmethod
    def from_bytes(b: bytes) -> np.ndarray:
        """Deserialize a BLOB back into a float32 array. Returns a
        read-only view onto the bytes — copy if you need to mutate."""
        if not b:
            return np.zeros(EMBEDDING_DIM, dtype=np.float32)
        return np.frombuffer(b, dtype=np.float32)


# Singleton — most callers should use this one rather than constructing
# their own. Lazy: doesn't load until embed() / embed_many() is called.
_default_embedder: Optional[Embedder] = None


def get_embedder() -> Embedder:
    """Return the process-wide default Embedder, creating on first call.
    Tests that want isolation should construct their own Embedder()
    rather than using this."""
    global _default_embedder
    if _default_embedder is None:
        _default_embedder = Embedder()
    return _default_embedder


__all__ = [
    "Embedder",
    "DEFAULT_MODEL",
    "EMBEDDING_DIM",
    "EMBEDDING_BYTES",
    "get_embedder",
]
