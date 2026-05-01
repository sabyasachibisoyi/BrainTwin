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
    """Redirect enrichment + failure + hydration paths to the test tmpdir.

    Also disables OG fetching AND video transcription by default — Phase
    2.5 Fixes 2 + 3 added pre-enrich hydration calls, but the bulk of
    these tests assume a deterministic worker that doesn't reach for
    the network. Tests that exercise hydration explicitly opt back in
    by monkeypatching `og_fetch_enabled` / `video_transcribe_enabled`
    and the matching injection points on the hydration module."""
    enrich_path = tmp_path / "enrichments.jsonl"
    failures_path = tmp_path / "capture_failures.jsonl"
    hydrations_path = tmp_path / "hydrations.jsonl"
    monkeypatch.setattr(worker_mod.settings, "enrichments_path", str(enrich_path))
    monkeypatch.setattr(worker_mod.settings, "capture_failures_path", str(failures_path))
    monkeypatch.setattr(worker_mod.settings, "hydrations_path", str(hydrations_path))
    # Default off — see docstring above. Hydration tests opt back in.
    monkeypatch.setattr(worker_mod.settings, "og_fetch_enabled", False)
    monkeypatch.setattr(worker_mod.settings, "video_transcribe_enabled", False)
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
        # Phase 2.5 Fix 1 — empty content is logged with
        # phase="enrichment_skipped", not "enrichment". It's not a
        # failure, just nothing to enrich.
        enrich_path, failures_path = tmp_jsonl
        client = StubLLMClient([])
        asyncio.run(enqueue_enrichment("cid-empty", make_processed(clean_text=""), client))
        assert _read_jsonl(enrich_path) == []
        fails = _read_jsonl(failures_path)
        assert len(fails) == 1
        assert fails[0]["phase"] == "enrichment_skipped"
        assert fails[0]["reason"] == "empty_content"
        assert client.calls == []

    def test_too_long_logs_skip(self, tmp_jsonl, monkeypatch):
        # Phase 2.5 Fix 1 — same hygiene treatment as empty content.
        enrich_path, failures_path = tmp_jsonl
        monkeypatch.setattr(enrichment_mod.settings, "enrichment_max_input_chars", 10)
        client = StubLLMClient([])
        asyncio.run(enqueue_enrichment(
            "cid-long", make_processed(clean_text="x" * 100), client,
        ))
        fails = _read_jsonl(failures_path)
        assert len(fails) == 1
        assert fails[0]["phase"] == "enrichment_skipped"
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


# ---- Phase 2.5 Fix 2.A — hydration hook in enqueue_enrichment ----------

from backend.capture import hydration as hydration_mod  # noqa: E402
from backend.capture.og_fetcher import OGMetadata  # noqa: E402


def _stub_fetcher(meta: OGMetadata | None):
    """Return an async fetcher that always yields `meta`. None = miss."""
    async def _fetch(_url: str) -> OGMetadata | None:
        return meta
    return _fetch


