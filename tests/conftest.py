"""Project-wide pytest configuration.

Sets DATABASE_URL to an in-memory SQLite *before* any test module
imports `backend.config.settings`. Without this, the first test file
imported during collection wins — if it didn't set DATABASE_URL, the
Pydantic settings singleton caches the default (`sqlite:///./data/
braintwin.db`, the real local DB), and subsequent tests pollute /
collide with whatever rows already live there.

conftest.py runs before test modules in pytest's collection order,
which is what makes this work.

Also sets a known bearer token (Phase 4.0.6 M.1) so the auth dep
doesn't 503 every existing endpoint test. Tests that specifically
exercise auth failure paths override this via the FastAPI app's
dependency_overrides — see tests/test_auth.py.
"""

from __future__ import annotations

import os

# Point the storage layer at an in-memory SQLite for ALL tests by
# default. Tests that need a file-backed DB (e.g. the dual-write
# integration test) monkeypatch `db_module.settings.database_url`
# inside their fixtures.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

# M.1: a known bearer token for the test process. Real value is
# irrelevant — tests will inject the same string into Authorization
# headers OR override the auth dep entirely.
os.environ.setdefault("BACKEND_BEARER_TOKEN", "test-bearer-token")


# ---- Fast pre-commit subset (BRAINTWIN_FAST_TESTS=1) ------------------
# The pre-commit git hook (scripts/git-hooks/pre-commit) runs the suite
# with BRAINTWIN_FAST_TESTS=1 to keep every commit snappy. The wall-clock
# cost of the full suite (~60s) is dominated by *importing* torch /
# chromadb / faster-whisper — and pytest imports a test module at
# COLLECTION time, before any `-m "not slow"` marker filtering applies.
# So a marker alone wouldn't dodge the import cost. `collect_ignore` does:
# it tells pytest not to even import these files, so the heavy libraries
# are never loaded in fast mode.
#
# This list is the single source of truth for "slow" test files. When a
# new test imports torch/chromadb/whisper (or backend.storage.embedder /
# vector_store / capture.video_transcriber, which pull them in), add it
# here. The full suite still runs everything — fast mode just skips these.
_SLOW_TEST_FILES = [
    "test_embedder.py",         # sentence-transformers / torch
    "test_vector_store.py",     # chromadb (+ embedder)
    "test_video_transcriber.py",  # faster-whisper
    "test_retrieval_service.py",  # embedder + vector_store
    "test_storage_sync.py",     # embedder + vector_store (chroma mirror)
    "test_enrichment.py",       # pulls video_transcriber
]

if os.environ.get("BRAINTWIN_FAST_TESTS") == "1":
    collect_ignore = list(_SLOW_TEST_FILES)
