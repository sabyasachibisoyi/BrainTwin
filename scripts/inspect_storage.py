"""Inspect what's actually landed in SQL + Chroma.

A practical replacement for "install pgAdmin / install a Chroma GUI"
since:
  - pgAdmin is for Postgres; you're on SQLite locally, so use DB
    Browser for SQLite (https://sqlitebrowser.org) or DBeaver
    Community (free, supports SQLite + Postgres in one tool — worth
    installing now since Phase 3.5 will move to Postgres anyway).
  - ChromaDB has no widely-trusted GUI. So this script opens the same
    PersistentClient your live code uses and prints what's inside.

Usage:
    # Default — show SQL counts + samples + Chroma counts + samples
    python scripts/inspect_storage.py

    # Just SQL
    python scripts/inspect_storage.py --no-chroma

    # Just Chroma
    python scripts/inspect_storage.py --no-sql

    # Peek at one specific capture across SQL + Chroma
    python scripts/inspect_storage.py --capture-id <uuid>

    # Show more samples per table (default 3)
    python scripts/inspect_storage.py --samples 10

This script is read-only. It never inserts, deletes, or alters
anything. Safe to run on the live DB.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import settings  # noqa: E402
from backend.storage.db import session_scope  # noqa: E402
from backend.storage.sync import DEFAULT_USER_ID  # noqa: E402


# ---- Helpers --------------------------------------------------------

def _hr(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def _truncate(s: Optional[str], n: int = 70) -> str:
    if s is None:
        return "(none)"
    s = str(s).replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ---- SQL inspector --------------------------------------------------

async def _inspect_sql(*, samples: int, capture_id: Optional[str]) -> None:
    from sqlalchemy import func, select

    from backend.storage.schema import (
        captures, chunks, chunk_entities, chunk_topics,
        entities, enrichments, hydrations, topics, users,
    )

    _hr(f"SQL — {settings.database_url}")

    # ---- Row counts (whole DB, not user-scoped — top-level health) -
    print("\nRow counts (whole DB):")
    async with session_scope() as session:
        for tbl in (users, captures, hydrations, enrichments, chunks,
                    topics, entities, chunk_topics, chunk_entities):
            cnt = (await session.execute(
                select(func.count()).select_from(tbl)
            )).scalar_one()
            print(f"  {tbl.name:<18} {cnt:>6}")

    # ---- Per-user breakdown for Sabya ------------------------------
    print(f"\nRow counts (user_id={DEFAULT_USER_ID}, joined through captures):")
    async with session_scope() as session:
        cap_cnt = (await session.execute(
            select(func.count()).select_from(captures)
            .where(captures.c.user_id == DEFAULT_USER_ID)
        )).scalar_one()
        hyd_cnt = (await session.execute(
            select(func.count()).select_from(hydrations)
            .join(captures, hydrations.c.capture_id == captures.c.id)
            .where(captures.c.user_id == DEFAULT_USER_ID)
        )).scalar_one()
        enr_cnt = (await session.execute(
            select(func.count()).select_from(enrichments)
            .join(captures, enrichments.c.capture_id == captures.c.id)
            .where(captures.c.user_id == DEFAULT_USER_ID)
        )).scalar_one()
        chk_cnt = (await session.execute(
            select(func.count()).select_from(chunks)
            .join(captures, chunks.c.capture_id == captures.c.id)
            .where(captures.c.user_id == DEFAULT_USER_ID)
        )).scalar_one()
        print(f"  captures      {cap_cnt:>6}")
        print(f"  hydrations    {hyd_cnt:>6}")
        print(f"  enrichments   {enr_cnt:>6}")
        print(f"  chunks        {chk_cnt:>6}")

    # ---- Capture-id deep-dive --------------------------------------
    if capture_id:
        print(f"\nDeep dive: capture_id={capture_id}")
        async with session_scope() as session:
            cap_row = (await session.execute(
                select(captures).where(captures.c.id == capture_id)
            )).first()
            if cap_row is None:
                print("  not found in SQL.")
                return
            print(f"  url       : {_truncate(cap_row.url, 80)}")
            print(f"  title     : {_truncate(cap_row.title, 80)}")
            print(f"  platform  : {cap_row.platform}")
            print(f"  captured  : {cap_row.captured_at}")
            print(f"  user_id   : {cap_row.user_id}")

            hyds = (await session.execute(
                select(hydrations).where(hydrations.c.capture_id == capture_id)
                .order_by(hydrations.c.hydrated_at.asc())
            )).all()
            print(f"  hydrations: {len(hyds)}")
            for h in hyds:
                print(f"    - tier={h.tier} at={h.hydrated_at}")

            enrs = (await session.execute(
                select(enrichments).where(enrichments.c.capture_id == capture_id)
            )).all()
            print(f"  enrichments: {len(enrs)}")
            for e in enrs:
                print(f"    - model={e.model}")
                print(f"      summary={_truncate(e.summary, 100)}")

            chks = (await session.execute(
                select(chunks.c.id, chunks.c.chunk_index, chunks.c.source_kind,
                       chunks.c.text)
                .where(chunks.c.capture_id == capture_id)
                .order_by(chunks.c.chunk_index.asc())
            )).all()
            print(f"  chunks    : {len(chks)}")
            for c in chks:
                print(f"    - #{c.chunk_index} [{c.source_kind}] "
                      f"{_truncate(c.text, 70)}")
        return

    # ---- Sample rows (most recent N) -------------------------------
    print(f"\nMost recent {samples} captures (user_id={DEFAULT_USER_ID}):")
    async with session_scope() as session:
        rows = (await session.execute(
            select(
                captures.c.id, captures.c.url, captures.c.title,
                captures.c.platform, captures.c.captured_at,
            )
            .where(captures.c.user_id == DEFAULT_USER_ID)
            .order_by(captures.c.captured_at.desc())
            .limit(samples)
        )).all()
        for r in rows:
            print(f"  {r.id[:8]} | {r.platform or '?':<10} | "
                  f"{_truncate(r.title or r.url, 70)}")

    print(f"\nMost recent {samples} enrichments:")
    async with session_scope() as session:
        rows = (await session.execute(
            select(
                enrichments.c.capture_id, enrichments.c.model,
                enrichments.c.enriched_at, enrichments.c.summary,
            )
            .join(captures, enrichments.c.capture_id == captures.c.id)
            .where(captures.c.user_id == DEFAULT_USER_ID)
            .order_by(enrichments.c.enriched_at.desc())
            .limit(samples)
        )).all()
        for r in rows:
            print(f"  {r.capture_id[:8]} | {r.model or '?':<22} | "
                  f"{_truncate(r.summary, 70)}")

    print(f"\nTop topics (any user — topics are global per B.3):")
    async with session_scope() as session:
        rows = (await session.execute(
            select(
                topics.c.slug, topics.c.label,
                func.count(chunk_topics.c.chunk_id).label("usage"),
            )
            .outerjoin(chunk_topics, chunk_topics.c.topic_id == topics.c.id)
            .group_by(topics.c.id)
            .order_by(func.count(chunk_topics.c.chunk_id).desc())
            .limit(samples)
        )).all()
        for r in rows:
            print(f"  {r.usage:>4}× {r.slug:<28} ({r.label})")

    print(f"\nTop entities (any user):")
    async with session_scope() as session:
        rows = (await session.execute(
            select(
                entities.c.slug, entities.c.label, entities.c.entity_type,
                func.count(chunk_entities.c.chunk_id).label("usage"),
            )
            .outerjoin(
                chunk_entities, chunk_entities.c.entity_id == entities.c.id,
            )
            .group_by(entities.c.id)
            .order_by(func.count(chunk_entities.c.chunk_id).desc())
            .limit(samples)
        )).all()
        for r in rows:
            print(f"  {r.usage:>4}× {r.slug:<28} "
                  f"({r.entity_type:<8}) {r.label}")


# ---- Chroma inspector -----------------------------------------------

def _inspect_chroma(*, samples: int, capture_id: Optional[str]) -> None:
    try:
        import chromadb
    except ImportError:
        print("\n(chromadb not installed — pip install chromadb)")
        return

    chroma_path = Path(getattr(settings, "chroma_path", "data/chroma"))
    _hr(f"Chroma — {chroma_path}")

    if not chroma_path.exists():
        print(f"\n{chroma_path} doesn't exist yet. Run a capture or the "
              f"migration to populate it.")
        return

    client = chromadb.PersistentClient(path=str(chroma_path))

    collections = client.list_collections()
    if not collections:
        print("\nNo collections found.")
        return

    print(f"\n{len(collections)} collection(s):")
    for col in collections:
        # In recent chromadb, list_collections returns Collection objects
        # with a name attribute; older versions return dicts. Handle both.
        name = getattr(col, "name", None) or (col.get("name") if isinstance(col, dict) else None) or str(col)
        try:
            handle = client.get_collection(name=name)
            cnt = handle.count()
        except Exception as e:  # noqa: BLE001
            print(f"  {name}: (could not open: {e})")
            continue
        print(f"  {name:<10} {cnt:>6} vectors")

    if capture_id:
        print(f"\nDeep dive (Chroma): capture_id={capture_id}")
        try:
            chunks_col = client.get_collection(name="chunks")
            res = chunks_col.get(
                where={"capture_id": capture_id},
                include=["metadatas", "documents"],
            )
            ids = res.get("ids") or []
            docs = res.get("documents") or []
            metas = res.get("metadatas") or []
            print(f"  chunks collection: {len(ids)} vector(s)")
            for i, (cid, d, m) in enumerate(zip(ids, docs, metas)):
                print(f"    [{i}] id={cid} src={m.get('source_kind')}")
                print(f"        {_truncate(d, 80)}")
        except Exception as e:  # noqa: BLE001
            print(f"  (could not query chunks: {e})")
        return

    # Generic samples per collection.
    print(f"\nSample (up to {samples} per collection):")
    for col in collections:
        name = getattr(col, "name", None) or (col.get("name") if isinstance(col, dict) else None) or str(col)
        try:
            handle = client.get_collection(name=name)
            res = handle.peek(limit=samples)
        except Exception as e:  # noqa: BLE001
            print(f"  {name}: (peek failed: {e})")
            continue
        ids = res.get("ids") or []
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        print(f"\n  {name}:")
        if not ids:
            print("    (empty)")
            continue
        for cid, d, m in zip(ids, docs, metas):
            extra = ""
            if isinstance(m, dict):
                bits = []
                for k in ("source_kind", "user_id", "slug", "label", "entity_type"):
                    if k in m:
                        bits.append(f"{k}={m[k]}")
                if bits:
                    extra = " | " + " ".join(bits)
            print(f"    id={cid}{extra}")
            if d:
                print(f"      {_truncate(d, 80)}")


# ---- Main -----------------------------------------------------------

async def _run(args: argparse.Namespace) -> int:
    if not args.no_sql:
        try:
            await _inspect_sql(samples=args.samples, capture_id=args.capture_id)
        except Exception as e:  # noqa: BLE001
            print(f"\nSQL inspection failed: {e}")

    if not args.no_chroma:
        try:
            _inspect_chroma(samples=args.samples, capture_id=args.capture_id)
        except Exception as e:  # noqa: BLE001
            print(f"\nChroma inspection failed: {e}")

    print()
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Read-only inspector for SQL + Chroma stores."
    )
    p.add_argument("--no-sql", action="store_true", help="Skip SQL.")
    p.add_argument("--no-chroma", action="store_true", help="Skip Chroma.")
    p.add_argument("--samples", type=int, default=3,
                   help="Rows / vectors to show per section (default: 3).")
    p.add_argument("--capture-id", default=None,
                   help="Drill into one specific capture across SQL + Chroma.")
    args = p.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
