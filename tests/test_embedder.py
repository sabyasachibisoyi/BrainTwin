"""Tests for backend/storage/embedder.py — Phase 3 Step 2.

Run with: pytest tests/test_embedder.py -v

Uses the real sentence-transformers model (all-MiniLM-L6-v2). The first
test triggers the ~80 MB download + a few seconds of model warmup;
subsequent tests reuse the cached model. The whole file should run in
under 30 seconds on a warm cache.

If sentence-transformers isn't installed in the test environment, the
file is skipped wholesale rather than failing — keeps the CI signal
clean for deployments that don't ship the embedder dependency.

Covered:
  - Single embed produces a 384-dim float32 array
  - embed_many gives same vectors as repeated embed() (modulo numerics)
  - Empty / whitespace strings produce zero vectors
  - Same input → same output (deterministic)
  - Different inputs → different output
  - to_bytes / from_bytes round-trip preserves the vector exactly
  - Bytes form is exactly EMBEDDING_BYTES long
  - Module-level get_embedder() returns the same instance
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Skip the whole file if optional embedder deps are unavailable or
# partially broken in this environment (e.g. sentence-transformers pulls
# torch, which fails during import on some Python/CPU builds).
try:
    import sentence_transformers  # noqa: F401
except Exception as exc:  # pragma: no cover - environment-specific
    pytest.skip(
        f"Skipping embedder tests; sentence-transformers stack unavailable: {exc}",
        allow_module_level=True,
    )

from backend.storage.embedder import (  # noqa: E402
    DEFAULT_MODEL,
    EMBEDDING_BYTES,
    EMBEDDING_DIM,
    Embedder,
    get_embedder,
)
import backend.storage.embedder as embedder_mod  # noqa: E402


# ---- Module-level fixtures ------------------------------------------

@pytest.fixture(scope="module")
def shared_embedder():
    """Single Embedder shared across this module's tests so the model
    only downloads/loads once. Tests that mutate state (none currently)
    should construct their own instance."""
    return Embedder()


# ---- Single-text embedding -------------------------------------------

class TestEmbed:
    def test_single_returns_384d_float32(self, shared_embedder):
        vec = shared_embedder.embed("Hello world.")
        assert isinstance(vec, np.ndarray)
        assert vec.shape == (EMBEDDING_DIM,)
        assert vec.dtype == np.float32

    def test_empty_string_returns_zero_vector(self, shared_embedder):
        for empty in ["", "   ", "\n\t\n"]:
            vec = shared_embedder.embed(empty)
            assert vec.shape == (EMBEDDING_DIM,)
            assert vec.dtype == np.float32
            assert np.all(vec == 0.0)

    def test_deterministic(self, shared_embedder):
        a = shared_embedder.embed("The quick brown fox.")
        b = shared_embedder.embed("The quick brown fox.")
        # Same text -> identical vector. (Stronger than np.allclose;
        # all-MiniLM is fully deterministic for the same input.)
        np.testing.assert_array_equal(a, b)

    def test_different_inputs_give_different_outputs(self, shared_embedder):
        a = shared_embedder.embed("A capture about kanban methodology.")
        b = shared_embedder.embed("A capture about hash functions.")
        # Cosine distance > 0 — the two should not collide
        cos_sim = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
        assert cos_sim < 0.99


# ---- Batched embedding -----------------------------------------------

class TestEmbedMany:
    def test_returns_one_vector_per_input(self, shared_embedder):
        texts = ["First.", "Second.", "Third one."]
        vectors = shared_embedder.embed_many(texts)
        assert len(vectors) == len(texts)
        for v in vectors:
            assert v.shape == (EMBEDDING_DIM,)
            assert v.dtype == np.float32

    def test_empty_input_returns_empty_list(self, shared_embedder):
        assert shared_embedder.embed_many([]) == []

    def test_batch_matches_single_call(self, shared_embedder):
        """A vector produced via embed_many should equal (within float
        tolerance) the same vector produced via embed() — that's the
        guarantee the controlled-vocabulary flow in B.7 relies on, since
        topics are looked up via embedding similarity regardless of
        whether they were embedded in a batch or one at a time."""
        text = "Verification cost reduction technology."
        single = shared_embedder.embed(text)
        batched = shared_embedder.embed_many([text])[0]
        np.testing.assert_allclose(single, batched, rtol=1e-5, atol=1e-5)

    def test_empty_strings_in_batch_get_zero_vectors(self, shared_embedder):
        out = shared_embedder.embed_many(["", "real text", "  "])
        assert np.all(out[0] == 0.0)
        assert not np.all(out[1] == 0.0)  # real text → non-zero
        assert np.all(out[2] == 0.0)


# ---- Codec: bytes round-trip -----------------------------------------

class TestCodec:
    def test_to_bytes_length(self, shared_embedder):
        arr = shared_embedder.embed("anything")
        b = Embedder.to_bytes(arr)
        # 384 dims * 4 bytes per float32 = 1536
        assert len(b) == EMBEDDING_BYTES

    def test_round_trip_preserves_vector(self, shared_embedder):
        original = shared_embedder.embed("Round-trip me.")
        b = Embedder.to_bytes(original)
        recovered = Embedder.from_bytes(b)
        np.testing.assert_array_equal(original, recovered)

    def test_from_bytes_on_empty_returns_zero_vector(self):
        out = Embedder.from_bytes(b"")
        assert out.shape == (EMBEDDING_DIM,)
        assert np.all(out == 0.0)

    def test_to_bytes_casts_float64_to_float32(self):
        # If a caller hands us float64 by accident, we should still
        # produce the same EMBEDDING_BYTES output and round-trip cleanly
        # (with float32 precision).
        arr64 = np.ones(EMBEDDING_DIM, dtype=np.float64)
        b = Embedder.to_bytes(arr64)
        assert len(b) == EMBEDDING_BYTES
        recovered = Embedder.from_bytes(b)
        assert recovered.dtype == np.float32
        np.testing.assert_allclose(recovered, np.ones(EMBEDDING_DIM, dtype=np.float32))


# ---- Module-level singleton ------------------------------------------

class TestGetEmbedder:
    def test_singleton(self, monkeypatch):
        # Reset any cached singleton first so this test is hermetic.
        monkeypatch.setattr(embedder_mod, "_default_embedder", None)
        a = get_embedder()
        b = get_embedder()
        assert a is b
        assert a.model_name == DEFAULT_MODEL

    def test_lazy_load(self, monkeypatch):
        """Constructor must NOT load the model. is_loaded only becomes
        True after embed() / embed_many() is called."""
        monkeypatch.setattr(embedder_mod, "_default_embedder", None)
        emb = Embedder()
        assert emb.is_loaded is False
        emb.embed("trigger load")
        assert emb.is_loaded is True
