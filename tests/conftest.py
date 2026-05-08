"""Project-wide pytest configuration.

Sets DATABASE_URL to an in-memory SQLite *before* any test module
imports `backend.config.settings`. Without this, the first test file
imported during collection wins — if it didn't set DATABASE_URL, the
Pydantic settings singleton caches the default (`sqlite:///./data/
braintwin.db`, the real local DB), and subsequent tests pollute /
collide with whatever rows already live there.

conftest.py runs before test modules in pytest's collection order,
which is what makes this work.
"""

from __future__ import annotations

import os

# Point the storage layer at an in-memory SQLite for ALL tests by
# default. Tests that need a file-backed DB (e.g. the dual-write
# integration test) monkeypatch `db_module.settings.database_url`
# inside their fixtures.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
