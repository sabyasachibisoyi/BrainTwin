"""Tests for Phase 4 M.4 — POST /recall endpoint.

Run with: pytest tests/test_recall_endpoint.py -v

Covers:
  - Happy path: POST /recall returns the RecallResponse.to_dict() shape
    with all S.3 fields present and well-typed.
  - conversation_id round-trip (request body → recaller → response).
  - no_match=True is returned cleanly when the Recaller says so.
  - 503 when the Recaller is not initialized (missing API key) — the
    rest of the app keeps working.
  - 422 on missing `query` (Pydantic validation kicks in).
  - The user_id forwarded to the Recaller is DEFAULT_USER_ID (B.5.4).

We monkey-patch the module-level `_recaller` singleton to a stub so
nothing reaches the real Anthropic SDK or the storage layer. The
startup hook isn't triggered by TestClient by default; we set
`_recaller` directly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi.testclient import TestClient  # noqa: E402

from backend import main as main_mod  # noqa: E402
from backend.agent.recaller import RankedResult, RecallResponse  # noqa: E402
from backend.auth import require_bearer_token  # noqa: E402
from backend.storage import DEFAULT_USER_ID  # noqa: E402


# ---- Stub Recaller --------------------------------------------------

class _StubRecaller:
    """Records calls and returns a preconfigured RecallResponse. Lets
    the test exercise the HTTP layer without touching Sonnet, Chroma,
    or SQL."""

    def __init__(self, response: RecallResponse):
        self._response = response
        self.calls: list[dict] = []

    async def recall(
        self, *, query: str, user_id: int, conversation_id: str | None = None,
    ) -> RecallResponse:
        self.calls.append({
            "query": query,
            "user_id": user_id,
            "conversation_id": conversation_id,
        })
        return self._response


def _make_response(
    *,
    answer: str = "I think you're remembering capture cap-a.",
    confidence: float = 0.82,
    conversation_id: str = "conv-1",
    no_match: bool = False,
    n_results: int = 1,
) -> RecallResponse:
    results = [
        RankedResult(
            capture_id=f"cap-{i}",
            title=f"Article {i}",
            source_domain="example.com",
            captured_at="2026-05-08T10:00:00+00:00",
            client="chrome",
            dwell_time_seconds=42,
            why_this_matches=f"reason {i}",
            snippet=f"matching snippet {i}",
            original_url=f"https://example.com/{i}",
            summary=f"summary {i}",
            confidence=max(0.1, confidence - 0.1 * i),
        )
        for i in range(n_results)
    ]
    return RecallResponse(
        answer=answer,
        confidence=confidence,
        results=results,
        conversation_id=conversation_id,
        no_match=no_match,
    )


# ---- Fixtures -------------------------------------------------------

@pytest.fixture
def client():
    """FastAPI TestClient — uses the real `app` but tests inject the
    Recaller via monkeypatch.

    Phase 4.0.6 M.1: bypass the bearer-token dep so these tests stay
    focused on recall behavior. Auth itself is covered in
    tests/test_auth.py.
    """
    main_mod.app.dependency_overrides[require_bearer_token] = lambda: None
    try:
        yield TestClient(main_mod.app)
    finally:
        main_mod.app.dependency_overrides.pop(require_bearer_token, None)


@pytest.fixture
def install_recaller(monkeypatch):
    """Replace the module-level _recaller with a stub. Returns a
    function the test calls with the response shape it wants."""
    def _install(response: RecallResponse) -> _StubRecaller:
        stub = _StubRecaller(response)
        monkeypatch.setattr(main_mod, "_recaller", stub)
        return stub
    return _install


@pytest.fixture
def no_recaller(monkeypatch):
    """Force _recaller to None (simulating missing ANTHROPIC_API_KEY)
    so /recall returns 503."""
    monkeypatch.setattr(main_mod, "_recaller", None)


# ---- Happy path -----------------------------------------------------

class TestRecallEndpointHappyPath:
    def test_returns_recall_response_shape(self, client, install_recaller):
        stub = install_recaller(_make_response(n_results=2))

        r = client.post("/recall", json={"query": "kanban article"})

        assert r.status_code == 200
        body = r.json()
        # S.3 fields all present.
        assert set(body.keys()) == {
            "answer", "confidence", "results", "conversation_id", "no_match",
        }
        # Recaller was called with the user's query + default user.
        assert len(stub.calls) == 1
        assert stub.calls[0]["query"] == "kanban article"
        assert stub.calls[0]["user_id"] == DEFAULT_USER_ID
        assert stub.calls[0]["conversation_id"] is None

    def test_result_blocks_have_u2_fields(self, client, install_recaller):
        install_recaller(_make_response(n_results=1))

        r = client.post("/recall", json={"query": "x"})
        body = r.json()
        block = body["results"][0]

        # Every U.2 field is serialised.
        for field in [
            "capture_id", "title", "source_domain", "captured_at", "client",
            "dwell_time_seconds", "why_this_matches", "snippet",
            "original_url", "summary", "confidence",
        ]:
            assert field in block, f"missing field {field!r}"

        # Types are reasonable for JSON consumers.
        assert isinstance(block["dwell_time_seconds"], int)
        assert isinstance(block["confidence"], (int, float))
        assert 0.0 <= block["confidence"] <= 1.0


# ---- conversation_id round-trip ------------------------------------

class TestConversationIdRoundTrip:
    def test_conversation_id_forwarded_to_recaller(self, client, install_recaller):
        """Body conversation_id arrives at Recaller.recall and the
        response carries it back."""
        stub = install_recaller(_make_response(conversation_id="conv-given"))

        r = client.post("/recall", json={
            "query": "follow up", "conversation_id": "conv-given",
        })
        assert r.status_code == 200
        assert stub.calls[0]["conversation_id"] == "conv-given"
        assert r.json()["conversation_id"] == "conv-given"

    def test_missing_conversation_id_lets_recaller_mint_one(
        self, client, install_recaller,
    ):
        """No conversation_id in the body → Recaller is called with
        None and mints its own. The response surfaces that uuid."""
        stub = install_recaller(_make_response(conversation_id="conv-new"))

        r = client.post("/recall", json={"query": "first turn"})
        assert r.status_code == 200
        assert stub.calls[0]["conversation_id"] is None
        # Whatever the Recaller returned is what the client sees.
        assert r.json()["conversation_id"] == "conv-new"


# ---- no_match shape -------------------------------------------------

class TestNoMatchShape:
    def test_no_match_response_passes_through(self, client, install_recaller):
        install_recaller(_make_response(
            answer="I don't think this is in your corpus.",
            confidence=0.3,
            no_match=True,
            n_results=1,
        ))

        r = client.post("/recall", json={"query": "nothing matches"})
        body = r.json()
        assert r.status_code == 200
        assert body["no_match"] is True
        # Closest-miss result still in the list per U.4.
        assert len(body["results"]) == 1


# ---- Recaller not initialized → 503 --------------------------------

class TestRecallerUnavailable:
    def test_no_recaller_returns_503(self, client, no_recaller):
        r = client.post("/recall", json={"query": "anything"})
        assert r.status_code == 503
        detail = r.json().get("detail", "")
        # The error message points the operator at the fix.
        assert "ANTHROPIC_API_KEY" in detail


# ---- Pydantic validation -------------------------------------------

class TestRequestValidation:
    def test_missing_query_returns_422(self, client, install_recaller):
        # We install a stub so the test failure is "validation should
        # reject this" not "recaller is None".
        install_recaller(_make_response())
        r = client.post("/recall", json={"conversation_id": "x"})
        assert r.status_code == 422

    def test_empty_body_returns_422(self, client, install_recaller):
        install_recaller(_make_response())
        r = client.post("/recall", json={})
        assert r.status_code == 422

    def test_empty_query_string_reaches_recaller(self, client, install_recaller):
        """An empty `query=""` is technically valid Pydantic input —
        the Recaller is responsible for the empty-query no_match
        response, not FastAPI."""
        stub = install_recaller(_make_response(
            answer="Please type something.", no_match=True, n_results=0,
        ))
        r = client.post("/recall", json={"query": ""})
        assert r.status_code == 200
        assert stub.calls[0]["query"] == ""
        body = r.json()
        assert body["no_match"] is True
        assert body["results"] == []


# ---- The /ask placeholder is gone ----------------------------------

class TestAskPlaceholderRemoved:
    def test_ask_endpoint_no_longer_routed(self, client):
        """`/ask` was the Phase 1 placeholder; M.4 replaced it with
        `/recall`. The route should be 404 now so callers update."""
        r = client.post("/ask", json={"question": "anything"})
        assert r.status_code == 404
