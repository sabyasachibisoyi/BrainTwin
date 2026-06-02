"""Tests for Phase 2 enrichment, post-Phase-3.5 cutover.

Run with: pytest tests/test_enrichment.py -v

These tests stub the Anthropic SDK so they can run offline. They cover:
  - schema validation (good/bad shapes, fallback behavior)
  - empty / oversized content skip cases
  - JSON-malformed retry path inside enrich()
  - retry policy in the worker (transient → success, transient → exhausted,
    permanent → no retry, malformed → no retry, schema error → no retry)
  - persistence: `sync_enrichment` / `sync_hydration` are spied so we
    can assert what the worker tried to persist without standing up SQL.
    capture_failures.jsonl is still a real file (it survived 3.5).
  - the SQL-backed `iter_unenriched_captures` recovery helper
  - backfill's `is_test_capture` classifier (the Phase-3.5 replacement
    for the old JSONL-row classifier)
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
    iter_unenriched_captures,
)
from backend.knowledge.llm_client import (  # noqa: E402
    MalformedResponseError,
    PermanentLLMError,
    TransientLLMError,
)
from backend.storage import (  # noqa: E402
    Capture,
    CaptureRepository,
    DEFAULT_USER_ID,
    UserRepository,
    init_db,
    session_scope,
)
from backend.storage import db as db_module  # noqa: E402
from scripts.backfill_enrichment import is_test_capture  # noqa: E402


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

class _WorkerWrites:
    """Captures what `enqueue_enrichment` tries to persist.

    Phase 3.5 replacement for the old `(enrich_path, failures_path)`
    tuple returned by `tmp_jsonl`. Successful enrichments and
    hydrations go through SQL only now, so we spy on `sync_enrichment`
    / `sync_hydration` instead of reading JSONL files for them. The
    failures log is still a real JSONL file on disk because
    capture_failures.jsonl was intentionally kept post-cutover (see
    docs/phase3.5-cutover.md, decision 2).
    """

    def __init__(self, failures_path: "Path"):
        self.enrichments: list[dict] = []  # kwargs passed to sync_enrichment
        self.hydrations: list[dict] = []   # decoded source_payload_json (the old hydrations.jsonl row)
        self.hydration_kwargs: list[dict] = []  # raw kwargs for completeness
        self.failures_path = failures_path

    def failures(self) -> list[dict]:
        """Read capture_failures.jsonl (still a real file post-3.5)."""
        if not self.failures_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.failures_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


@pytest.fixture
def worker_writes(tmp_path, monkeypatch):
    """Spy on the worker's persistence calls.

    Phase 3.5 replacement for the old `tmp_jsonl` fixture. Returns a
    `_WorkerWrites` object whose `.enrichments` and `.hydrations`
    lists fill in as the worker fires `sync_enrichment` /
    `sync_hydration`. The capture_failures.jsonl file is still real
    (and writable under `tmp_path`) because that log survived the
    cutover.

    Also disables OG fetching AND video transcription by default —
    Phase 2.5 Fixes 2 + 3 added pre-enrich hydration calls, but the
    bulk of these tests assume a deterministic worker that doesn't
    reach for the network. Tests that exercise hydration explicitly
    opt back in by monkeypatching `og_fetch_enabled` /
    `video_transcribe_enabled` and the matching injection points on
    the hydration module.
    """
    failures_path = tmp_path / "capture_failures.jsonl"
    monkeypatch.setattr(worker_mod.settings, "capture_failures_path", str(failures_path))
    # Default off — see docstring above. Hydration tests opt back in.
    monkeypatch.setattr(worker_mod.settings, "og_fetch_enabled", False)
    monkeypatch.setattr(worker_mod.settings, "video_transcribe_enabled", False)

    recorder = _WorkerWrites(failures_path=failures_path)

    async def _record_enrichment(**kwargs):
        recorder.enrichments.append(kwargs)
        return True

    async def _record_hydration(**kwargs):
        recorder.hydration_kwargs.append(kwargs)
        # `source_payload_json` carries the exact dict the old
        # hydrations.jsonl row held — decode it so tests can assert on
        # the same shape they used pre-3.5.
        payload = kwargs.get("source_payload_json")
        if payload:
            try:
                recorder.hydrations.append(json.loads(payload))
            except json.JSONDecodeError:
                recorder.hydrations.append({"_raw": payload})
        else:
            recorder.hydrations.append({})
        return True

    monkeypatch.setattr(worker_mod, "sync_enrichment", _record_enrichment)
    monkeypatch.setattr(worker_mod, "sync_hydration", _record_hydration)

    return recorder


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestEnqueueEnrichment:
    def test_success_writes_enrichment_only(self, worker_writes):
        client = StubLLMClient([GOOD_RESPONSE])
        asyncio.run(enqueue_enrichment("cid-good", make_processed(), client))
        assert len(worker_writes.enrichments) == 1
        call = worker_writes.enrichments[0]
        assert call["capture_id"] == "cid-good"
        assert call["summary"] == "A short summary."
        assert worker_writes.failures() == []

    def test_empty_content_logs_skip(self, worker_writes):
        # Phase 2.5 Fix 1 — empty content is logged with
        # phase="enrichment_skipped", not "enrichment". It's not a
        # failure, just nothing to enrich.
        client = StubLLMClient([])
        asyncio.run(enqueue_enrichment("cid-empty", make_processed(clean_text=""), client))
        assert worker_writes.enrichments == []
        fails = worker_writes.failures()
        assert len(fails) == 1
        assert fails[0]["phase"] == "enrichment_skipped"
        assert fails[0]["reason"] == "empty_content"
        assert client.calls == []

    def test_too_long_logs_skip(self, worker_writes, monkeypatch):
        # Phase 2.5 Fix 1 — same hygiene treatment as empty content.
        monkeypatch.setattr(enrichment_mod.settings, "enrichment_max_input_chars", 10)
        client = StubLLMClient([])
        asyncio.run(enqueue_enrichment(
            "cid-long", make_processed(clean_text="x" * 100), client,
        ))
        fails = worker_writes.failures()
        assert len(fails) == 1
        assert fails[0]["phase"] == "enrichment_skipped"
        assert "content_too_long" in fails[0]["reason"]
        assert client.calls == []

    def test_transient_then_success(self, worker_writes, monkeypatch):
        # Skip the actual sleep so the test runs fast
        monkeypatch.setattr(worker_mod.asyncio, "sleep", _noop_sleep)
        client = StubLLMClient([
            TransientLLMError("blip"),
            TransientLLMError("blip again"),
            GOOD_RESPONSE,
        ])
        asyncio.run(enqueue_enrichment("cid-transient", make_processed(), client))
        assert len(worker_writes.enrichments) == 1
        assert worker_writes.failures() == []
        assert len(client.calls) == 3

    def test_transient_exhausted_logs_failure(self, worker_writes, monkeypatch):
        monkeypatch.setattr(worker_mod.asyncio, "sleep", _noop_sleep)
        # 4 attempts (1 initial + 3 retries) all transient
        client = StubLLMClient([TransientLLMError(f"e{i}") for i in range(4)])
        asyncio.run(enqueue_enrichment("cid-x", make_processed(), client))
        assert worker_writes.enrichments == []
        fails = worker_writes.failures()
        assert len(fails) == 1
        assert "transient_exhausted" in fails[0]["reason"]
        assert len(client.calls) == 4

    def test_permanent_no_retry(self, worker_writes):
        client = StubLLMClient([PermanentLLMError("auth")])
        asyncio.run(enqueue_enrichment("cid-perm", make_processed(), client))
        fails = worker_writes.failures()
        assert len(fails) == 1
        assert "permanent" in fails[0]["reason"]
        # No retry: stub sees only one call
        assert len(client.calls) == 1

    def test_malformed_after_retry_logs_failure(self, worker_writes):
        # enrich() retries once internally, so 2 malformed = give up.
        client = StubLLMClient([
            MalformedResponseError("bad 1"),
            MalformedResponseError("bad 2"),
        ])
        asyncio.run(enqueue_enrichment("cid-mf", make_processed(), client))
        fails = worker_writes.failures()
        assert len(fails) == 1
        assert "malformed_json" in fails[0]["reason"]
        assert len(client.calls) == 2

    def test_schema_error_logs_failure(self, worker_writes):
        # Returns valid JSON dict but missing required keys → SchemaError.
        client = StubLLMClient([{"summary": "ok"}])
        asyncio.run(enqueue_enrichment("cid-schema", make_processed(), client))
        fails = worker_writes.failures()
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

    def test_empty_capture_is_hydrated_then_enriched(self, worker_writes, monkeypatch):
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

        # 1. Enrichment write happened.
        assert len(worker_writes.enrichments) == 1
        assert worker_writes.enrichments[0]["capture_id"] == "cid-hyd"
        # 2. No failure / skip rows.
        assert worker_writes.failures() == []
        # 3. Hydration sidecar persisted with the Fix-3 schema:
        #    `tier` is the dominant source, `tiers_used` lists every layer
        #    that contributed, OG-specific fields nested under "og".
        assert len(worker_writes.hydrations) == 1
        hyd = worker_writes.hydrations[0]
        assert hyd["capture_id"] == "cid-hyd"
        assert hyd["tier"] == "og_metadata"
        assert hyd["tiers_used"] == ["og_metadata"]
        assert hyd["og"]["source"] == "og"
        assert hyd["og"]["image_url"] == "https://cdn.example.com/img.jpg"
        assert hyd["title_replaced"] is True
        assert hyd["title_after"] == "Real Article Title"
        assert "transcript" not in hyd
        # 4. LLM was called with the hydrated text, not the empty original.
        assert len(client.calls) == 1
        assert "lede paragraph" in client.calls[0]["text"]

    def test_hydration_disabled_falls_through_to_skipped(self, worker_writes):
        # og_fetch_enabled defaults to False in worker_writes — empty
        # captures should still land in enrichment_skipped.
        client = StubLLMClient([])
        asyncio.run(enqueue_enrichment(
            "cid-off", make_processed(clean_text=""), client,
        ))
        fails = worker_writes.failures()
        assert len(fails) == 1
        assert fails[0]["phase"] == "enrichment_skipped"
        # No hydration call — feature was off.
        assert worker_writes.hydrations == []

    def test_useful_check_filters_title_only_meta(self, worker_writes, monkeypatch):
        # OG returned a title but no description — `is_useful` is False,
        # so we must NOT treat that as hydration (would just feed the
        # title back into LLM, which is the same as the empty-content
        # path).
        monkeypatch.setattr(worker_mod.settings, "og_fetch_enabled", True)
        meta = OGMetadata(title="Just a title", description=None, source="html")
        monkeypatch.setattr(hydration_mod, "fetch_og_metadata", _stub_fetcher(meta))

        client = StubLLMClient([])
        asyncio.run(enqueue_enrichment(
            "cid-titleonly", make_processed(clean_text=""), client,
        ))
        fails = worker_writes.failures()
        assert len(fails) == 1
        assert fails[0]["phase"] == "enrichment_skipped"
        assert worker_writes.hydrations == []
        # LLM never called — we never had anything to send.
        assert client.calls == []

    def test_already_full_capture_skips_hydration(self, worker_writes, monkeypatch):
        # Captures with content shouldn't trigger an HTTP fetch even
        # when og_fetch_enabled=True. Use a fetcher that explodes if
        # called to prove it.
        monkeypatch.setattr(worker_mod.settings, "og_fetch_enabled", True)

        async def _explode(_url: str):
            raise AssertionError("fetcher should not be called for non-empty capture")

        monkeypatch.setattr(hydration_mod, "fetch_og_metadata", _explode)

        client = StubLLMClient([GOOD_RESPONSE])
        asyncio.run(enqueue_enrichment(
            "cid-full", make_processed(clean_text="Already has content here."), client,
        ))
        assert len(worker_writes.enrichments) == 1
        assert worker_writes.hydrations == []

    def test_fetcher_exception_does_not_kill_enrichment(self, worker_writes, monkeypatch):
        # If the fetcher raises (it shouldn't, but defensive), we fall
        # through to the empty-content skip — no crash, no double-write.
        monkeypatch.setattr(worker_mod.settings, "og_fetch_enabled", True)

        async def _raises(_url: str):
            raise RuntimeError("network blew up")

        monkeypatch.setattr(hydration_mod, "fetch_og_metadata", _raises)

        client = StubLLMClient([])
        asyncio.run(enqueue_enrichment(
            "cid-boom", make_processed(clean_text=""), client,
        ))
        fails = worker_writes.failures()
        assert len(fails) == 1
        assert fails[0]["phase"] == "enrichment_skipped"
        assert worker_writes.hydrations == []


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

    def test_video_url_merges_og_caption_and_transcript(self, worker_writes, monkeypatch):
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
        assert len(worker_writes.hydrations) == 1
        hyd = worker_writes.hydrations[0]
        assert hyd["tier"] == "video_transcript"             # transcript dominates
        assert hyd["tiers_used"] == ["og_metadata", "video_transcript"]
        assert hyd["og"]["source"] == "og"
        assert hyd["transcript"]["duration_seconds"] == 58.2
        assert hyd["transcript"]["chars"] > 0
        assert hyd["title_replaced"] is True
        assert hyd["title_after"] == "Reel by @cookingnerd"

        # LLM sees both layers — caption AND transcript, labelled.
        assert len(client.calls) == 1
        sent = client.calls[0]["text"]
        assert "POST CAPTION" in sent and "leftover dal" in sent
        assert "TRANSCRIPT" in sent and "grandmother's recipe" in sent

        # Enrichment was persisted as normal.
        assert len(worker_writes.enrichments) == 1
        assert worker_writes.failures() == []

    def test_video_transcribe_disabled_falls_back_to_og(self, worker_writes, monkeypatch):
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

        client = StubLLMClient([GOOD_RESPONSE])
        asyncio.run(enqueue_enrichment("cid-vid-noW", _video_processed(), client))

        assert len(worker_writes.hydrations) == 1
        hyd = worker_writes.hydrations[0]
        assert hyd["tier"] == "og_metadata"
        assert hyd["tiers_used"] == ["og_metadata"]
        assert "transcript" not in hyd

    def test_too_long_video_falls_back_to_og(self, worker_writes, monkeypatch):
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

        client = StubLLMClient([GOOD_RESPONSE])
        asyncio.run(enqueue_enrichment(
            "cid-long", _video_processed("https://www.youtube.com/watch?v=long"), client,
        ))

        assert len(worker_writes.hydrations) == 1
        hyd = worker_writes.hydrations[0]
        assert hyd["tier"] == "og_metadata"              # OG dominates because no transcript
        assert hyd["tiers_used"] == ["og_metadata"]
        assert hyd["transcript_skipped"]["reason"] == "video_too_long"
        assert hyd["transcript_skipped"]["duration_seconds"] == 3600.0

    def test_video_with_no_og_uses_transcript_only(self, worker_writes, monkeypatch):
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

        client = StubLLMClient([GOOD_RESPONSE])
        asyncio.run(enqueue_enrichment("cid-tonly", _video_processed(), client))

        assert len(worker_writes.hydrations) == 1
        hyd = worker_writes.hydrations[0]
        assert hyd["tier"] == "video_transcript"
        assert hyd["tiers_used"] == ["video_transcript"]
        # OG block is omitted when OG wasn't useful.
        assert "og" not in hyd
        # LLM sees just the transcript (no POST CAPTION header).
        sent = client.calls[0]["text"]
        assert "TRANSCRIPT" in sent
        assert "POST CAPTION" not in sent

    def test_non_video_url_does_not_call_transcriber(self, worker_writes, monkeypatch):
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

        client = StubLLMClient([GOOD_RESPONSE])
        asyncio.run(enqueue_enrichment(
            "cid-art",
            make_processed(clean_text="", title="Telegram link",
                           url="https://en.wikipedia.org/wiki/Knowledge_graph"),
            client,
        ))

        assert len(worker_writes.hydrations) == 1
        assert worker_writes.hydrations[0]["tier"] == "og_metadata"

    def test_transcriber_exception_does_not_kill_enrichment(self, worker_writes, monkeypatch):
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

        client = StubLLMClient([GOOD_RESPONSE])
        asyncio.run(enqueue_enrichment("cid-vboom", _video_processed(), client))

        # OG fallback path.
        assert len(worker_writes.hydrations) == 1
        assert worker_writes.hydrations[0]["tier"] == "og_metadata"
        assert worker_writes.failures() == []
        assert len(worker_writes.enrichments) == 1


async def _noop_sleep(_seconds: float) -> None:
    return None


# ---- End-to-end: worker writes through to SQL + Chroma ---------------
# At least one test that doesn't rely on the worker_writes spies, so
# the actual sync_enrichment / sync_hydration wiring stays verified
# end-to-end. Pre-creates the parent capture row (the /capture endpoint
# does this in production), then asserts the enrichment + chunks +
# topics show up in SQL and Chroma after the worker runs.

class TestEnqueueEnrichmentEndToEnd:
    @pytest.fixture
    def sql_chroma_env(self, tmp_path, monkeypatch):
        chromadb = pytest.importorskip("chromadb")  # noqa: F841

        from backend.storage import db as db_module
        from backend.storage import embedder as embedder_mod
        from backend.storage import vector_store as vs_mod
        from backend.storage.embedder import EMBEDDING_DIM
        from backend.storage.vector_store import ChromaVectorStore

        import numpy as np

        # Phase 3.5: keep the failures log under tmp_path so this test
        # doesn't write into the real data/capture_failures.jsonl.
        monkeypatch.setattr(
            worker_mod.settings, "capture_failures_path",
            str(tmp_path / "capture_failures.jsonl"),
        )
        monkeypatch.setattr(worker_mod.settings, "og_fetch_enabled", False)
        monkeypatch.setattr(worker_mod.settings, "video_transcribe_enabled", False)

        # File-backed SQLite under tmp_path so state is fully isolated
        # per test. Using sqlite:///:memory: would leak across tests
        # because SQLAlchemy's default pool keeps the connection alive
        # even after we null the engine global; the next test then
        # finds the old DB still populated.
        db_path = tmp_path / "dual_write.db"
        monkeypatch.setattr(
            db_module.settings, "database_url", f"sqlite:///{db_path}",
        )
        # Reset SQL singletons so the new database_url takes effect.
        monkeypatch.setattr(db_module, "_engine", None)
        monkeypatch.setattr(db_module, "_session_factory", None)
        # tmp Chroma path so the test doesn't pollute data/chroma.
        monkeypatch.setattr(
            vs_mod.settings, "chroma_path", str(tmp_path / "chroma"),
        )
        monkeypatch.setattr(vs_mod, "_default_store", None)

        # Stub embedder — deterministic, no model download. Same shape
        # as test_storage_sync.py's _StubEmbedder.
        class _StubEmbedder:
            @property
            def model_name(self) -> str: return "stub"
            @property
            def dim(self) -> int: return EMBEDDING_DIM
            def _vec(self, text: str) -> np.ndarray:
                seed = abs(hash(text)) % (2**32)
                rng = np.random.default_rng(seed)
                v = rng.standard_normal(EMBEDDING_DIM, dtype=np.float32)
                return (v / max(float(np.linalg.norm(v)), 1e-9)).astype(np.float32)
            def embed(self, text: str) -> np.ndarray: return self._vec(text or "")
            def embed_many(self, texts: list[str]) -> list[np.ndarray]:
                return [self._vec(t or "") for t in texts]

        stub = _StubEmbedder()
        monkeypatch.setattr(embedder_mod, "_default_embedder", stub)
        # ChromaVectorStore takes the embedder via constructor; we
        # pre-create the singleton so sync_enrichment's get_vector_store()
        # returns this one (with the stub) rather than building a fresh
        # default Chroma that would try to load the real embedder.
        monkeypatch.setattr(
            vs_mod, "_default_store",
            ChromaVectorStore(embedder=stub, path=str(tmp_path / "chroma")),
        )

        yield tmp_path

        # Explicit teardown — dispose the engine so the next test
        # creates a fresh one. monkeypatch only restores the global
        # references; the live engine + connection pool need an
        # explicit aclose() to drop the SQLite file handle cleanly.
        try:
            asyncio.run(db_module.aclose())
        except Exception:
            pass

    def test_enrichment_worker_writes_to_sql_and_chroma(self, sql_chroma_env):
        from backend.storage import (
            ChunkRepository,
            EnrichmentRepository,
            TopicRepository,
            sync_capture,
        )
        from backend.storage.vector_store import COLLECTION_CHUNKS, get_vector_store

        async def go():
            await init_db()
            async with session_scope() as session:
                await UserRepository(session).create(
                    email="sabya@example.com",
                    display_name="Sabya",
                    user_id=DEFAULT_USER_ID,
                )
            # Capture row first (the /capture endpoint does this in prod).
            await sync_capture(
                capture_id="cid-dw",
                url="https://example.com/article",
                title="Test",
                platform="general",
                content_type="article",
                captured_at="2026-05-08T10:00:00+00:00",
                dwell_seconds=10,
                raw_metadata_json=None,
            )
            # Worker runs.
            client = StubLLMClient([GOOD_RESPONSE])
            await enqueue_enrichment(
                "cid-dw",
                make_processed(
                    clean_text=(
                        "Para one about kanban.\n\n"
                        "Para two about WIP limits.\n\n"
                        "Para three closing."
                    ),
                ),
                client,
            )
            # Verify SQL state.
            async with session_scope() as session:
                cap = await CaptureRepository(session).get(
                    "cid-dw", user_id=DEFAULT_USER_ID,
                )
                enr = await EnrichmentRepository(session).get_by_capture(
                    "cid-dw", user_id=DEFAULT_USER_ID,
                )
                chunk_rows = await ChunkRepository(session).list_by_capture(
                    "cid-dw", user_id=DEFAULT_USER_ID,
                )
                topic_rows = await TopicRepository(session).list_all()
            chroma_count = await get_vector_store().count(COLLECTION_CHUNKS)
            return cap, enr, chunk_rows, topic_rows, chroma_count

        cap, enr, chunk_rows, topic_rows, chroma_count = asyncio.run(go())
        assert cap is not None
        # Enrichment row landed (summary from GOOD_RESPONSE).
        assert enr is not None and enr.summary == "A short summary."
        # 3 paragraphs + 1 summary = 4 chunks.
        assert len(chunk_rows) == 4
        # Chroma mirrors SQL count.
        assert chroma_count == 4
        # Topics from GOOD_RESPONSE (topic-a, topic-b) made it through.
        topic_slugs = {t.slug for t in topic_rows}
        assert {"topic-a", "topic-b"}.issubset(topic_slugs)


# ---- iter_unenriched_captures (SQL-backed recovery helper) -----------

class TestIterUnenrichedCaptures:
    """Phase 3.5 replacement for the JSONL-scanning
    `find_unenriched_capture_ids`. Walks SQL and excludes capture_ids
    tagged `enrichment_skipped` in the failures log."""

    @pytest.fixture
    def fresh_db(self, monkeypatch):
        # In-memory SQLite per test, no Chroma involved.
        monkeypatch.setattr(db_module, "_engine", None)
        monkeypatch.setattr(db_module, "_session_factory", None)
        monkeypatch.setattr(
            db_module.settings, "database_url", "sqlite:///:memory:",
        )

        async def setup():
            await init_db()
            async with session_scope() as session:
                await UserRepository(session).create(
                    email="sabya@example.com",
                    display_name="Sabya",
                    user_id=DEFAULT_USER_ID,
                )

        asyncio.run(setup())
        yield
        try:
            asyncio.run(db_module.aclose())
        except Exception:
            pass

    @staticmethod
    async def _seed_captures(ids: list[str]) -> None:
        async with session_scope() as session:
            repo = CaptureRepository(session)
            for i, cid in enumerate(ids):
                # Strictly increasing captured_at so the ordering
                # assertion below is deterministic regardless of insert
                # order.
                ts = f"2026-05-08T10:0{i}:00+00:00"
                await repo.create(Capture(
                    id=cid,
                    user_id=DEFAULT_USER_ID,
                    url=f"https://example.com/{cid}",
                    title=cid,
                    platform="general",
                    content_type="article",
                    captured_at=ts,
                    dwell_seconds=0,
                    raw_metadata_json=None,
                    clean_text="body",
                ))

    @staticmethod
    async def _seed_enrichments(ids: list[str]) -> None:
        from backend.storage import EnrichmentRepository

        async with session_scope() as session:
            repo = EnrichmentRepository(session)
            for cid in ids:
                await repo.create(
                    capture_id=cid,
                    summary=f"sum-{cid}",
                    key_facts_json="[]",
                    model="stub",
                    enriched_at="2026-05-08T10:30:00+00:00",
                )

    async def _collect(self, **kwargs):
        out = []
        async for cap in iter_unenriched_captures(**kwargs):
            out.append(cap)
        return out

    def test_returns_only_unenriched(self, fresh_db, tmp_path):
        asyncio.run(self._seed_captures(["a", "b", "c"]))
        asyncio.run(self._seed_enrichments(["b"]))
        rows = asyncio.run(self._collect(
            user_id=DEFAULT_USER_ID,
            failures_path=tmp_path / "missing.jsonl",
        ))
        assert [r.id for r in rows] == ["a", "c"]

    def test_excludes_enrichment_skipped(self, fresh_db, tmp_path):
        asyncio.run(self._seed_captures(["a", "b", "c", "d"]))
        asyncio.run(self._seed_enrichments(["a"]))

        failures = tmp_path / "capture_failures.jsonl"
        failures.write_text(
            "\n".join([
                json.dumps({"capture_id": "b", "phase": "enrichment_skipped",
                            "reason": "empty_content"}),
                json.dumps({"capture_id": "c", "phase": "enrichment",
                            "reason": "transient_exhausted: blip"}),
                json.dumps({"capture_id": "d", "phase": "capture",
                            "reason": "vision_error"}),
            ]) + "\n",
            encoding="utf-8",
        )
        rows = asyncio.run(self._collect(
            user_id=DEFAULT_USER_ID, failures_path=failures,
        ))
        # a is enriched, b is skipped → only c and d remain.
        assert [r.id for r in rows] == ["c", "d"]

    def test_empty_when_no_captures(self, fresh_db, tmp_path):
        rows = asyncio.run(self._collect(
            user_id=DEFAULT_USER_ID,
            failures_path=tmp_path / "missing.jsonl",
        ))
        assert rows == []


# ---- backfill is_test_capture classifier ----------------------------
#
# Phase 3.5 — `is_test_row(dict)` was replaced with
# `is_test_capture(Capture)` since backfill now walks SQL rows, not
# JSONL dicts. Same fingerprint logic, same skip reasons.

def _cap(**overrides) -> Capture:
    defaults = dict(
        id="cid",
        user_id=DEFAULT_USER_ID,
        url="https://realsite.com",
        title="An Article",
        platform="general",
        content_type="article",
        captured_at="2026-05-08T10:00:00+00:00",
        dwell_seconds=0,
        raw_metadata_json=None,
        clean_text="Some real content here.",
        transcript=None,
        image_text="",
        image_descriptions_json=None,
        text_source="extension",
    )
    defaults.update(overrides)
    return Capture(**defaults)


class TestIsTestCapture:
    def test_skips_example_com(self):
        skip, _ = is_test_capture(_cap(url="https://example.com/foo"))
        assert skip

    def test_skips_mock_capture_metadata(self):
        skip, _ = is_test_capture(_cap(
            raw_metadata_json=json.dumps({"source": "mock_capture"}),
        ))
        assert skip

    def test_skips_empty_telegram_link(self):
        skip, _ = is_test_capture(_cap(
            url="https://t.me/foo",
            title="Telegram link",
            clean_text="",
            image_text="",
        ))
        assert skip

    def test_skips_no_text_at_all(self):
        skip, _ = is_test_capture(_cap(
            clean_text="",
            transcript=None,
            image_text="",
        ))
        assert skip

    def test_keeps_real_row(self):
        skip, _ = is_test_capture(_cap(
            url="https://hindustantimes.com/article",
            title="HSR Layout rents jump",
            clean_text="Some real content here.",
            raw_metadata_json=json.dumps({"source": "chrome_extension"}),
        ))
        assert not skip
