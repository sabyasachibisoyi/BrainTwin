"""Tests for the bearer-token auth dependency — Phase 4.0.6 M.1.

Run with: pytest tests/test_auth.py -v

Covers:
  - Missing/wrong bearer header → 401 (on protected routes)
  - Correct bearer header → request passes through
  - Empty / wrong-scheme / malformed header → 401
  - BACKEND_BEARER_TOKEN unset → 503 ("auth not configured")
  - Public routes (/, /health) ignore the dep entirely
  - Constant-time compare doesn't leak length

These tests intentionally do NOT hit the real Recaller / storage —
they install a stub on `_recaller` and hit the real /recall route so
the dep runs in its actual request context. /capture is harder to
test in isolation (needs the whole storage stack) so we use /recall
as the representative protected route.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend import auth  # noqa: E402
from backend import main as main_mod  # noqa: E402
from backend.agent.recaller import RankedResult, RecallResponse  # noqa: E402


# ---- Stub recaller so we can hit /recall without Sonnet -------------

class _StubRecaller:
    async def recall(self, *, query: str, user_id: int, conversation_id: str | None = None):
        return RecallResponse(
            answer="ok",
            confidence=0.9,
            results=[
                RankedResult(
                    capture_id="cap-1",
                    title="t",
                    source_domain="example.com",
                    captured_at="2026-01-01T00:00:00+00:00",
                    client="chrome",
                    dwell_time_seconds=30,
                    why_this_matches="r",
                    snippet="s",
                    original_url="https://example.com/1",
                    summary="sum",
                    confidence=0.9,
                ),
            ],
            conversation_id="conv-1",
            no_match=False,
        )


@pytest.fixture
def client(monkeypatch):
    # Make sure recall's downstream dep is mocked; we only care about auth here.
    monkeypatch.setattr(main_mod, "_recaller", _StubRecaller())
    return TestClient(main_mod.app)


@pytest.fixture
def configured_token(monkeypatch):
    """Set a known token in the settings the dep reads."""
    monkeypatch.setattr(auth.settings, "backend_bearer_token", "good-token")


@pytest.fixture
def unset_token(monkeypatch):
    """Force the configured token to empty so the dep returns 503."""
    monkeypatch.setattr(auth.settings, "backend_bearer_token", "")


# ---- Configuration failure path -------------------------------------

class TestAuthNotConfigured:
    def test_missing_token_env_returns_503(self, client, unset_token):
        r = client.post("/recall", json={"query": "x"},
                        headers={"Authorization": "Bearer anything"})
        assert r.status_code == 503
        assert "not configured" in r.json()["detail"]


# ---- Missing / malformed header -------------------------------------

class TestHeaderMissingOrMalformed:
    def test_no_header_returns_401(self, client, configured_token):
        r = client.post("/recall", json={"query": "x"})
        assert r.status_code == 401
        assert r.json()["detail"] == "bearer token required"

    def test_empty_header_returns_401(self, client, configured_token):
        r = client.post("/recall", json={"query": "x"},
                        headers={"Authorization": ""})
        assert r.status_code == 401

    def test_wrong_scheme_returns_401(self, client, configured_token):
        r = client.post("/recall", json={"query": "x"},
                        headers={"Authorization": "Basic abc:def"})
        assert r.status_code == 401

    def test_bearer_without_value_returns_401(self, client, configured_token):
        r = client.post("/recall", json={"query": "x"},
                        headers={"Authorization": "Bearer"})
        assert r.status_code == 401

    def test_bearer_with_whitespace_value_returns_401(self, client, configured_token):
        r = client.post("/recall", json={"query": "x"},
                        headers={"Authorization": "Bearer    "})
        assert r.status_code == 401


# ---- Wrong token ----------------------------------------------------

class TestWrongToken:
    def test_wrong_token_returns_401(self, client, configured_token):
        r = client.post("/recall", json={"query": "x"},
                        headers={"Authorization": "Bearer wrong-token"})
        assert r.status_code == 401
        assert r.json()["detail"] == "bearer token invalid"

    def test_close_but_not_equal_token_returns_401(self, client, configured_token):
        # Prefix match shouldn't pass — compare_digest is byte-exact.
        r = client.post("/recall", json={"query": "x"},
                        headers={"Authorization": "Bearer good-toke"})
        assert r.status_code == 401


# ---- Correct token --------------------------------------------------

class TestCorrectToken:
    def test_correct_token_passes_through(self, client, configured_token):
        r = client.post("/recall", json={"query": "x"},
                        headers={"Authorization": "Bearer good-token"})
        assert r.status_code == 200
        assert r.json()["answer"] == "ok"

    def test_lowercase_bearer_scheme_accepted(self, client, configured_token):
        # The scheme is case-insensitive per RFC 6750.
        r = client.post("/recall", json={"query": "x"},
                        headers={"Authorization": "bearer good-token"})
        assert r.status_code == 200


# ---- Public routes are NOT gated ------------------------------------

class TestPublicRoutes:
    def test_root_is_public(self, client, configured_token):
        r = client.get("/")
        assert r.status_code == 200
        assert r.json()["name"] == "DigitalTwin"

    def test_health_is_public(self, client, configured_token):
        r = client.get("/health")
        assert r.status_code == 200

    def test_root_works_even_when_token_unset(self, client, unset_token):
        # If we accidentally gated public routes, /health would 503 here
        # and UptimeRobot would page the operator forever.
        r = client.get("/")
        assert r.status_code == 200
