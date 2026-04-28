"""Tests for Phase 2 enrichment.

Run with: pytest tests/test_enrichment.py -v

These tests stub the Anthropic SDK so they can run offline. They cover:
  - schema validation (good/bad shapes, fallback behavior)
  - empty / oversized content skip cases
  - JSON-malformed retry path inside enrich()
  - retry policy in the worker (transient → success, transient → exhausted,
    permanent → no retry, malformed → no retry, schema error → no retry)
  - sidecar persistence: enrichments.jsonl on success,
    capture_failures.jsonl on every skip / failure
  - find_unenriched_capture_ids set logic
  - backfill test-row classifier
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.capture.processor import ProcessedContent  # noqa: E402
from backend.knowledge import enrichment as enrichment_mod  # noqa: E402
from backend.knowledge import enrichment_worker as worker_mod  # noqa: E402
from backend.knowledge.enrichment import (  # noqa: E402
    ContentTooLongError,
    EmptyContentError,
    SchemaError,
    _validate,
    enrich,
    wrap_enrichment_record,
)
from backend.knowledge.enrichment_worker import (  # noqa: E402
    enqueue_enrichment,
    find_unenriched_capture_ids,
)
from backend.knowledge.llm_client import (  # noqa: E402
    MalformedResponseError,
    PermanentLLMError,
    TransientLLMError,
)
from scripts.backfill_enrichment import is_test_row  # noqa: E402


# ---- Helpers --------------------------------------------------------

def make_processed(
    *,
    clean_text: str = "Some article body.",
    title: str = "An Article",
    url: str = "https://example.com/x",
    transcript: str | None = None,
    image_text: str = "",
) -> ProcessedContent:
    return ProcessedContent(
        url=url,
        title=title,
        platform="general",
        content_type="article",
        clean_text=clean_text,
        text_source="extension",
        transcript=transcript,
        image_descriptions=[],
        image_text=image_text,
        timestamp="2026-04-27T12:00:00+00:00",
        dwell_time_seconds=42,
        metadata={},
    )


class StubLLMClient:
    """Drop-in replacement for LLMClient. `responses` is a list of
    things to return or raise on successive `enrich()` calls. A dict =
    return; an exception class/instance = raise."""

    def __init__(self, responses: list[Any]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def enrich(self, *, text: str, system_prompt: str, user_prompt: str, max_tokens: int = 1024):
        self.calls.append({
            "text": text,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "max_tokens": max_tokens,
        })
        if not self._responses:
            raise AssertionError("StubLLMClient ran out of responses")
        nxt = self._responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        if isinstance(nxt, type) and issubclass(nxt, BaseException):
            raise nxt("stub error")
        return nxt

    async def aclose(self) -> None:
        return None


GOOD_RESPONSE = {
    "summary": "A short summary.",
    "entities": [{"name": "Bengaluru", "type": "place"}],
    "key_facts": ["fact one", "fact two"],
    "topics": ["topic-a", "topic-b"],
}


# ---- _validate ------------------------------------------------------

class TestValidate:
    def test_accepts_well_formed(self):
        out = _validate(dict(GOOD_RESPONSE))
        assert out["summary"] == "A short summary."
        assert out["entities"] == [{"name": "Bengaluru", "type": "place"}]
        assert out["key_facts"] == ["fact one", "fact two"]
        assert out["topics"] == ["topic-a", "topic-b"]

    def test_missing_field_raises(self):
        bad = {k: v for k, v in GOOD_RESPONSE.items() if k != "summary"}
        with pytest.raises(SchemaError, match="missing"):
            _validate(bad)

    def test_summary_must_be_string(self):
        bad = {**GOOD_RESPONSE, "summary": 42}
        with pytest.raises(SchemaError, match="summary"):
            _validate(bad)

    def test_entities_unknown_type_falls_back_to_event(self):
        out = _validate({
            **GOOD_RESPONSE,
            "entities": [{"name": "Acme", "type": "wat"}],
        })
        assert out["entities"] == [{"name": "Acme", "type": "event"}]

    def test_entities_drops_malformed_entries(self):
        out = _validate({
            **GOOD_RESPONSE,
            "entities": [
                {"name": "OK", "type": "org"},
                "not a dict",
                {"type": "person"},               # missing name
                {"name": "", "type": "person"},   # empty name
            ],
        })
        assert out["entities"] == [{"name": "OK", "type": "org"}]

    def test_topics_lowercased(self):
        out = _validate({**GOOD_RESPONSE, "topics": ["UpperCase", "Mixed-Case"]})
        assert out["topics"] == ["uppercase", "mixed-case"]

    def test_key_facts_coerced_to_strings(self):
        out = _validate({**GOOD_RESPONSE, "key_facts": ["a", 5, 3.14, ""]})
        assert out["key_facts"] == ["a", "5", "3.14"]

    def test_entities_must_be_list(self):
        with pytest.raises(SchemaError, match="entities"):
            _validate({**GOOD_RESPONSE, "entities": "not a list"})


# ---- enrich() pure function -----------------------------------------

class TestEnrich:
    def test_empty_content_raises(self):
        client = StubLLMClient([])
        with pytest.raises(EmptyContentError):
            asyncio.run(enrich(make_processed(clean_text=""), client=client))
        assert client.calls == []

    def test_too_long_raises(self, monkeypatch):
        monkeypatch.setattr(enrichment_mod.settings, "enrichment_max_input_chars", 10)
        client = StubLLMClient([])
        with pytest.raises(ContentTooLongError):
            asyncio.run(enrich(make_processed(clean_text="x" * 100), client=client))
        assert client.calls == []

    def test_happy_path(self):
        client = StubLLMClient([GOOD_RESPONSE])
        out = asyncio.run(enrich(make_processed(), client=client))
        assert out == _validate(dict(GOOD_RESPONSE))
        assert len(client.calls) == 1

    def test_malformed_then_good_retries_once(self):
        client = StubLLMClient([MalformedResponseError("bad json"), GOOD_RESPONSE])
        out = asyncio.run(enrich(make_processed(), client=client))
        assert out == _validate(dict(GOOD_RESPONSE))
        assert len(client.calls) == 2
        # Second call's system prompt should include the retry reminder
        assert "REMINDER" in client.calls[1]["system_prompt"]

    def test_malformed_twice_propagates(self):
        client = StubLLMClient([
            MalformedResponseError("bad 1"),
            MalformedResponseError("bad 2"),
        ])
        with pytest.raises(MalformedResponseError):
            asyncio.run(enrich(make_processed(), client=client))
        assert len(client.calls) == 2


# ---- wrap_enrichment_record -----------------------------------------

class TestWrapEnrichmentRecord:
    def test_shape(self):
        rec = wrap_enrichment_record(capture_id="cid-1", enrichment=dict(GOOD_RESPONSE))
        assert rec["capture_id"] == "cid-1"
        assert rec["enrichment"] == GOOD_RESPONSE
        assert rec["related_captures"] == []  # Decision I — empty in v1
        assert "model" in rec and "enriched_at" in rec


# ---- enqueue_enrichment (worker) ------------------------------------

@pytest.fixture
def tmp_jsonl(tmp_path, monkeypatch):
    """Redirect enrichment + failure paths to the test tmpdir."""
    enrich_path = tmp_path / "enrichments.jsonl"
    failures_path = tmp_path / "capture_failures.jsonl"
    monkeypatch.setattr(worker_mod.settings, "enrichments_path", str(enrich_path))
    monkeypatch.setattr(worker_mod.settings, "capture_failures_path", str(failures_path))
    return enrich_path, failures_path


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestEnqueueEnrichment:
    def test_success_writes_enrichment_only(self, tmp_jsonl):
        enrich_path, failures_path = tmp_jsonl
        client = StubLLMClient([GOOD_RESPONSE])
        asyncio.run(enqueue_enrichment("cid-good", make_processed(), client))
        rows = _read_jsonl(enrich_path)
        assert len(rows) == 1
        assert rows[0]["capture_id"] == "cid-good"
        assert rows[0]["enrichment"]["summary"] == "A short summary."
        assert _read_jsonl(failures_path) == []

    def test_empty_content_logs_skip(self, tmp_jsonl):
        enrich_path, failures_path = tmp_jsonl
        client = StubLLMClient([])
        asyncio.run(enqueue_enrichment("cid-empty", make_processed(clean_text=""), client))
        assert _read_jsonl(enrich_path) == []
        fails = _read_jsonl(failures_path)
        assert len(fails) == 1
        assert fails[0]["phase"] == "enrichment"
        assert fails[0]["reason"] == "empty_content"
        assert client.calls == []

    def test_too_long_logs_skip(self, tmp_jsonl, monkeypatch):
        enrich_path, failures_path = tmp_jsonl
        monkeypatch.setattr(enrichment_mod.settings, "enrichment_max_input_chars", 10)
        client = StubLLMClient([])
        asyncio.run(enqueue_enrichment(
            "cid-long", make_processed(clean_text="x" * 100), client,
        ))
        fails = _read_jsonl(failures_path)
        assert len(fails) == 1
        assert "content_too_long" in fails[0]["reason"]
        assert client.calls == []

    def test_transient_then_success(self, tmp_jsonl, monkeypatch):
        # Skip the actual sleep so the test runs fast
        monkeypatch.setattr(worker_mod.asyncio, "sleep", _noop_sleep)
        enrich_path, failures_path = tmp_jsonl
        client = StubLLMClient([
            TransientLLMError("blip"),
            TransientLLMError("blip again"),
            GOOD_RESPONSE,
        ])
        asyncio.run(enqueue_enrichment("cid-transient", make_processed(), client))
        assert len(_read_jsonl(enrich_path)) == 1
        assert _read_jsonl(failures_path) == []
        assert len(client.calls) == 3

    def test_transient_exhausted_logs_failure(self, tmp_jsonl, monkeypatch):
        monkeypatch.setattr(worker_mod.asyncio, "sleep", _noop_sleep)
        enrich_path, failures_path = tmp_jsonl
        # 4 attempts (1 initial + 3 retries) all transient
        client = StubLLMClient([TransientLLMError(f"e{i}") for i in range(4)])
        asyncio.run(enqueue_enrichment("cid-x", make_processed(), client))
        assert _read_jsonl(enrich_path) == []
        fails = _read_jsonl(failures_path)
        assert len(fails) == 1
        assert "transient_exhausted" in fails[0]["reason"]
        assert len(client.calls) == 4

    def test_permanent_no_retry(self, tmp_jsonl):
        enrich_path, failures_path = tmp_jsonl
        client = StubLLMClient([PermanentLLMError("auth")])
        asyncio.run(enqueue_enrichment("cid-perm", make_processed(), client))
        fails = _read_jsonl(failures_path)
        assert len(fails) == 1
        assert "permanent" in fails[0]["reason"]
        # No retry: stub sees only one call
        assert len(client.calls) == 1

    def test_malformed_after_retry_logs_failure(self, tmp_jsonl):
        enrich_path, failures_path = tmp_jsonl
        # enrich() retries once internally, so 2 malformed = give up.
        client = StubLLMClient([
            MalformedResponseError("bad 1"),
            MalformedResponseError("bad 2"),
        ])
        asyncio.run(enqueue_enrichment("cid-mf", make_processed(), client))
        fails = _read_jsonl(failures_path)
        assert len(fails) == 1
        assert "malformed_json" in fails[0]["reason"]
        assert len(client.calls) == 2

    def test_schema_error_logs_failure(self, tmp_jsonl):
        enrich_path, failures_path = tmp_jsonl
        # Returns valid JSON dict but missing required keys → SchemaError.
        client = StubLLMClient([{"summary": "ok"}])
        asyncio.run(enqueue_enrichment("cid-schema", make_processed(), client))
        fails = _read_jsonl(failures_path)
        assert len(fails) == 1
        assert "schema" in fails[0]["reason"]


async def _noop_sleep(_seconds: float) -> None:
    return None


# ---- find_unenriched_capture_ids ------------------------------------

class TestFindUnenriched:
    def test_set_logic(self, tmp_path):
        captures = tmp_path / "captures.jsonl"
        enrichments = tmp_path / "enrichments.jsonl"
        captures.write_text(
            "\n".join([
                json.dumps({"capture_id": "a"}),
                json.dumps({"capture_id": "b"}),
                json.dumps({"capture_id": "c"}),
                json.dumps({"capture_id": "b"}),  # duplicate — should dedupe
                "{ not json",                     # ignored
                json.dumps({"no_id": True}),      # ignored
            ]) + "\n",
            encoding="utf-8",
        )
        enrichments.write_text(
            json.dumps({"capture_id": "b"}) + "\n",
            encoding="utf-8",
        )
        out = find_unenriched_capture_ids(
            captures_path=captures, enrichments_path=enrichments,
        )
        assert out == ["a", "c"]

    def test_no_files(self, tmp_path):
        out = find_unenriched_capture_ids(
            captures_path=tmp_path / "missing.jsonl",
            enrichments_path=tmp_path / "missing2.jsonl",
        )
        assert out == []


# ---- backfill is_test_row classifier --------------------------------

class TestIsTestRow:
    def test_skips_example_com(self):
        skip, _ = is_test_row({"url": "https://example.com/foo", "clean_text": "x"})
        assert skip

    def test_skips_mock_capture_metadata(self):
        skip, _ = is_test_row({
            "url": "https://realsite.com",
            "clean_text": "x",
            "metadata": {"source": "mock_capture"},
        })
        assert skip

    def test_skips_empty_telegram_link(self):
        skip, _ = is_test_row({
            "url": "https://t.me/foo",
            "title": "Telegram link",
            "clean_text": "",
            "image_text": "",
        })
        assert skip

    def test_skips_no_text_at_all(self):
        skip, _ = is_test_row({
            "url": "https://realsite.com",
            "clean_text": "",
            "transcript": None,
            "image_text": "",
        })
        assert skip

    def test_keeps_real_row(self):
        skip, _ = is_test_row({
            "url": "https://hindustantimes.com/article",
            "title": "HSR Layout rents jump",
            "clean_text": "Some real content here.",
            "metadata": {"source": "chrome_extension"},
        })
        assert not skip
