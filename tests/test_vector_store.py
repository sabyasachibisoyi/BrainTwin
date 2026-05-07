"""Tests for backend/storage/vector_store.py — Phase 3 Step 2.

Run with: pytest tests/test_vector_store.py -v

Uses a real chromadb instance pointed at a per-test tmp directory so
collections don't bleed across cases. Avoids hitting the real
data/chroma/ dir even by accident.

For tests that need an embedding (rather than a hand-crafted vector),
we use the real Embedder — same caveat as test_embedder.py: first run
downloads the model, subsequent runs reuse the cache. Most cases here
use synthetic vectors to keep them fast and not depend on the embedder.

Covered:
  - add() then query() round-trip — the basic path
  - upsert() is idempotent (same id silently overwrites)
  - where-filter on user_id correctly isolates tenants
  - cosine distance: identical vectors → distance 0
  - count() reports the right cardinality
  - delete() removes vectors
  - Multiple collections are isolated (no cross-collection bleed)
  - Empty collection returns empty list (no exception)
  - query_text() embeds via the injected embedder
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Skip whole file if chromadb isn't installed (sandbox dev env).
chromadb = pytest.importorskip("chromadb")

from backend.storage.embedder import EMBEDDING_DIM, Embedder  # noqa: E402
from backend.storage.vector_store import (  # noqa: E402
    COLLECTION_CHUNKS,
    COLLECTION_TOPICS,
    ChromaVectorStore,
    VectorHit,
)


# ---- Fixtures --------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    """Fresh ChromaVectorStore pointing at a tmp directory. Each test
    gets its own — no data leaks between cases."""
    # Use a stub embedder so tests that exercise query_text don't have
    # to load the real model. The real embedder is exercised in
    # test_embedder.py.
    return ChromaVectorStore(
        embedder=_StubEmbedder(),
        path=str(tmp_path / "chroma"),
    )


class _StubEmbedder:
    """Deterministic non-trivial embedder for tests. Maps text → vector
    based on a stable hash so the same text always yields the same
    vector, but different texts diverge."""

    def embed(self, text: str) -> np.ndarray:
        # Hash the text into a seed, use it to draw a deterministic vector.
        seed = abs(hash(text)) % (2**32)
        rng = np.random.default_rng(seed)
        vec = rng.standard_normal(EMBEDDING_DIM, dtype=np.float32)
        # Normalize so distance values are in a predictable range.
        norm = np.linalg.norm(vec)
        return (vec / norm).astype(np.float32) if norm > 0 else vec


def _vec(seed: int) -> np.ndarray:
    """Make a deterministic vector for synthetic tests."""
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(EMBEDDING_DIM, dtype=np.float32)
    return v / max(float(np.linalg.norm(v)), 1e-9)


# ---- Basic round-trip ------------------------------------------------

class TestAddAndQuery:
    def test_add_then_query_returns_added_ids(self, store):
        async def go():
            await store.add(
                COLLECTION_CHUNKS,
                ids=["c1", "c2", "c3"],
                embeddings=[_vec(1), _vec(2), _vec(3)],
                metadatas=[
                    {"user_id": 1, "source_kind": "article_paragraph"},
                    {"user_id": 1, "source_kind": "article_paragraph"},
                    {"user_id": 1, "source_kind": "transcript_segment"},
                ],
                documents=["first chunk", "second chunk", "third chunk"],
            )
            hits = await store.query(
                COLLECTION_CHUNKS,
                embedding=_vec(1),  # identical to c1
                top_k=10,
            )
            return hits

        hits = asyncio.run(go())
        assert len(hits) == 3
        # The vector identical to what was added at id=c1 should rank first.
        assert hits[0].id == "c1"
        # Distance ≈ 0 for identical vectors (cosine space).
        assert hits[0].distance < 1e-4
        # Hits are VectorHit dataclasses with metadata + document populated
        assert isinstance(hits[0], VectorHit)
        assert hits[0].metadata["user_id"] == 1
        assert hits[0].document == "first chunk"

    def test_query_empty_collection_returns_empty(self, store):
        async def go():
            return await store.query(
                COLLECTION_CHUNKS,
                embedding=_vec(1),
                top_k=10,
            )
        assert asyncio.run(go()) == []

    def test_query_text_uses_embedder(self, store):
        async def go():
            # Get the embedder's vector for "hello" and add it as a chunk
            stub_vec = _StubEmbedder().embed("hello")
            await store.add(
                COLLECTION_CHUNKS,
                ids=["hello-chunk"],
                embeddings=[stub_vec],
                metadatas=[{"user_id": 1}],
                documents=["hello"],
            )
            # query_text("hello") should re-embed "hello" identically
            # and find that chunk at the top.
            hits = await store.query_text(
                COLLECTION_CHUNKS, text="hello", top_k=5,
            )
            return hits

        hits = asyncio.run(go())
        assert len(hits) >= 1
        assert hits[0].id == "hello-chunk"
        assert hits[0].distance < 1e-4


# ---- Tenant isolation -----------------------------------------------

class TestWhereFilter:
    def test_user_id_filter_isolates_tenants(self, store):
        async def go():
            # Same vector for both — distinguished only by user_id metadata.
            same_vec = _vec(42)
            await store.add(
                COLLECTION_CHUNKS,
                ids=["sabya-1", "alice-1"],
                embeddings=[same_vec, same_vec],
                metadatas=[{"user_id": 1}, {"user_id": 2}],
                documents=["sabya doc", "alice doc"],
            )
            sabya_hits = await store.query(
                COLLECTION_CHUNKS,
                embedding=same_vec,
                where={"user_id": 1},
                top_k=10,
            )
            alice_hits = await store.query(
                COLLECTION_CHUNKS,
                embedding=same_vec,
                where={"user_id": 2},
                top_k=10,
            )
            return sabya_hits, alice_hits

        sabya, alice = asyncio.run(go())
        # Each tenant sees ONLY their own row, even though both have
        # the identical embedding (the metadata filter does the work).
        assert [h.id for h in sabya] == ["sabya-1"]
        assert [h.id for h in alice] == ["alice-1"]


# ---- upsert is idempotent --------------------------------------------

class TestUpsert:
    def test_upsert_overwrites_same_id(self, store):
        async def go():
            await store.upsert(
                COLLECTION_CHUNKS,
                ids=["dup"],
                embeddings=[_vec(1)],
                metadatas=[{"user_id": 1, "version": "v1"}],
                documents=["first version"],
            )
            # Same id, different metadata + document — should overwrite.
            await store.upsert(
                COLLECTION_CHUNKS,
                ids=["dup"],
                embeddings=[_vec(1)],
                metadatas=[{"user_id": 1, "version": "v2"}],
                documents=["second version"],
            )
            hits = await store.query(
                COLLECTION_CHUNKS,
                embedding=_vec(1),
                top_k=10,
            )
            count = await store.count(COLLECTION_CHUNKS)
            return hits, count

        hits, count = asyncio.run(go())
        # Still only one row, but with v2 content.
        assert count == 1
        assert hits[0].id == "dup"
        assert hits[0].metadata["version"] == "v2"
        assert hits[0].document == "second version"


# ---- count + delete --------------------------------------------------

class TestCountAndDelete:
    def test_count_reflects_inserts_and_deletes(self, store):
        async def go():
            await store.add(
                COLLECTION_CHUNKS,
                ids=["a", "b", "c"],
                embeddings=[_vec(1), _vec(2), _vec(3)],
                metadatas=[{"user_id": 1}] * 3,
            )
            count1 = await store.count(COLLECTION_CHUNKS)
            await store.delete(COLLECTION_CHUNKS, ids=["b"])
            count2 = await store.count(COLLECTION_CHUNKS)
            return count1, count2

        c1, c2 = asyncio.run(go())
        assert c1 == 3
        assert c2 == 2

    def test_delete_empty_list_is_noop(self, store):
        async def go():
            await store.delete(COLLECTION_CHUNKS, ids=[])
            return await store.count(COLLECTION_CHUNKS)

        assert asyncio.run(go()) == 0


# ---- Multiple collections are isolated -------------------------------

class TestMultipleCollections:
    def test_collections_dont_cross_pollinate(self, store):
        async def go():
            await store.add(
                COLLECTION_CHUNKS,
                ids=["chunk-1"],
                embeddings=[_vec(1)],
                metadatas=[{"user_id": 1}],
            )
            # Note: ChromaDB rejects empty metadata dicts; pass a real
            # field. In production topics and entities will carry at
            # least their label/slug as metadata for inspectability,
            # so this matches actual usage.
            await store.add(
                COLLECTION_TOPICS,
                ids=["kanban"],
                embeddings=[_vec(1)],   # SAME vector — different collection
                metadatas=[{"label": "Kanban"}],
            )
            # Querying chunks must NOT return the topic, and vice versa.
            chunk_hits = await store.query(
                COLLECTION_CHUNKS, embedding=_vec(1), top_k=10,
            )
            topic_hits = await store.query(
                COLLECTION_TOPICS, embedding=_vec(1), top_k=10,
            )
            return chunk_hits, topic_hits

        chunk_hits, topic_hits = asyncio.run(go())
        assert [h.id for h in chunk_hits] == ["chunk-1"]
        assert [h.id for h in topic_hits] == ["kanban"]


# ---- Empty / edge cases ---------------------------------------------

class TestEdgeCases:
    def test_add_empty_lists_is_noop(self, store):
        async def go():
            await store.add(
                COLLECTION_CHUNKS,
                ids=[],
                embeddings=[],
                metadatas=[],
            )
            return await store.count(COLLECTION_CHUNKS)
        assert asyncio.run(go()) == 0

    def test_top_k_larger_than_collection(self, store):
        """Asking for top 100 when only 2 exist returns those 2 without
        raising. We clamp internally to avoid Chroma's behaviour around
        n_results > collection size in some versions."""
        async def go():
            await store.add(
                COLLECTION_CHUNKS,
                ids=["a", "b"],
                embeddings=[_vec(1), _vec(2)],
                metadatas=[{"user_id": 1}, {"user_id": 1}],
            )
            return await store.query(
                COLLECTION_CHUNKS,
                embedding=_vec(1),
                top_k=100,
            )

        hits = asyncio.run(go())
        assert len(hits) == 2
