"""Tests for scripts/migrate_jsonl_to_sql.py — Phase 3 Step 5 backfill.

Run with: pytest tests/test_migrate_jsonl_to_sql.py -v

Project convention (matches tests/test_storage_sync.py):
  - synchronous test functions
  - async work wrapped in asyncio.run(go())
  - clean_engine autouse fixture resets the SQL singleton between tests

These tests exercise the migration's orchestration logic without
touching real LLM / Chroma / SentenceTransformer dependencies. We use
the real SQL layer (in-memory SQLite via the project conftest's
DATABASE_URL pin) and stub `sync_capture` / `sync_hydration` /
`sync_enrichment` so we can assert WHICH rows the orchestration tries
to insert and that idempotency probes correctly skip already-mirrored
ids.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from backend.storage import (  # noqa: E402
    CaptureRepository,
    EnrichmentRepository,
    HydrationRepository,
    UserRepository,
)
from backend.storage import db as db_module  # noqa: E402
from backend.storage.db import init_db, session_scope  # noqa: E402
from backend.storage.models import Capture  # noqa: E402
from scripts import migrate_jsonl_to_sql as mig  # noqa: E402


# ---- Fixtures --------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_engine(monkeypatch):
    """Reset SQL engine + session factory between tests so each gets
    a fresh in-memory DB. Matches tests/test_storage_sync.py."""
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_session_factory", None)
    yield


def _write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            if isinstance(row, str):  # raw line — for malformed JSON tests
                f.write(row + "\n")
            else:
                f.write(json.dumps(row) + "\n")


async def _seed_user_and_init() -> None:
    """Init the schema and seed user_id=1 (Sabya) so FK on captures
    succeeds. Mirrors what the migration's run() does in production."""
    await init_db()
    async with session_scope() as session:
        repo = UserRepository(session)
        if await repo.get(mig.DEFAULT_USER_ID) is None:
            await repo.create(
                email="sabya.bisoyi@gmail.com",
                display_name="Sabya",
                user_id=mig.DEFAULT_USER_ID,
            )


# ---- Pure-helper tests (no DB) --------------------------------------

def test_legacy_capture_id_is_deterministic():
    row = {"url": "https://example.com/a", "timestamp": "2026-01-01T00:00:00+00:00"}
    a = mig._mint_legacy_capture_id(row)
    b = mig._mint_legacy_capture_id(row)
    assert a == b
    assert len(a) == 36


def test_legacy_capture_id_changes_with_url():
    a = mig._mint_legacy_capture_id({"url": "https://x.com", "timestamp": "t"})
    b = mig._mint_legacy_capture_id({"url": "https://y.com", "timestamp": "t"})
    assert a != b


def test_legacy_capture_id_falls_back_to_uuid4_when_both_missing():
    a = mig._mint_legacy_capture_id({})
    b = mig._mint_legacy_capture_id({})
    # Without url+timestamp the id can't be stable; uuid4 fallback.
    assert a != b