class TestHydrationHook:
    """Covers the Fix 2.A wiring: empty captures get hydrated via the
    OG fetcher before enrichment runs, hydrated rows persist to the
    sidecar, and the LLM sees the hydrated text."""

    def test_empty_capture_is_hydrated_then_enriched(self, tmp_jsonl, monkeypatch):
        enrich_path, failures_path = tmp_jsonl
        hydrations_path = Path(worker_mod.settings.hydrations_path)
        monkeypatch.setattr(worker_mod.settings, "og_fetch_enabled", True)
        meta = OGMetadata(
            title="Real Article Title",
            description="The article's lede paragraph from og:description.",
            image_url="https://cdn.example.com/img.jpg",
            site_name="Example",
            source="og",
        )
        # Inject the stub fetcher into the hydration module — no network.
        monkeypatch.setattr(hydration_mod, "fetch_og_metadata", _stub_fetcher(meta))

        client = StubLLMClient([GOOD_RESPONSE])
        asyncio.run(enqueue_enrichment(
            "cid-hyd", make_processed(clean_text="", title="Telegram link"), client,
        ))

        # 1. Enrichment row written.
        rows = _read_jsonl(enrich_path)
        assert len(rows) == 1 and rows[0]["capture_id"] == "cid-hyd"
        # 2. No failure / skip rows.
        assert _read_jsonl(failures_path) == []
        # 3. Hydration sidecar persisted with the Fix-3 schema:
        #    `tier` is the dominant source, `tiers_used` lists every layer
        #    that contributed, OG-specific fields nested under "og".
        hyd = _read_jsonl(hydrations_path)
        assert len(hyd) == 1
        assert hyd[0]["capture_id"] == "cid-hyd"
        assert hyd[0]["tier"] == "og_metadata"
        assert hyd[0]["tiers_used"] == ["og_metadata"]
        assert hyd[0]["og"]["source"] == "og"
        assert hyd[0]["og"]["image_url"] == "https://cdn.example.com/img.jpg"
        assert hyd[0]["title_replaced"] is True
        assert hyd[0]["title_after"] == "Real Article Title"
        assert "transcript" not in hyd[0]
        # 4. LLM was called with the hydrated text, not the empty original.
        assert len(client.calls) == 1
        assert "lede paragraph" in client.calls[0]["text"]

    def test_hydration_disabled_falls_through_to_skipped(self, tmp_jsonl):
        # og_fetch_enabled defaults to False in tmp_jsonl — empty
        # captures should still land in enrichment_skipped.
        enrich_path, failures_path = tmp_jsonl
        client = StubLLMClient([])
        asyncio.run(enqueue_enrichment(
            "cid-off", make_processed(clean_text=""), client,
        ))
        fails = _read_jsonl(failures_path)
        assert len(fails) == 1
        assert fails[0]["phase"] == "enrichment_skipped"
        # Nothing in hydrations.jsonl — feature was off.
        assert _read_jsonl(Path(worker_mod.settings.hydrations_path)) == []

    def test_useful_check_filters_title_only_meta(self, tmp_jsonl, monkeypatch):
        # OG returned a title but no description — `is_useful` is False,
        # so we must NOT treat that as hydration (would just feed the
        # title back into LLM, which is the same as the empty-content
        # path).
        monkeypatch.setattr(worker_mod.settings, "og_fetch_enabled", True)
        meta = OGMetadata(title="Just a title", description=None, source="html")
        monkeypatch.setattr(hydration_mod, "fetch_og_metadata", _stub_fetcher(meta))

        enrich_path, failures_path = tmp_jsonl
        client = StubLLMClient([])
        asyncio.run(enqueue_enrichment(
            "cid-titleonly", make_processed(clean_text=""), client,
        ))
        fails = _read_jsonl(failures_path)
        assert len(fails) == 1
        assert fails[0]["phase"] == "enrichment_skipped"
        assert _read_jsonl(Path(worker_mod.settings.hydrations_path)) == []
        # LLM never called — we never had anything to send.
        assert client.calls == []

    def test_already_full_capture_skips_hydration(self, tmp_jsonl, monkeypatch):
        # Captures with content shouldn't trigger an HTTP fetch even
        # when og_fetch_enabled=True. Use a fetcher that explodes if
        # called to prove it.
        monkeypatch.setattr(worker_mod.settings, "og_fetch_enabled", True)

        async def _explode(_url: str):
            raise AssertionError("fetcher should not be called for non-empty capture")

        monkeypatch.setattr(hydration_mod, "fetch_og_metadata", _explode)

        enrich_path, failures_path = tmp_jsonl
        client = StubLLMClient([GOOD_RESPONSE])
        asyncio.run(enqueue_enrichment(
            "cid-full", make_processed(clean_text="Already has content here."), client,
        ))
        assert len(_read_jsonl(enrich_path)) == 1
        assert _read_jsonl(Path(worker_mod.settings.hydrations_path)) == []

    def test_fetcher_exception_does_not_kill_enrichment(self, tmp_jsonl, monkeypatch):
        # If the fetcher raises (it shouldn't, but defensive), we fall
        # through to the empty-content skip — no crash, no double-write.
        monkeypatch.setattr(worker_mod.settings, "og_fetch_enabled", True)

        async def _raises(_url: str):
            raise RuntimeError("network blew up")

        monkeypatch.setattr(hydration_mod, "fetch_og_metadata", _raises)

        enrich_path, failures_path = tmp_jsonl
        client = StubLLMClient([])
        asyncio.run(enqueue_enrichment(
            "cid-boom", make_processed(clean_text=""), client,
        ))
        fails = _read_jsonl(failures_path)
        assert len(fails) == 1
        assert fails[0]["phase"] == "enrichment_skipped"
        assert _read_jsonl(Path(worker_mod.settings.hydrations_path)) == []


