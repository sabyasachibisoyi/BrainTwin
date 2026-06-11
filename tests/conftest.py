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