def test_iter_jsonl_skips_blank_lines(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a":1}\n\n{"b":2}\n', encoding="utf-8")
    rows = list(mig._iter_jsonl(p))
    assert [r[1] for r in rows] == [{"a": 1}, {"b": 2}]


def test_iter_jsonl_yields_bad_json_sentinel(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"good":1}\n{not json\n{"good":2}\n', encoding="utf-8")
    rows = list(mig._iter_jsonl(p))
    assert len(rows) == 3
    assert rows[1][1].get("__bad_json__") is True


def test_log_failure_writes_jsonl(tmp_path):
    fail = tmp_path / "failures.jsonl"
    mig._log_failure(
        fail, source_file="x.jsonl", line_number=7,
        raw_row={"a": 1}, error_reason="oops",
    )
    mig._log_failure(
        fail, source_file="x.jsonl", line_number=8,
        raw_row="not-json", error_reason="bad json",
    )
    lines = fail.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    r0 = json.loads(lines[0])
    assert r0 == {
        "source_file": "x.jsonl", "line_number": 7,
        "raw_row": {"a": 1}, "error_reason": "oops",
    }


# ---- Stage 1 — captures ---------------------------------------------

def test_stage1_skips_test_fixtures(tmp_path, monkeypatch):
    captures = tmp_path / "captures.jsonl"
    fail = tmp_path / "failures.jsonl"
    _write_jsonl(captures, [
        {  # Real row
            "capture_id": "real-1",
            "url": "https://en.wikipedia.org/wiki/X",
            "title": "X", "platform": "general", "content_type": "article",
            "clean_text": "hello world",
            "text_source": "extension",
            "transcript": None, "image_descriptions": [], "image_text": "",
            "timestamp": "2026-04-01T00:00:00+00:00",
            "dwell_time_seconds": 30, "metadata": {},
        },
        {  # mock fixture
            "capture_id": "mock-1",
            "url": "https://example.com",
            "title": "M", "platform": "general", "content_type": "article",
            "clean_text": "x", "text_source": "extension",
            "transcript": None, "image_descriptions": [], "image_text": "",
            "timestamp": "2026-04-01T00:00:01+00:00",
            "dwell_time_seconds": 0, "metadata": {"source": "mock_capture"},
        },
    ])

    calls: list[str] = []

    async def fake_sync_capture(**kwargs):
        calls.append(kwargs["capture_id"])
        async with session_scope() as session:
            await CaptureRepository(session).create(Capture(
                id=kwargs["capture_id"],
                user_id=kwargs["user_id"],
                url=kwargs.get("url"),
                title=kwargs.get("title"),
                platform=kwargs.get("platform"),
                content_type=kwargs.get("content_type"),
                captured_at=kwargs["captured_at"],
                dwell_seconds=kwargs.get("dwell_seconds", 0),
                raw_metadata_json=kwargs.get("raw_metadata_json"),
            ))
        return True

    monkeypatch.setattr(mig, "sync_capture", fake_sync_capture)

    async def go():
        await _seed_user_and_init()
        return await mig._migrate_captures(
            captures_path=captures,
            failures_path=fail,
            user_id=mig.DEFAULT_USER_ID,
            dry_run=False,
            include_test_rows=False,
            limit=None,
        )

    counts = asyncio.run(go())
    assert counts["seen"] == 2
    assert counts["test_skipped"] == 1
    assert counts["inserted"] == 1
    assert calls == ["real-1"]


def test_stage1_idempotent_on_rerun(tmp_path, monkeypatch):
    captures = tmp_path / "captures.jsonl"
    fail = tmp_path / "failures.jsonl"
    _write_jsonl(captures, [{
        "capture_id": "c1",
        "url": "https://en.wikipedia.org/wiki/A",
        "title": "A", "platform": "general", "content_type": "article",
        "clean_text": "hello", "text_source": "extension",
        "transcript": None, "image_descriptions": [], "image_text": "",
        "timestamp": "2026-04-01T00:00:00+00:00",
        "dwell_time_seconds": 0, "metadata": {},
    }])

    insert_count = 0

    async def fake_sync_capture(**kwargs):
        nonlocal insert_count
        async with session_scope() as session:
            await CaptureRepository(session).create(Capture(
                id=kwargs["capture_id"], user_id=kwargs["user_id"],
                url=kwargs.get("url"), title=kwargs.get("title"),
                platform=kwargs.get("platform"),
                content_type=kwargs.get("content_type"),
                captured_at=kwargs["captured_at"],
                dwell_seconds=kwargs.get("dwell_seconds", 0),
                raw_metadata_json=kwargs.get("raw_metadata_json"),
            ))
        insert_count += 1
        return True

    monkeypatch.setattr(mig, "sync_capture", fake_sync_capture)

    async def go():
        await _seed_user_and_init()
        c1 = await mig._migrate_captures(
            captures_path=captures, failures_path=fail,
            user_id=mig.DEFAULT_USER_ID,
            dry_run=False, include_test_rows=False, limit=None,
        )
        c2 = await mig._migrate_captures(
            captures_path=captures, failures_path=fail,
            user_id=mig.DEFAULT_USER_ID,
            dry_run=False, include_test_rows=False, limit=None,
        )
        return c1, c2

    c1, c2 = asyncio.run(go())
    assert c1["inserted"] == 1
    assert c2["already_in_sql"] == 1
    assert c2["inserted"] == 0
    assert insert_count == 1


def test_stage1_dry_run_writes_nothing(tmp_path, monkeypatch):
    captures = tmp_path / "captures.jsonl"
    fail = tmp_path / "failures.jsonl"
    _write_jsonl(captures, [{
        "capture_id": "c1",
        "url": "https://en.wikipedia.org/wiki/A",
        "title": "A", "platform": "general", "content_type": "article",
        "clean_text": "hello", "text_source": "extension",
        "transcript": None, "image_descriptions": [], "image_text": "",
        "timestamp": "2026-04-01T00:00:00+00:00",
        "dwell_time_seconds": 0, "metadata": {},
    }])

    async def boom(**_):
        raise AssertionError("sync_capture must not be called in dry-run")

    monkeypatch.setattr(mig, "sync_capture", boom)

    async def go():
        # Schema must exist even in dry-run — _migrate_captures now
        # pre-loads existing capture_ids unconditionally so the counts
        # the operator sees match what a real run would do.
        await _seed_user_and_init()
        return await mig._migrate_captures(
            captures_path=captures, failures_path=fail,
            user_id=mig.DEFAULT_USER_ID,
            dry_run=True, include_test_rows=False, limit=None,
        )

    counts = asyncio.run(go())
    assert counts["seen"] == 1
    assert counts["inserted"] == 1


def test_stage1_bad_json_logged_and_continues(tmp_path, monkeypatch):
    captures = tmp_path / "captures.jsonl"
    fail = tmp_path / "failures.jsonl"
    captures.write_text(
        '{"capture_id":"c1","url":"https://wiki/x","title":"X",'
        '"platform":"general","content_type":"article","clean_text":"hi",'
        '"text_source":"extension","transcript":null,'
        '"image_descriptions":[],"image_text":"",'
        '"timestamp":"2026-04-01T00:00:00+00:00",'
        '"dwell_time_seconds":0,"metadata":{}}\n'
        "this is not json\n"
        '{"capture_id":"c2","url":"https://wiki/y","title":"Y",'
        '"platform":"general","content_type":"article","clean_text":"hi",'
        '"text_source":"extension","transcript":null,'
        '"image_descriptions":[],"image_text":"",'
        '"timestamp":"2026-04-01T00:00:00+00:00",'
        '"dwell_time_seconds":0,"metadata":{}}\n',
        encoding="utf-8",
    )

    async def fake_sync(**kwargs):
        async with session_scope() as session:
            await CaptureRepository(session).create(Capture(
                id=kwargs["capture_id"], user_id=kwargs["user_id"],
                url=kwargs.get("url"), title=kwargs.get("title"),
                platform=kwargs.get("platform"),
                content_type=kwargs.get("content_type"),
                captured_at=kwargs["captured_at"],
                dwell_seconds=kwargs.get("dwell_seconds", 0),
                raw_metadata_json=kwargs.get("raw_metadata_json"),
            ))
        return True

    monkeypatch.setattr(mig, "sync_capture", fake_sync)

    async def go():
        await _seed_user_and_init()
        return await mig._migrate_captures(
            captures_path=captures, failures_path=fail,
            user_id=mig.DEFAULT_USER_ID,
            dry_run=False, include_test_rows=False, limit=None,
        )

    counts = asyncio.run(go())
    assert counts["bad_json"] == 1
    assert counts["inserted"] == 2
    failures = [json.loads(l) for l in fail.read_text(encoding="utf-8").splitlines()]
    assert len(failures) == 1
    assert failures[0]["line_number"] == 2


# ---- Stage 2 — hydrations -------------------------------------------

def test_stage2_skips_when_parent_capture_missing(tmp_path, monkeypatch):
    hyd = tmp_path / "hydrations.jsonl"
    fail = tmp_path / "failures.jsonl"
    _write_jsonl(hyd, [{
        "capture_id": "orphan",
        "tier": "og_metadata",
        "timestamp": "2026-04-01T00:00:00+00:00",
    }])

    async def boom(**_):
        raise AssertionError("sync_hydration must not be called for orphan parent")

    monkeypatch.setattr(mig, "sync_hydration", boom)

    async def go():
        await _seed_user_and_init()
        return await mig._migrate_hydrations(
            hydrations_path=hyd, failures_path=fail,
            user_id=mig.DEFAULT_USER_ID, dry_run=False, limit=None,
        )

    counts = asyncio.run(go())
    assert counts["failed"] == 1
    assert counts["inserted"] == 0
    failures = [json.loads(l) for l in fail.read_text(encoding="utf-8").splitlines()]
    assert "parent capture orphan not in SQL" in failures[0]["error_reason"]


def test_stage2_idempotent_on_rerun(tmp_path, monkeypatch):
    """Once a hydration row exists in SQL for capture X, subsequent runs
    skip every hydration row for X (coarse-grained; documented in script)."""
    hyd = tmp_path / "hydrations.jsonl"
    fail = tmp_path / "failures.jsonl"
    _write_jsonl(hyd, [{
        "capture_id": "cap1", "tier": "og_metadata",
        "timestamp": "2026-04-01T00:00:00+00:00",
    }])

    async def fake_sync_hydration(**kwargs):
        async with session_scope() as session:
            await HydrationRepository(session).create(
                capture_id=kwargs["capture_id"],
                tier=kwargs["tier"],
                source_payload_json=kwargs.get("source_payload_json"),
                hydrated_at=kwargs["hydrated_at"],
            )
        return True

    monkeypatch.setattr(mig, "sync_hydration", fake_sync_hydration)

    async def go():
        await _seed_user_and_init()
        async with session_scope() as session:
            await CaptureRepository(session).create(Capture(
                id="cap1", user_id=mig.DEFAULT_USER_ID,
                url="https://x", title="t", platform="general",
                content_type="article",
                captured_at="2026-04-01T00:00:00+00:00",
                dwell_seconds=0, raw_metadata_json="{}",
            ))
        c1 = await mig._migrate_hydrations(
            hydrations_path=hyd, failures_path=fail,
            user_id=mig.DEFAULT_USER_ID, dry_run=False, limit=None,
        )
        c2 = await mig._migrate_hydrations(
            hydrations_path=hyd, failures_path=fail,
            user_id=mig.DEFAULT_USER_ID, dry_run=False, limit=None,
        )
        return c1, c2

    c1, c2 = asyncio.run(go())
    assert c1["inserted"] == 1
    assert c2["inserted"] == 0
    assert c2["already_in_sql"] == 1


# ---- Stage 3 — enrichments ------------------------------------------

def test_stage3_skips_already_enriched(tmp_path, monkeypatch):
    captures = tmp_path / "captures.jsonl"
    enrichments = tmp_path / "enrichments.jsonl"
    fail = tmp_path / "failures.jsonl"
    _write_jsonl(captures, [{
        "capture_id": "cap1", "url": "https://x", "title": "t",
        "platform": "general", "content_type": "article",
        "clean_text": "hi", "text_source": "extension",
        "transcript": None, "image_descriptions": [], "image_text": "",
        "timestamp": "2026-04-01T00:00:00+00:00",
        "dwell_time_seconds": 0, "metadata": {},
    }])
    _write_jsonl(enrichments, [{
        "capture_id": "cap1", "enriched_at": "2026-04-02T00:00:00+00:00",
        "model": "m",
        "enrichment": {"summary": "new", "topics": ["t1"], "entities": [],
                       "key_facts": []},
    }])

    async def boom(**_):
        raise AssertionError("sync_enrichment must skip already-enriched")

    monkeypatch.setattr(mig, "sync_enrichment", boom)

    async def go():
        await _seed_user_and_init()
        async with session_scope() as session:
            await CaptureRepository(session).create(Capture(
                id="cap1", user_id=mig.DEFAULT_USER_ID,
                url="https://x", title="t", platform="general",
                content_type="article",
                captured_at="2026-04-01T00:00:00+00:00",
                dwell_seconds=0, raw_metadata_json="{}",
            ))
            await EnrichmentRepository(session).create(
                capture_id="cap1", summary="prior",
                key_facts_json="[]", model="m",
                enriched_at="2026-04-02T00:00:00+00:00",
            )
        return await mig._migrate_enrichments(
            captures_path=captures, enrichments_path=enrichments,
            failures_path=fail,
            user_id=mig.DEFAULT_USER_ID, dry_run=False, limit=None,
        )

    counts = asyncio.run(go())
    assert counts["already_in_sql"] == 1
    assert counts["inserted"] == 0


def test_stage3_passes_topics_and_entities_through(tmp_path, monkeypatch):
    captures = tmp_path / "captures.jsonl"
    enrichments = tmp_path / "enrichments.jsonl"
    fail = tmp_path / "failures.jsonl"
    _write_jsonl(captures, [{
        "capture_id": "cap1", "url": "https://x", "title": "t",
        "platform": "general", "content_type": "article",
        "clean_text": "hi", "text_source": "extension",
        "transcript": None, "image_descriptions": [], "image_text": "",
        "timestamp": "2026-04-01T00:00:00+00:00",
        "dwell_time_seconds": 0, "metadata": {},
    }])
    _write_jsonl(enrichments, [{
        "capture_id": "cap1", "enriched_at": "2026-04-02T00:00:00+00:00",
        "model": "m",
        "enrichment": {
            "summary": "S",
            "topics": ["kanban", "agile"],
            "entities": [{"name": "Toyota", "type": "company"}],
            "key_facts": ["fact1"],
        },
    }])

    captured_kwargs: list[dict] = []

    async def fake_sync_enrichment(**kwargs):
        captured_kwargs.append(kwargs)
        return True

    monkeypatch.setattr(mig, "sync_enrichment", fake_sync_enrichment)

    async def go():
        await _seed_user_and_init()
        async with session_scope() as session:
            await CaptureRepository(session).create(Capture(
                id="cap1", user_id=mig.DEFAULT_USER_ID,
                url="https://x", title="t", platform="general",
                content_type="article",
                captured_at="2026-04-01T00:00:00+00:00",
                dwell_seconds=0, raw_metadata_json="{}",
            ))
        return await mig._migrate_enrichments(
            captures_path=captures, enrichments_path=enrichments,
            failures_path=fail,
            user_id=mig.DEFAULT_USER_ID, dry_run=False, limit=None,
        )

    counts = asyncio.run(go())
    assert counts["inserted"] == 1
    assert len(captured_kwargs) == 1
    kw = captured_kwargs[0]
    assert kw["capture_id"] == "cap1"
    assert kw["summary"] == "S"
    assert kw["topics"] == ["kanban", "agile"]
    assert kw["entities"] == [{"name": "Toyota", "type": "company"}]
    assert kw["model"] == "m"
    assert kw["user_id"] == mig.DEFAULT_USER_ID
    from backend.capture.processor import ProcessedContent
    assert isinstance(kw["processed"], ProcessedContent)
    assert kw["processed"].clean_text == "hi"


def test_stage3_handles_unhydratable_capture(tmp_path, monkeypatch):
    """Captures rows missing fields hydrate_processed needs still allow
    enrichment metadata (+ topics/entities) to land — only the chunk
    pipeline is skipped (processed=None)."""
    captures = tmp_path / "captures.jsonl"
    enrichments = tmp_path / "enrichments.jsonl"
    fail = tmp_path / "failures.jsonl"
    _write_jsonl(captures, [{
        "capture_id": "cap1", "url": "https://x",
        "timestamp": "2026-04-01T00:00:00+00:00",
        # NO clean_text, text_source, etc.
    }])
    _write_jsonl(enrichments, [{
        "capture_id": "cap1", "enriched_at": "2026-04-02T00:00:00+00:00",
        "model": "m",
        "enrichment": {"summary": "S", "topics": ["t"], "entities": []},
    }])

    captured_kwargs: list[dict] = []

    async def fake_sync_enrichment(**kwargs):
        captured_kwargs.append(kwargs)
        return True

    monkeypatch.setattr(mig, "sync_enrichment", fake_sync_enrichment)

    async def go():
        await _seed_user_and_init()
        async with session_scope() as session:
            await CaptureRepository(session).create(Capture(
                id="cap1", user_id=mig.DEFAULT_USER_ID,
                url="https://x", title=None, platform=None,
                content_type=None,
                captured_at="2026-04-01T00:00:00+00:00",
                dwell_seconds=0, raw_metadata_json=None,
            ))
        return await mig._migrate_enrichments(
            captures_path=captures, enrichments_path=enrichments,
            failures_path=fail,
            user_id=mig.DEFAULT_USER_ID, dry_run=False, limit=None,
        )

    counts = asyncio.run(go())
    assert counts["inserted"] == 1
    assert counts["unhydratable"] == 1
    assert captured_kwargs[0]["processed"] is None


def test_stage3_corrupt_enrichment_field_logged_not_raised(tmp_path, monkeypatch):
    """If the `enrichment` field is the wrong type (string / list instead
    of dict), the loop must log + skip the row, NOT raise AttributeError
    on the next .get() and abort the whole migration."""
    captures = tmp_path / "captures.jsonl"
    enrichments = tmp_path / "enrichments.jsonl"
    fail = tmp_path / "failures.jsonl"
    _write_jsonl(captures, [
        {
            "capture_id": "cap-good", "url": "https://x", "title": "t",
            "platform": "general", "content_type": "article",
            "clean_text": "hi", "text_source": "extension",
            "transcript": None, "image_descriptions": [], "image_text": "",
            "timestamp": "2026-04-01T00:00:00+00:00",
            "dwell_time_seconds": 0, "metadata": {},
        },
        {
            "capture_id": "cap-bad", "url": "https://y", "title": "t",
            "platform": "general", "content_type": "article",
            "clean_text": "hi", "text_source": "extension",
            "transcript": None, "image_descriptions": [], "image_text": "",
            "timestamp": "2026-04-01T00:00:00+00:00",
            "dwell_time_seconds": 0, "metadata": {},
        },
    ])
    _write_jsonl(enrichments, [
        # Corrupt — enrichment is a string, not a dict.
        {"capture_id": "cap-bad", "enriched_at": "2026-04-02T00:00:00+00:00",
         "model": "m", "enrichment": "this should be a dict"},
        # Valid — must still process after the bad row above.
        {"capture_id": "cap-good", "enriched_at": "2026-04-02T00:00:00+00:00",
         "model": "m",
         "enrichment": {"summary": "S", "topics": [], "entities": [],
                        "key_facts": []}},
    ])

    captured: list[dict] = []

    async def fake_sync_enrichment(**kwargs):
        captured.append(kwargs)
        return True

    monkeypatch.setattr(mig, "sync_enrichment", fake_sync_enrichment)

    async def go():
        await _seed_user_and_init()
        async with session_scope() as session:
            for cid in ("cap-good", "cap-bad"):
                await CaptureRepository(session).create(Capture(
                    id=cid, user_id=mig.DEFAULT_USER_ID,
                    url="https://x", title="t", platform="general",
                    content_type="article",
                    captured_at="2026-04-01T00:00:00+00:00",
                    dwell_seconds=0, raw_metadata_json=None,
                ))
        return await mig._migrate_enrichments(
            captures_path=captures, enrichments_path=enrichments,
            failures_path=fail,
            user_id=mig.DEFAULT_USER_ID, dry_run=False, limit=None,
        )

    counts = asyncio.run(go())
    assert counts["failed"] == 1, "corrupt row should be counted as failed"
    assert counts["inserted"] == 1, "valid row after the corrupt one must still land"
    assert [k["capture_id"] for k in captured] == ["cap-good"]
    failures = [json.loads(l) for l in fail.read_text(encoding="utf-8").splitlines()]
    assert any("expected dict" in f["error_reason"] for f in failures)


# ---- --limit flag ----------------------------------------------------

def test_limit_caps_inserts_per_stage(tmp_path, monkeypatch):
    """--limit N stops the loop after N inserts. Documented but
    previously untested."""
    captures = tmp_path / "captures.jsonl"
    fail = tmp_path / "failures.jsonl"
    _write_jsonl(captures, [
        {
            "capture_id": f"cap-{i}", "url": f"https://wiki/{i}",
            "title": f"t{i}", "platform": "general", "content_type": "article",
            "clean_text": "hi", "text_source": "extension",
            "transcript": None, "image_descriptions": [], "image_text": "",
            "timestamp": "2026-04-01T00:00:00+00:00",
            "dwell_time_seconds": 0, "metadata": {},
        }
        for i in range(5)
    ])

    inserted_cids: list[str] = []

    async def fake_sync_capture(**kwargs):
        inserted_cids.append(kwargs["capture_id"])
        async with session_scope() as session:
            await CaptureRepository(session).create(Capture(
                id=kwargs["capture_id"], user_id=kwargs["user_id"],
                url=kwargs.get("url"), title=kwargs.get("title"),
                platform=kwargs.get("platform"),
                content_type=kwargs.get("content_type"),
                captured_at=kwargs["captured_at"],
                dwell_seconds=kwargs.get("dwell_seconds", 0),
                raw_metadata_json=kwargs.get("raw_metadata_json"),
            ))
        return True

    monkeypatch.setattr(mig, "sync_capture", fake_sync_capture)

    async def go():
        await _seed_user_and_init()
        return await mig._migrate_captures(
            captures_path=captures, failures_path=fail,
            user_id=mig.DEFAULT_USER_ID,
            dry_run=False, include_test_rows=False, limit=2,
        )

    counts = asyncio.run(go())
    assert counts["inserted"] == 2
    assert len(inserted_cids) == 2


# ---- storage_dual_write=False short-circuit -------------------------

def test_run_aborts_when_storage_dual_write_is_off(tmp_path, monkeypatch):
    """If the operator forgot to flip storage_dual_write=True before
    running a real migration, every sync_* call would no-op. run()
    must bail with rc=2 and a clear error message rather than chugging
    through the JSONLs and producing zero SQL rows."""
    monkeypatch.setattr(mig.settings, "storage_dual_write", False)

    rc = asyncio.run(mig.run(
        captures_path=tmp_path / "captures.jsonl",
        hydrations_path=tmp_path / "hydrations.jsonl",
        enrichments_path=tmp_path / "enrichments.jsonl",
        failures_path=tmp_path / "failures.jsonl",
        dry_run=False,
        include_test_rows=False,
        limit=None,
        verify_only=False,
    ))
    assert rc == 2


def test_run_proceeds_in_dry_run_even_with_dual_write_off(tmp_path, monkeypatch):
    """Dry-run is safe regardless of the flag — sync_* never fires."""
    monkeypatch.setattr(mig.settings, "storage_dual_write", False)
    # Empty inputs — should breeze through all three stages and return 0.
    rc = asyncio.run(mig.run(
        captures_path=tmp_path / "captures.jsonl",
        hydrations_path=tmp_path / "hydrations.jsonl",
        enrichments_path=tmp_path / "enrichments.jsonl",
        failures_path=tmp_path / "failures.jsonl",
        dry_run=True,
        include_test_rows=False,
        limit=None,
        verify_only=False,
    ))
    assert rc == 0


# ---- Legacy capture_id round-trip across stages 1 + 3 ---------------

def test_legacy_id_round_trips_across_stages(tmp_path, monkeypatch):
    """A pre-Phase-2 capture row (no capture_id) gets minted in stage 1
    and re-minted by stage 3's _build_capture_lookup. Both must arrive
    at the same id so the enrichment row joins correctly. Regression
    test for the duplicated id-derivation code path."""
    captures = tmp_path / "captures.jsonl"
    enrichments = tmp_path / "enrichments.jsonl"
    fail = tmp_path / "failures.jsonl"
    _write_jsonl(captures, [{
        # NO capture_id field — legacy row.
        "url": "https://en.wikipedia.org/wiki/Legacy",
        "title": "Legacy", "platform": "general", "content_type": "article",
        "clean_text": "hello", "text_source": "extension",
        "transcript": None, "image_descriptions": [], "image_text": "",
        "timestamp": "2026-04-01T00:00:00+00:00",
        "dwell_time_seconds": 0, "metadata": {},
    }])

    # Compute the id the migration WILL mint, so we can write the
    # matching enrichment row.
    minted = mig._mint_legacy_capture_id({
        "url": "https://en.wikipedia.org/wiki/Legacy",
        "timestamp": "2026-04-01T00:00:00+00:00",
    })
    _write_jsonl(enrichments, [{
        "capture_id": minted,
        "enriched_at": "2026-04-02T00:00:00+00:00",
        "model": "m",
        "enrichment": {"summary": "S", "topics": ["t"], "entities": [],
                       "key_facts": []},
    }])

    enrichment_calls: list[dict] = []

    async def fake_sync_capture(**kwargs):
        async with session_scope() as session:
            await CaptureRepository(session).create(Capture(
                id=kwargs["capture_id"], user_id=kwargs["user_id"],
                url=kwargs.get("url"), title=kwargs.get("title"),
                platform=kwargs.get("platform"),
                content_type=kwargs.get("content_type"),
                captured_at=kwargs["captured_at"],
                dwell_seconds=kwargs.get("dwell_seconds", 0),
                raw_metadata_json=kwargs.get("raw_metadata_json"),
            ))
        return True

    async def fake_sync_enrichment(**kwargs):
        enrichment_calls.append(kwargs)
        return True

    monkeypatch.setattr(mig, "sync_capture", fake_sync_capture)
    monkeypatch.setattr(mig, "sync_enrichment", fake_sync_enrichment)

    async def go():
        await _seed_user_and_init()
        c1 = await mig._migrate_captures(
            captures_path=captures, failures_path=fail,
            user_id=mig.DEFAULT_USER_ID,
            dry_run=False, include_test_rows=False, limit=None,
        )
        c3 = await mig._migrate_enrichments(
            captures_path=captures, enrichments_path=enrichments,
            failures_path=fail,
            user_id=mig.DEFAULT_USER_ID, dry_run=False, limit=None,
        )
        return c1, c3

    c1, c3 = asyncio.run(go())
    assert c1["minted_ids"] == 1
    assert c1["inserted"] == 1
    # The key assertion: stage 3 found the parent (so the two stages
    # agree on the minted id) and called sync_enrichment, NOT counted
    # the row as missing_parent.
    assert c3["missing_parent"] == 0
    assert c3["inserted"] == 1
    assert enrichment_calls[0]["capture_id"] == minted


# ---- _verify subcommand ----------------------------------------------

def test_verify_returns_zero_when_sql_matches_jsonl(tmp_path, monkeypatch):
    """Happy path: SQL captures count >= kept JSONL captures count
    (test-fixture skipping is the only allowed gap). _verify exits 0."""
    captures = tmp_path / "captures.jsonl"
    hyd = tmp_path / "hydrations.jsonl"
    enr = tmp_path / "enrichments.jsonl"
    _write_jsonl(captures, [{
        "capture_id": f"cap-{i}", "url": f"https://wiki/{i}",
        "title": f"t{i}", "platform": "general", "content_type": "article",
        "clean_text": "hi", "text_source": "extension",
        "transcript": None, "image_descriptions": [], "image_text": "",
        "timestamp": "2026-04-01T00:00:00+00:00",
        "dwell_time_seconds": 0, "metadata": {},
    } for i in range(3)])

    async def go():
        await _seed_user_and_init()
        async with session_scope() as session:
            for i in range(3):
                await CaptureRepository(session).create(Capture(
                    id=f"cap-{i}", user_id=mig.DEFAULT_USER_ID,
                    url=f"https://wiki/{i}", title=f"t{i}",
                    platform="general", content_type="article",
                    captured_at="2026-04-01T00:00:00+00:00",
                    dwell_seconds=0, raw_metadata_json=None,
                ))
        return await mig._verify(
            captures_path=captures,
            hydrations_path=hyd,
            enrichments_path=enr,
            user_id=mig.DEFAULT_USER_ID,
            include_test_rows=False,
        )

    rc = asyncio.run(go())
    assert rc == 0


def test_verify_returns_nonzero_when_sql_short(tmp_path, monkeypatch):
    """Migration partially failed — JSONL has 3 kept captures but only
    1 made it to SQL. _verify must exit non-zero so the operator notices."""
    captures = tmp_path / "captures.jsonl"
    hyd = tmp_path / "hydrations.jsonl"
    enr = tmp_path / "enrichments.jsonl"
    _write_jsonl(captures, [{
        "capture_id": f"cap-{i}", "url": f"https://wiki/{i}",
        "title": f"t{i}", "platform": "general", "content_type": "article",
        "clean_text": "hi", "text_source": "extension",
        "transcript": None, "image_descriptions": [], "image_text": "",
        "timestamp": "2026-04-01T00:00:00+00:00",
        "dwell_time_seconds": 0, "metadata": {},
    } for i in range(3)])

    async def go():
        await _seed_user_and_init()
        # Only seed ONE capture — the other two are "missing".
        async with session_scope() as session:
            await CaptureRepository(session).create(Capture(
                id="cap-0", user_id=mig.DEFAULT_USER_ID,
                url="https://wiki/0", title="t0",
                platform="general", content_type="article",
                captured_at="2026-04-01T00:00:00+00:00",
                dwell_seconds=0, raw_metadata_json=None,
            ))
        return await mig._verify(
            captures_path=captures,
            hydrations_path=hyd,
            enrichments_path=enr,
            user_id=mig.DEFAULT_USER_ID,
            include_test_rows=False,
        )

    rc = asyncio.run(go())
    assert rc == 1
