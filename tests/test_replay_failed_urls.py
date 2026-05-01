"""Tests for scripts/replay_failed_urls.py — Phase 2.5 Fix 4.

Run with: pytest tests/test_replay_failed_urls.py -v

Covers the pure-logic functions (load + dedupe + already-enriched
filter + payload shape). The HTTP path itself is exercised by the
smoke test in docs/phase2-smoke-test.md Pass 8 — too dependent on a
running backend to mock cleanly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.replay_failed_urls import (  # noqa: E402
    FailureRow,
    ReplayCandidate,
    _bot_style_payload,
    load_already_enriched_urls,
    load_failure_rows,
    select_candidates,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )


# ---- load_failure_rows --------------------------------------------------

class TestLoadFailureRows:
    def test_skips_non_url_rows(self, tmp_path):
        # Phase 1 rows sometimes have null/empty URL (rejected payloads).
        p = tmp_path / "f.jsonl"
        _write_jsonl(p, [
            {"url": "https://example.com/a", "phase": "enrichment_skipped",
             "capture_id": "c1", "reason": "empty_content"},
            {"url": None, "phase": "capture", "reason": "processing"},
            {"url": "", "phase": "capture", "reason": "processing"},
            {"phase": "capture", "reason": "no url at all"},
            {"url": "tg://message/1/2", "phase": "capture",
             "reason": "telegram failed"},  # non-http URL
        ])
        rows = load_failure_rows(p)
        assert len(rows) == 1
        assert rows[0].url == "https://example.com/a"
        assert rows[0].phase == "enrichment_skipped"

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_failure_rows(tmp_path / "nope.jsonl") == []

    def test_malformed_lines_are_ignored(self, tmp_path):
        p = tmp_path / "f.jsonl"
        p.write_text(
            'not json\n'
            + json.dumps({"url": "https://x.com/a", "phase": "enrichment"}) + "\n"
            + '{"unterminated\n',
            encoding="utf-8",
        )
        rows = load_failure_rows(p)
        assert len(rows) == 1
        assert rows[0].url == "https://x.com/a"


# ---- load_already_enriched_urls ----------------------------------------

class TestLoadAlreadyEnriched:
    def test_joins_via_capture_id(self, tmp_path):
        cap = tmp_path / "captures.jsonl"
        enr = tmp_path / "enrichments.jsonl"
        _write_jsonl(cap, [
            {"capture_id": "a", "url": "https://example.com/A"},
            {"capture_id": "b", "url": "https://example.com/B"},
            {"capture_id": "c", "url": "https://example.com/C"},
        ])
        _write_jsonl(enr, [
            {"capture_id": "a", "enrichment": {"summary": "..."}},
            {"capture_id": "c", "enrichment": {"summary": "..."}},
        ])
        urls = load_already_enriched_urls(cap, enr)
        assert urls == {"https://example.com/A", "https://example.com/C"}

    def test_no_enrichments_returns_empty(self, tmp_path):
        cap = tmp_path / "captures.jsonl"
        enr = tmp_path / "enrichments.jsonl"
        _write_jsonl(cap, [{"capture_id": "a", "url": "https://x.com"}])
        # No enrichments file → empty set, nothing skipped
        assert load_already_enriched_urls(cap, enr) == set()


# ---- select_candidates --------------------------------------------------

def _failure(url: str, phase: str, *, capture_id: str = "cid", title: str = "t",
             platform: str = "general", reason: str = "r") -> FailureRow:
    return FailureRow(
        capture_id=capture_id, url=url, phase=phase,
        title=title, platform=platform, timestamp="2026-04-29T00:00:00+00:00",
        reason=reason,
    )


class TestSelectCandidates:
    def test_skips_capture_phase_by_default(self):
        rows = [
            _failure("https://x.com/a", "enrichment"),
            _failure("https://x.com/b", "enrichment_skipped"),
            _failure("https://x.com/c", "capture"),
        ]
        candidates, summary = select_candidates(rows, already_enriched_urls=set())
        urls = [c.url for c in candidates]
        assert urls == ["https://x.com/a", "https://x.com/b"]
        assert summary.skipped_capture_phase == 1

    def test_dedupes_by_url_first_wins(self):
        # Same URL appears in both an enrichment and an enrichment_skipped
        # row (e.g., the first attempt was a real failure, then Fix 1
        # re-tagged the second). First occurrence wins.
        rows = [
            _failure("https://x.com/a", "enrichment", capture_id="cid-1", reason="transient"),
            _failure("https://x.com/a", "enrichment_skipped", capture_id="cid-2", reason="empty_content"),
        ]
        candidates, _ = select_candidates(rows, already_enriched_urls=set())
        assert len(candidates) == 1
        assert candidates[0].original_phase == "enrichment"
        assert candidates[0].original_capture_id == "cid-1"

    def test_skips_already_enriched(self):
        rows = [
            _failure("https://x.com/a", "enrichment_skipped"),
            _failure("https://x.com/b", "enrichment_skipped"),
            _failure("https://x.com/c", "enrichment_skipped"),
        ]
        candidates, summary = select_candidates(
            rows,
            already_enriched_urls={"https://x.com/b"},
        )
        urls = [c.url for c in candidates]
        assert urls == ["https://x.com/a", "https://x.com/c"]
        assert summary.skipped_already_enriched == 1

    def test_phase_filter_overrides_default(self):
        rows = [
            _failure("https://x.com/a", "enrichment"),
            _failure("https://x.com/b", "enrichment_skipped"),
        ]
        candidates, _ = select_candidates(
            rows,
            already_enriched_urls=set(),
            phase_filter="enrichment_skipped",
        )
        assert [c.url for c in candidates] == ["https://x.com/b"]

    def test_summary_counts(self):
        rows = [
            _failure("https://x.com/a", "enrichment"),
            _failure("https://x.com/a", "enrichment"),       # dupe
            _failure("https://x.com/b", "enrichment_skipped"),
            _failure("https://x.com/c", "capture"),          # phase-filtered
            _failure("https://x.com/d", "enrichment_skipped"),  # already enriched
        ]
        _, summary = select_candidates(
            rows, already_enriched_urls={"https://x.com/d"},
        )
        assert summary.total_failure_rows == 5
        # candidates after phase + dedupe: a, b, d → 3
        assert summary.candidates_after_dedupe == 3
        assert summary.skipped_already_enriched == 1
        assert summary.skipped_capture_phase == 1


# ---- _bot_style_payload -------------------------------------------------

class TestBotStylePayload:
    def test_shape_matches_bot_handle_text(self):
        c = ReplayCandidate(
            url="https://www.instagram.com/reel/abc/",
            title="Reel",
            platform="instagram",
            original_capture_id="orig-cid",
            original_phase="enrichment_skipped",
            original_reason="empty_content",
        )
        p = _bot_style_payload(c)
        # Same fields the bot's handle_text uses
        assert p["url"] == c.url
        assert p["text"] == ""               # URL-only forward
        assert p["images"] == []
        assert p["platform"] == "instagram"
        assert p["title"] == "Reel"
        # Replay-side provenance lives in metadata so the resulting
        # capture row says "this came from replay, not from the bot".
        assert p["metadata"]["source"] == "replay_failed_urls"
        assert p["metadata"]["original_capture_id"] == "orig-cid"
        assert p["metadata"]["original_phase"] == "enrichment_skipped"
        assert p["metadata"]["original_reason"] == "empty_content"