# ---- Phase 2.5 Fix 3 — video transcription merge ----------------------

from backend.capture.video_transcriber import (  # noqa: E402
    TranscriptionResult,
    TranscriptionSkipped,
)


def _stub_transcriber(outcome):
    """Return an async transcriber that always yields `outcome`. Use
    a TranscriptionResult / TranscriptionSkipped / None."""
    async def _t(_url: str):
        return outcome
    return _t


def _video_processed(url: str = "https://www.instagram.com/reel/abc/") -> ProcessedContent:
    return make_processed(clean_text="", title="Telegram link", url=url)


class TestVideoTranscriptMerge:
    """Covers the Fix 3 wiring: video URLs run BOTH OG fetch and
    transcription, the sidecar records `tiers_used`, and the LLM sees
    the post caption + spoken transcript stitched together."""

    def test_video_url_merges_og_caption_and_transcript(self, tmp_jsonl, monkeypatch):
        enrich_path, failures_path = tmp_jsonl
        hydrations_path = Path(worker_mod.settings.hydrations_path)
        monkeypatch.setattr(worker_mod.settings, "og_fetch_enabled", True)
        monkeypatch.setattr(worker_mod.settings, "video_transcribe_enabled", True)

        # Both layers fire.
        meta = OGMetadata(
            title="Reel by @cookingnerd",
            description="30 ways to use leftover dal — saved my Sundays.",
            image_url="https://cdn.instagram.com/x.jpg",
            site_name="Instagram",
            source="og",
        )
        monkeypatch.setattr(hydration_mod, "fetch_og_metadata", _stub_fetcher(meta))
        transcription = TranscriptionResult(
            transcript="So today I'm showing you my grandmother's recipe for dal vada.",
            duration_seconds=58.2,
            title="Reel by @cookingnerd",
            extractor="Instagram",
        )
        monkeypatch.setattr(hydration_mod, "transcribe_video", _stub_transcriber(transcription))

        client = StubLLMClient([GOOD_RESPONSE])
        asyncio.run(enqueue_enrichment("cid-vid", _video_processed(), client))

        # Hydration sidecar records BOTH layers.
        hyd = _read_jsonl(hydrations_path)
        assert len(hyd) == 1
        assert hyd[0]["tier"] == "video_transcript"          # transcript dominates
        assert hyd[0]["tiers_used"] == ["og_metadata", "video_transcript"]
        assert hyd[0]["og"]["source"] == "og"
        assert hyd[0]["transcript"]["duration_seconds"] == 58.2
        assert hyd[0]["transcript"]["chars"] > 0
        assert hyd[0]["title_replaced"] is True
        assert hyd[0]["title_after"] == "Reel by @cookingnerd"

        # LLM sees both layers — caption AND transcript, labelled.
        assert len(client.calls) == 1
        sent = client.calls[0]["text"]
        assert "POST CAPTION" in sent and "leftover dal" in sent
        assert "TRANSCRIPT" in sent and "grandmother's recipe" in sent

        # Enrichment row produced as normal.
        assert len(_read_jsonl(enrich_path)) == 1
        assert _read_jsonl(failures_path) == []

    def test_video_transcribe_disabled_falls_back_to_og(self, tmp_jsonl, monkeypatch):
        # Whisper unavailable / disabled — OG-only path still works.
        monkeypatch.setattr(worker_mod.settings, "og_fetch_enabled", True)
        monkeypatch.setattr(worker_mod.settings, "video_transcribe_enabled", False)

        meta = OGMetadata(
            title="Reel by @x",
            description="Caption only.",
            source="og",
        )
        monkeypatch.setattr(hydration_mod, "fetch_og_metadata", _stub_fetcher(meta))

        async def explode(_url):
            raise AssertionError("transcriber should not be called when disabled")
        monkeypatch.setattr(hydration_mod, "transcribe_video", explode)

        enrich_path, failures_path = tmp_jsonl
        hydrations_path = Path(worker_mod.settings.hydrations_path)
        client = StubLLMClient([GOOD_RESPONSE])
        asyncio.run(enqueue_enrichment("cid-vid-noW", _video_processed(), client))

        hyd = _read_jsonl(hydrations_path)
        assert len(hyd) == 1
        assert hyd[0]["tier"] == "og_metadata"
        assert hyd[0]["tiers_used"] == ["og_metadata"]
        assert "transcript" not in hyd[0]

    def test_too_long_video_falls_back_to_og(self, tmp_jsonl, monkeypatch):
        # Transcription returned TranscriptionSkipped(reason='video_too_long')
        # — sidecar records the skip reason and uses OG content.
        monkeypatch.setattr(worker_mod.settings, "og_fetch_enabled", True)
        monkeypatch.setattr(worker_mod.settings, "video_transcribe_enabled", True)
        meta = OGMetadata(
            title="Long talk", description="Summary of the talk.", source="og",
        )
        monkeypatch.setattr(hydration_mod, "fetch_og_metadata", _stub_fetcher(meta))
        skipped = TranscriptionSkipped(reason="video_too_long", duration_seconds=3600.0)
        monkeypatch.setattr(hydration_mod, "transcribe_video", _stub_transcriber(skipped))

        enrich_path, failures_path = tmp_jsonl
        hydrations_path = Path(worker_mod.settings.hydrations_path)
        client = StubLLMClient([GOOD_RESPONSE])
        asyncio.run(enqueue_enrichment(
            "cid-long", _video_processed("https://www.youtube.com/watch?v=long"), client,
        ))

        hyd = _read_jsonl(hydrations_path)
        assert len(hyd) == 1
        assert hyd[0]["tier"] == "og_metadata"           # OG dominates because no transcript
        assert hyd[0]["tiers_used"] == ["og_metadata"]
        assert hyd[0]["transcript_skipped"]["reason"] == "video_too_long"
        assert hyd[0]["transcript_skipped"]["duration_seconds"] == 3600.0

    def test_video_with_no_og_uses_transcript_only(self, tmp_jsonl, monkeypatch):
        # OG returned nothing useful (e.g., IG private post returns
        # title-only meta). Transcript still runs and becomes the
        # capture's content.
        monkeypatch.setattr(worker_mod.settings, "og_fetch_enabled", True)
        monkeypatch.setattr(worker_mod.settings, "video_transcribe_enabled", True)
        meta = OGMetadata(title="Reel", description=None, source="html")
        monkeypatch.setattr(hydration_mod, "fetch_og_metadata", _stub_fetcher(meta))
        transcription = TranscriptionResult(
            transcript="The whole content lives in this transcript only.",
            duration_seconds=22.0,
            title="Reel",
            extractor="Instagram",
        )
        monkeypatch.setattr(hydration_mod, "transcribe_video", _stub_transcriber(transcription))

        enrich_path, failures_path = tmp_jsonl
        hydrations_path = Path(worker_mod.settings.hydrations_path)
        client = StubLLMClient([GOOD_RESPONSE])
        asyncio.run(enqueue_enrichment("cid-tonly", _video_processed(), client))

        hyd = _read_jsonl(hydrations_path)
        assert len(hyd) == 1
        assert hyd[0]["tier"] == "video_transcript"
        assert hyd[0]["tiers_used"] == ["video_transcript"]
        # OG block is omitted when OG wasn't useful.
        assert "og" not in hyd[0]
        # LLM sees just the transcript (no POST CAPTION header).
        sent = client.calls[0]["text"]
        assert "TRANSCRIPT" in sent
        assert "POST CAPTION" not in sent

    def test_non_video_url_does_not_call_transcriber(self, tmp_jsonl, monkeypatch):
        # Article URL, not a video — only OG runs.
        monkeypatch.setattr(worker_mod.settings, "og_fetch_enabled", True)
        monkeypatch.setattr(worker_mod.settings, "video_transcribe_enabled", True)
        meta = OGMetadata(
            title="Article", description="Article body.", source="og",
        )
        monkeypatch.setattr(hydration_mod, "fetch_og_metadata", _stub_fetcher(meta))

        async def explode(_url):
            raise AssertionError("transcriber should not run for non-video URLs")
        monkeypatch.setattr(hydration_mod, "transcribe_video", explode)

        enrich_path, failures_path = tmp_jsonl
        hydrations_path = Path(worker_mod.settings.hydrations_path)
        client = StubLLMClient([GOOD_RESPONSE])
        asyncio.run(enqueue_enrichment(
            "cid-art",
            make_processed(clean_text="", title="Telegram link",
                           url="https://en.wikipedia.org/wiki/Knowledge_graph"),
            client,
        ))

        hyd = _read_jsonl(hydrations_path)
        assert len(hyd) == 1
        assert hyd[0]["tier"] == "og_metadata"

    def test_transcriber_exception_does_not_kill_enrichment(self, tmp_jsonl, monkeypatch):
        # yt-dlp version bump or full disk → transcriber raises. Worker
        # falls back to OG content if available, otherwise skips
        # cleanly. Either way, no crash.
        monkeypatch.setattr(worker_mod.settings, "og_fetch_enabled", True)
        monkeypatch.setattr(worker_mod.settings, "video_transcribe_enabled", True)
        meta = OGMetadata(title="Reel", description="Caption present.", source="og")
        monkeypatch.setattr(hydration_mod, "fetch_og_metadata", _stub_fetcher(meta))

        async def boom(_url):
            raise RuntimeError("yt-dlp blew up")
        monkeypatch.setattr(hydration_mod, "transcribe_video", boom)

        enrich_path, failures_path = tmp_jsonl
        hydrations_path = Path(worker_mod.settings.hydrations_path)
        client = StubLLMClient([GOOD_RESPONSE])
        asyncio.run(enqueue_enrichment("cid-vboom", _video_processed(), client))

        # OG fallback path.
        hyd = _read_jsonl(hydrations_path)
        assert len(hyd) == 1
        assert hyd[0]["tier"] == "og_metadata"
        assert _read_jsonl(failures_path) == []
        assert len(_read_jsonl(enrich_path)) == 1


