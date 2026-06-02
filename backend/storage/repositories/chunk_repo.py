"""ChunkRepository — the retrieval unit (β, A.3).

Owns the `chunks` table and the two junction tables (`chunk_topics`,
`chunk_entities`). Junction operations live here rather than in their
own repos because they're always invoked alongside chunk creation —
this keeps the call sites tight.

Vector / similarity search lives in `backend.storage.vector_store`
(Step 2), not here. ChunkRepository only handles SQL operations on
the chunks table and its junctions.
"""

from __future__ import annotations

from sqlalchemy import insert, select, text

from backend.storage.models import Chunk, ChunkAttachment, ChunkInsert, ChunkWithScore
from backend.storage.repositories.base import BaseRepository
from backend.storage.schema import (
    captures,
    chunk_entities,
    chunk_topics,
    chunks,
)


def _row_to_chunk(row) -> Chunk:
    return Chunk(
        id=row.id,
        capture_id=row.capture_id,
        chunk_index=row.chunk_index,
        text=row.text,
        source_kind=row.source_kind,
        embedding=row.embedding,
    )


class ChunkRepository(BaseRepository):
    async def create_many(self, chunks_to_insert: list[ChunkInsert]) -> list[int]:
        """Batch insert. Returns the new chunk ids in input order.

        SQLAlchemy's `RETURNING` works on both PostgreSQL and modern
        SQLite (3.35+), so we lean on it. Caller has already verified
        the parent capture belongs to the right tenant.
        """
        if not chunks_to_insert:
            return []
        rows = [
            {
                "capture_id": c.capture_id,
                "chunk_index": c.chunk_index,
                "text": c.text,
                "source_kind": c.source_kind,
                "embedding": c.embedding,
            }
            for c in chunks_to_insert
        ]
        result = await self.session.execute(
            insert(chunks).returning(chunks.c.id),
            rows,
        )
        return [int(r[0]) for r in result]

    async def list_by_capture(
        self,
        capture_id: str,
        *,
        user_id: int,
    ) -> list[Chunk]:
        """All chunks for one capture, in order. Tenant-checked via the
        join to captures so a leaked capture_id can't reach another
        tenant's chunks."""
        result = await self.session.execute(
            select(chunks)
            .join(captures, chunks.c.capture_id == captures.c.id)
            .where(captures.c.user_id == user_id)
            .where(chunks.c.capture_id == capture_id)
            .order_by(chunks.c.chunk_index.asc())
        )
        return [_row_to_chunk(row) for row in result]

    async def get_by_ids(
        self,
        chunk_ids: list[int],
        *,
        user_id: int,
    ) -> list[Chunk]:
        """Fetch a list of chunks by id, tenant-checked. Used by the
        Phase 4 retrieval pipeline: VectorStore returns chunk_ids by
        similarity, then we hydrate with this method to get text +
        metadata.

        Returns rows in arbitrary order (DB order). Caller re-sorts
        by similarity score if needed."""
        if not chunk_ids:
            return []
        result = await self.session.execute(
            select(chunks)
            .join(captures, chunks.c.capture_id == captures.c.id)
            .where(captures.c.user_id == user_id)
            .where(chunks.c.id.in_(chunk_ids))
        )
        return [_row_to_chunk(row) for row in result]

    # ---- Phase 4 M.1 — BM25 full-text retrieval ----------------------

    async def search_by_bm25(
        self,
        query: str,
        *,
        user_id: int,
        limit: int = 20,
    ) -> list[ChunkWithScore]:
        """Full-text BM25 search over `chunks.text` via the FTS5
        virtual table `chunks_fts`. Phase 4 M.1 — paired with Chroma's
        vector search in M.2 to form the hybrid retrieval pipeline.

        Tenant-isolated via the JOIN to `captures` on `user_id` —
        chunks belonging to other users cannot surface in this user's
        results.

        Score convention: SQLite's `bm25()` function returns negative
        values by convention (lower = better). We flip the sign before
        returning so `ChunkWithScore.score` follows the HIGHER-is-better
        convention, which matches what callers expect when fusing with
        a vector-similarity ranker (M.2 RRF) or sorting for display.

        Empty / whitespace-only queries return [] without touching the
        DB — FTS5's MATCH would error on a blank query and the caller
        gets a cleaner contract.

        Raw SQL via `text()` because SQLAlchemy Core doesn't model the
        FTS5 `MATCH` operator. Parameters are still bound — no string
        interpolation of `query` into the SQL.
        """
        if not query or not query.strip():
            return []
        if limit <= 0:
            return []
        sql = text(
            """
            SELECT
                c.id            AS id,
                c.capture_id    AS capture_id,
                c.chunk_index   AS chunk_index,
                c.text          AS text,
                c.source_kind   AS source_kind,
                c.embedding     AS embedding,
                bm25(chunks_fts) AS bm25_score
            FROM chunks_fts
            JOIN chunks   AS c   ON c.id = chunks_fts.rowid
            JOIN captures AS cap ON cap.id = c.capture_id
            WHERE chunks_fts MATCH :query
              AND cap.user_id = :user_id
            ORDER BY bm25(chunks_fts)
            LIMIT :limit
            """
        )
        result = await self.session.execute(
            sql,
            {"query": query, "user_id": user_id, "limit": limit},
        )
        out: list[ChunkWithScore] = []
        for row in result:
            chunk = Chunk(
                id=row.id,
                capture_id=row.capture_id,
                chunk_index=row.chunk_index,
                text=row.text,
                source_kind=row.source_kind,
                embedding=row.embedding,
            )
            # SQLite returns bm25() as a non-positive float (lower =
            # better). Flip the sign so higher = better, matching the
            # ChunkWithScore convention.
            out.append(ChunkWithScore(chunk=chunk, score=-float(row.bm25_score)))
        return out

    # ---- Junction-table operations -----------------------------------

    async def attach_topics(
        self,
        chunk_id: int,
        topic_ids_with_confidence: list[tuple[int, float | None]],
    ) -> None:
        """Tag a chunk with one or more topics. Rows that already
        exist (same (chunk_id, topic_id)) are silently ignored — this
        method is idempotent so re-enriching the same content doesn't
        produce duplicate junction rows."""
        if not topic_ids_with_confidence:
            return
        rows = [
            {"chunk_id": chunk_id, "topic_id": topic_id, "confidence": conf}
            for topic_id, conf in topic_ids_with_confidence
        ]
        # SQLAlchemy doesn't have a portable INSERT OR IGNORE, but we can
        # achieve idempotency with a manual existence check followed by
        # a filtered insert. For batch sizes typical in enrichment
        # (~5-10 topics per chunk), this is fast enough and works
        # uniformly on SQLite + Postgres.
        existing = await self.session.execute(
            select(chunk_topics.c.topic_id)
            .where(chunk_topics.c.chunk_id == chunk_id)
        )
        already = {r[0] for r in existing}
        new_rows = [r for r in rows if r["topic_id"] not in already]
        if new_rows:
            await self.session.execute(insert(chunk_topics), new_rows)

    async def attach_entities(
        self,
        chunk_id: int,
        attachments: list[ChunkAttachment],
    ) -> None:
        """Tag a chunk with one or more entity mentions. Each mention
        gets its own row (multiple mentions of the same entity in the
        same chunk are allowed — distinguished by mention_position).

        Idempotent like attach_topics: same (chunk_id, entity_id,
        mention_position) tuple won't duplicate."""
        if not attachments:
            return
        rows = [
            {
                "chunk_id": chunk_id,
                "entity_id": a.entity_id,
                "mention_position": a.mention_position,
                "confidence": a.confidence,
            }
            for a in attachments
        ]
        existing = await self.session.execute(
            select(
                chunk_entities.c.entity_id,
                chunk_entities.c.mention_position,
            ).where(chunk_entities.c.chunk_id == chunk_id)
        )
        already = {(r[0], r[1]) for r in existing}
        new_rows = [
            r for r in rows
            if (r["entity_id"], r["mention_position"]) not in already
        ]
        if new_rows:
            await self.session.execute(insert(chunk_entities), new_rows)