async def _noop_sleep(_seconds: float) -> None:
    return None


# ---- find_unenriched_capture_ids ------------------------------------

class TestFindUnenriched:
    def test_set_logic(self, tmp_path):
        captures = tmp_path / "captures.jsonl"
        enrichments = tmp_path / "enrichments.jsonl"
        failures = tmp_path / "capture_failures.jsonl"
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
            captures_path=captures,
            enrichments_path=enrichments,
            failures_path=failures,
        )
        assert out == ["a", "c"]

    def test_skipped_ids_are_excluded(self, tmp_path):
        # Phase 2.5 Fix 1 — IDs tagged enrichment_skipped should not be
        # re-queued by the startup recovery scan.
        captures = tmp_path / "captures.jsonl"
        enrichments = tmp_path / "enrichments.jsonl"
        failures = tmp_path / "capture_failures.jsonl"
        captures.write_text(
            "\n".join([
                json.dumps({"capture_id": "a"}),
                json.dumps({"capture_id": "b"}),
                json.dumps({"capture_id": "c"}),
                json.dumps({"capture_id": "d"}),
            ]) + "\n",
            encoding="utf-8",
        )
        enrichments.write_text(
            json.dumps({"capture_id": "a"}) + "\n",
            encoding="utf-8",
        )
        failures.write_text(
            "\n".join([
                # b was an empty-content skip
                json.dumps({"capture_id": "b", "phase": "enrichment_skipped",
                            "reason": "empty_content"}),
                # c was a real enrichment failure (transient) — must NOT
                # be excluded; we want to retry it on next boot.
                json.dumps({"capture_id": "c", "phase": "enrichment",
                            "reason": "transient_exhausted: blip"}),
                # legacy capture-side failure — also not excluded.
                json.dumps({"capture_id": "d", "phase": "capture",
                            "reason": "vision_error"}),
            ]) + "\n",
            encoding="utf-8",
        )
        out = find_unenriched_capture_ids(
            captures_path=captures,
            enrichments_path=enrichments,
            failures_path=failures,
        )
        # a is enriched, b is skipped → only c and d remain.
        assert out == ["c", "d"]

    def test_no_files(self, tmp_path):
        out = find_unenriched_capture_ids(
            captures_path=tmp_path / "missing.jsonl",
            enrichments_path=tmp_path / "missing2.jsonl",
            failures_path=tmp_path / "missing3.jsonl",
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
