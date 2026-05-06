# Phase 3 — Storage Layer (SQL + Vector)

> **Status as of 2026-05-05 — LAYER A SIGNED OFF. LAYER B BLOCKERS RESOLVED (B.1, B.2, B.3, B.5, B.7 ALL LOCKED). READY TO CODE.**
>
> Phase 3 is the storage layer that finally absorbs the three sidecar JSONLs (`captures.jsonl`, `enrichments.jsonl`, `hydrations.jsonl`) into proper joined storage backed by SQL + vector search. It also makes BrainTwin **multi-user from day one**, because the use cases below extend beyond a single personal twin.
>
> All Phase-3-blocking decisions are signed off as of 2026-05-05. The remaining open Layer B items (B.4 pgvector index, B.6 embedding regeneration, B.8 hybrid retrieval, B.9 graph storage) are intentionally deferred per the rationale in each section — none block implementation.
>
> **Locked decisions at a glance:**
> - **A.1** SQL with SQLite-now / Postgres-later (ChromaDB stays in cloud through Phase 5; pgvector swap is Phase 6+ if/when it hurts)
> - **A.2** Multi-tenant from day one
> - **A.3** β-with-γ-flavor schema (chunks + first-class topics/entities, no curated edge tables in v1)
> - **A.4** Schema (9 tables, including embedding columns on chunks + topics + entities)
> - **A.5** Chunking by source kind (paragraph for articles, chapter-aware for transcripts, fixed token windows as fallback)
> - **A.6** Embedding: `sentence-transformers/all-MiniLM-L6-v2` (384 dims, cosine)
> - **A.7** Cloud / free-tier compatibility (containerize, env vars, no vendor SDKs in storage layer)
> - **B.1** Dual-write for ~2 weeks, then Phase 3.5 commit removes JSONL writers
> - **B.2** Repository pattern + SQLAlchemy Core + thin VectorStore wrapper
> - **B.3** Three ChromaDB collections — `chunks` (per-user-filtered), `topics` and `entities` (shared global vocabulary)
> - **B.5** `scripts/migrate_jsonl_to_sql.py` — idempotent, streaming + batched commits, single-user mapping for historical data
> - **B.7** Controlled vocabulary that grows organically — LLM constrained to reuse existing topics/entities via embedding-similarity shortlist, may only coin new slugs above 0.75 cosine threshold

---

## What Phase 3 is for — the three use cases that drive it

BrainTwin's scope expanded during the Phase 3 interview from "personal knowledge twin for one user" to a research-and-learning platform with three layered use cases. The storage layer is designed to support all three with the same underlying data.

### A — Synthesis quiz for learners (build first)

A student consumes content (articles, reels, videos). BrainTwin can either **generate** a quiz that recombines 3-4 different concepts the student encountered, or **answer** a teacher-posed quiz by composing across what was read. Multi-user from the start — students log in, see only their own corpus, get quizzed only on what they've consumed. This is BrainTwin as a teaching tool.

### B — Vague-recall search (build second)

A student remembers fragments ("there was something about hash collisions a few weeks ago…"), and asks BrainTwin to find the source. Single-user feel; depends on strong semantic retrieval. This is BrainTwin as a memory prosthetic.

### C — Indirect-clue inference game (build third)

A third party gives layered, culturally loaded, sarcastic clues; BrainTwin (and a human in parallel) try to converge on the answer. Stress-tests how Claude reasons across the personal corpus + general world knowledge under indirection (cf. Anthropic, 2025, *Emotion concepts function in language models*).

C reuses the storage of A+B — it's the *hardest mode* of the same retrieval-and-reasoning capability, not a separate system. The main lift for C lives in Phase 4 (agent prompt design + multi-hop tool use), not Phase 3.

**Build order: A → B → C.** The storage decisions below are sized to support all three.

---

## Layer A — locked decisions

### A.1 — SQL with SQLite-now / Postgres-later

Phase 3 uses SQL, not NoSQL. Reasoning:

- BrainTwin's queries are exploratory ("find captures touching these 3 topics from the last week, grouped by entity"), not key-value lookups. SQL's flexible joins + ad-hoc WHERE clauses are exactly what synthesis quizzes (use case A) and multi-hop inference (use case C) need.
- Projected scale through the next 12-24 months (single user → small student cohort) sits well within a single PostgreSQL instance's comfort zone. No genuine need for NoSQL's horizontal-scaling story until ~100K users.
- Vector search lives natively alongside relational data via `pgvector` — same database, same transaction semantics, no separate Pinecone/Weaviate to operate.

**Implementation path** (revised 2026-05-05 — Sabya wants production experience with ChromaDB before any pgvector migration):

- **Phase 3 (now):** SQLite + ChromaDB, both file-backed in `data/`. Zero ops, runs on Sabya's laptop.
- **Cloud (Phase ~5, first multi-user deployment):** managed Postgres (Supabase free / Aurora Serverless v2 / Neon free / Oracle Cloud Free + self-hosted) **plus ChromaDB self-hosted on the same VM or via Chroma's hosted offering.** Single migration step: SQL only. Vector path stays the same.
- **pgvector swap (Phase 6+, only if/when it hurts):** consolidate the vector store into Postgres via pgvector. Triggered by one of: hybrid retrieval (B.8) becoming a priority, operational cost of running two stores becoming material, or single-query joins becoming a performance bottleneck. The repository pattern (B.2) keeps this swap behind one method signature with no call-site churn.

The schema we write today must use only SQL features that work in both SQLite and PostgreSQL (avoid SQLite-specific quirks like type-affinity tricks, avoid Postgres-specific features like LATERAL joins or JSONB-specific operators in v1).

### A.2 — Multi-tenant from day one

Every domain table carries a `user_id` (or descends from a row that does). Reasoning:

- Use case A means real students with real accounts. Retrofitting tenancy after data exists is painful (cross-row data leak risk, every query becomes a migration).
- Single-user mode is just `user_id = 1` until other users exist. Zero overhead during dev.

### A.3 — Hybrid chunk-and-graph schema (β with γ flavor)

Locked schema shape: chunks are the unit of retrieval (β), but topics and entities are first-class tables (γ flavor) so we can filter, aggregate, and join by them.

What we explicitly **do NOT** build in v1:

- Curated edge tables (`entity_relations`, `topic_hierarchies`). Most multi-hop reasoning is better done by Claude at query time over retrieved chunks than by graph traversal over hand-curated edges. We add edge tables only if the agent's reasoning quality demonstrates it would actually improve quiz accuracy — likely Phase 5 or 6.
- Entity normalization / dedup ("Bengaluru" / "Bangalore" merged). Defer to Phase 4 — fix it when retrieval surfaces duplicate-entity problems with real data.
- Cross-capture `related_captures` field deferred from Phase 2 Decision I. Same reasoning — fix it when retrieval shows the need.

### A.4 — Schema (first cut, locked enough to build against)

```sql
-- Users (multi-tenant from day one — A.2)
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    display_name TEXT,
    created_at TEXT NOT NULL                     -- ISO 8601
);

-- Captures (lifted from captures.jsonl, capture_id stays the join key)
CREATE TABLE captures (
    id TEXT PRIMARY KEY,                         -- UUID4 from extension/bot
    user_id INTEGER NOT NULL REFERENCES users(id),
    url TEXT,
    title TEXT,
    platform TEXT,
    content_type TEXT,
    captured_at TEXT NOT NULL,                   -- ISO 8601
    dwell_seconds INTEGER NOT NULL DEFAULT 0,
    raw_metadata_json TEXT                       -- full bot/extension payload (audit)
);

-- Hydrations (lifted from hydrations.jsonl — Phase 2.5 sidecar)
CREATE TABLE hydrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id TEXT NOT NULL REFERENCES captures(id),
    tier TEXT NOT NULL,                          -- "og_metadata" | "video_transcript"
    source_payload_json TEXT,                    -- full sidecar row from Phase 2.5
    hydrated_at TEXT NOT NULL
);

-- Enrichments (lifted from enrichments.jsonl)
CREATE TABLE enrichments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id TEXT NOT NULL REFERENCES captures(id),
    summary TEXT,
    key_facts_json TEXT,                         -- JSON array
    model TEXT,
    enriched_at TEXT NOT NULL
);

-- Chunks — the retrieval unit (β)
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id TEXT NOT NULL REFERENCES captures(id),
    chunk_index INTEGER NOT NULL,                -- 0-based ordering within capture
    text TEXT NOT NULL,
    source_kind TEXT NOT NULL,                   -- "article_paragraph" | "transcript_segment"
                                                 -- | "image_caption" | "summary"
    embedding BLOB,                              -- SQLite: BLOB
                                                 -- Postgres: VECTOR(384) via pgvector
    UNIQUE(capture_id, chunk_index)
);

-- Topics + entities as first-class tables (γ flavor)
-- NOTE (B.7): both are SHARED GLOBAL VOCABULARY across users — no user_id column.
-- "Kanban" coined by one student is available for reuse by every other student.
-- Tenant isolation comes from the junction tables joining to chunks (which have user_id via captures).
-- The `embedding` column is required by B.7's controlled-vocabulary approach: when enrichment
-- runs, we look up top-K topics whose embeddings are most similar to the new capture's summary
-- and prompt the LLM to prefer reuse over coinage.
CREATE TABLE topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,                   -- "kanban", "machine-learning"
    label TEXT NOT NULL,                         -- "Kanban", "Machine Learning"
    description TEXT,
    embedding BLOB,                              -- of (label + " " + description), 384-dim
                                                 -- mirrored into the `topics` ChromaDB collection
                                                 -- for fast similarity lookup
    coined_at TEXT NOT NULL                      -- ISO 8601 — when this topic first appeared
);

CREATE TABLE entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,                   -- "anthropic", "deepika-padukone"
    label TEXT NOT NULL,
    entity_type TEXT NOT NULL,                   -- "person" | "place" | "company" | "concept"
    embedding BLOB,                              -- mirrored into `entities` ChromaDB collection
    coined_at TEXT NOT NULL
);

-- Junction tables (chunk-level tagging)
CREATE TABLE chunk_topics (
    chunk_id INTEGER NOT NULL REFERENCES chunks(id),
    topic_id INTEGER NOT NULL REFERENCES topics(id),
    confidence REAL,                             -- 0-1, from enrichment LLM
    PRIMARY KEY (chunk_id, topic_id)
);

CREATE TABLE chunk_entities (
    chunk_id INTEGER NOT NULL REFERENCES chunks(id),
    entity_id INTEGER NOT NULL REFERENCES entities(id),
    confidence REAL,
    mention_position INTEGER,                    -- offset in chunk.text where mentioned
    PRIMARY KEY (chunk_id, entity_id, mention_position)
);

-- Indexes (the obvious ones)
CREATE INDEX idx_captures_user_id        ON captures(user_id);
CREATE INDEX idx_captures_captured_at    ON captures(captured_at);
CREATE INDEX idx_captures_platform       ON captures(platform);
CREATE INDEX idx_chunks_capture_id       ON chunks(capture_id);
CREATE INDEX idx_chunks_source_kind      ON chunks(source_kind);
CREATE INDEX idx_hydrations_capture_id   ON hydrations(capture_id);
CREATE INDEX idx_enrichments_capture_id  ON enrichments(capture_id);
CREATE INDEX idx_chunk_topics_topic_id   ON chunk_topics(topic_id);
CREATE INDEX idx_chunk_entities_entity_id ON chunk_entities(entity_id);
```

**Notes on the schema:**

- `captures.id` stays a TEXT UUID (matches today's `capture_id`) so existing JSONL rows migrate cleanly.
- `embedding` is BLOB in SQLite (we serialize the float32 array ourselves), VECTOR(384) in Postgres via pgvector. The data-access layer abstracts the difference.
- `chunks` has a UNIQUE constraint on `(capture_id, chunk_index)` so re-running enrichment doesn't create duplicates.
- `chunk_entities` includes `mention_position` so we can highlight WHERE in the chunk an entity is mentioned (useful for UI later, not used in v1 retrieval).

### A.5 — Chunking strategy (locked, but tunable in implementation)

**Rule of thumb:** if the source provides topic boundaries, use them. Otherwise, fall back to fixed-token windows. Topic-aligned chunks retrieve better than mid-thought cuts (e.g., a YouTube chapter mapped to one chunk preserves the chapter's coherence; a 256-token window may slice through the middle of an explanation).

Different content types get different chunking treatments:

| Source kind | Strategy | Rationale |
|---|---|---|
| Article body (Chrome extension) | Paragraph-level (split on `\n\n+`) | Articles already have semantic paragraph breaks |
| Transcript with chapter markers (YouTube `chapters`, podcast chapter metadata) | One chunk per chapter, up to ~800 tokens; sub-split long chapters with token windows | Topic-aligned chunks retrieve better than fixed cuts. yt-dlp already extracts the `chapters` field for us in Phase 2.5; we just need to read it. Educational content (Veritasium, Crash Course, MKBHD reviews) almost always has chapters; vlogs and reels rarely do. |
| Transcript without chapters, short (<2 min) | Whole thing, one chunk | IG / FB reels, TikToks — too short to benefit from chunking |
| Transcript without chapters, longer | Fixed token window (~256 tokens, ~64-token overlap) | Fallback for long transcripts where no topic boundaries exist; overlap preserves context across chunk boundaries |
| Image caption / OG description | Whole thing, one chunk | Already short |
| Enrichment summary | Whole thing, one chunk | Already a tight summary |

Semantic chunking (using a small LLM to detect topic boundaries) is **out of scope for v1**. Add later if retrieval quality is poor with the rules above. Apple Podcasts / Spotify chapter markers are increasingly common in newer podcasts (Dwarkesh Patel, Acquired, Lex Fridman); we should pick those up too when present in the audio metadata.

### A.6 — Embedding model

**Locked v1 choice: `sentence-transformers/all-MiniLM-L6-v2`** (384 dimensions, ~80 MB, English-leaning, ~15ms per chunk on M-series CPU).

Reasoning:
- Use cases A and B work fine with English-leaning embeddings — Sabya's MBA coursework + most consumed content is in English.
- C's reasoning happens at the LLM (Claude), not at the embedding layer, so embedding quality matters less.
- Free, local, no per-request cost, no vendor lock-in.

**Future upgrade path** (when use case A actually has multilingual student content): `BAAI/bge-m3` (1024 dims, multilingual including Hindi/Telugu/Odia, slower but still local) or Voyage AI's API (cheap, best-in-class retrieval, adds vendor dependency).

### A.7 — Cloud / free-tier compatibility

Phase 3 doesn't deploy to cloud — but the architectural decisions above must not preclude a free-tier cloud later. Concretely:

- **Containerize from day one.** A `Dockerfile` for the FastAPI app + Telegram bot, runnable on any provider.
- **All config via env vars.** No hardcoded paths. `DATABASE_URL` (single string) lets us swap SQLite ↔ Postgres without code changes — exactly the SQLAlchemy-dialect-agnostic pattern.
- **Avoid cloud-vendor SDKs in the storage layer.** Use SQL via SQLAlchemy/asyncpg, not boto3 / google-cloud-storage. Plain HTTP for everything else.
- **Design for one Postgres instance, not for distributed sharding.** Sharding-aware schemas (compound primary keys with tenant prefix) are real cost. Add only when load demands it.

Reference deployment options when we go to cloud:
- **Oracle Cloud Free** (4 vCPU + 24 GB RAM ARM VM, 200 GB disk, *forever* free — best ceiling for hobby/research). Run Postgres + ChromaDB + FastAPI + Telegram bot all on the one VM.
- **AWS Free Tier** (12 months: t3.micro EC2 + RDS Postgres + 20 GB; then ~$25/mo). Run ChromaDB on the EC2 instance alongside FastAPI; Postgres in RDS.
- **Supabase Free** (500 MB Postgres + auth, auto-pauses after 7 days idle) for the SQL side; ChromaDB self-hosted on a small VM elsewhere or Chroma's hosted offering.
- **Cloudflare tunnel + your laptop** for the very first months — genuinely $0 and zero ops, both stores stay on the laptop.

We deliberately do **not** migrate to pgvector at first cloud deployment. ChromaDB stays the vector store at least through Phase 5 — same engine end-to-end means one migration to plan (SQL only) and gives Sabya hands-on production experience with Chroma. pgvector swap is deferred to Phase 6+ and only happens if/when one of the triggers in A.1 materializes.

---

## Layer B — open questions (revisit after Sabya's reading)

These are intentionally deferred. Each requires familiarity with the chunking and vector-DB resources at the bottom before we can have a useful sign-off conversation.

### B.1 — Migration posture ✅ Locked (2026-05-05)

**Decision: Dual-write for ~2 weeks, then Phase 3.5 commit removes the JSONL writers.**

Mechanics:

1. **First Phase 3 commit** adds SQL + Chroma writers running in parallel with the existing JSONL writers. Both `/capture` (extension + bot path) and the enrichment worker write to both stores. New abstraction: a `CaptureWriter` interface with `JsonlWriter` and `SqlWriter` implementations; the entry point fan-outs to both.
2. **Backfill script** (B.5) reads existing JSONLs once into SQL + Chroma. Idempotent so it can be re-run.
3. **Live for 2 weeks of real usage.** Verify SQL queries return the same answers JSONL scans would have. Spot-check `/stats`, `/failures`, the new repository methods.
4. **Phase 3.5 commit** removes the JSONL writers. SQL becomes the sole path. JSONLs become read-only audit trail (gitignored, never written to again, kept on disk indefinitely as belt-and-suspenders).

**Why dual-write over hard cutover:**

- You have months of personal data already accumulated. Captures cannot be regenerated. If a migration bug corrupts data and we'd already cut over, recovery requires backup discipline we don't have set up.
- The added code is small — maybe 30 lines of fan-out glue. The cost of the safety net is low.
- Two-week cutover gives ~50-100 real captures of surface area, enough to validate the full pipeline (extension, bot, enrichment, hydration, all writers).
- Clean rollback path: comment out the SQL writer, keep JSONL writer, you're back to Phase 2 behavior with zero data loss.

**Why not derived view (JSONL primary, SQL as rebuilt index):**

- Commits us to JSONL forever as the source of truth — can never use SQL features that benefit from being authoritative (FK constraints, transactions across captures + chunks, etc.).
- Rebuild lag means queries can be stale by minutes/hours.
- Heavier architecture for less benefit than dual-write.

Hard cutover (one commit replaces JSONL with SQL) is defensible but the rollback story is materially worse, and the time savings are small (one commit vs two over a 2-week window).

### B.2 — Storage API design ✅ Locked (2026-05-05)

**Decision: Repository pattern + SQLAlchemy Core (not the Declarative ORM) + a thin `VectorStore` abstraction wrapping ChromaDB now / pgvector later.**

The shape:

```
backend/storage/
    db.py                       # SQLAlchemy engine + async session factory
    schema.py                   # Table definitions (Core, not Declarative)
    repositories/
        capture_repo.py         # CaptureRepository
        chunk_repo.py           # ChunkRepository
        enrichment_repo.py
        hydration_repo.py
        topic_repo.py           # TopicRepository, EntityRepository
    vector_store.py             # VectorStore interface + ChromaVectorStore impl
```

**Why repository over ORM or raw SQL:**

- Two storage backends (relational + vector) need to look unified to callers. Repository pattern hides both.
- We have at least one planned swap (SQLite → Postgres, A.1) and one possible swap (ChromaDB → pgvector, A.7 / Phase 6+). Repositories make either swap an internal change with zero call-site churn.
- Multi-tenant safety: every repository method takes `user_id` as a required keyword argument. Type system enforces what would otherwise be a code-review responsibility ("did you remember the WHERE clause?").
- Easy to mock for tests — `class FakeCaptureRepository` holding an in-memory dict. Same pattern as `StubLLMClient` from Phase 2.

**Why SQLAlchemy Core, not Declarative ORM:**

- We don't need ORM features (lazy loading, relationship traversal, identity maps, session-cached objects).
- Core is closer to a typed SQL builder — analogous to JPQL via SQLAlchemy expressions, with `text()` as the raw-SQL escape hatch (the Python equivalent of Spring Data's `@Query(nativeQuery = true)`).
- Rows return as dict-like records, which Pydantic models can adopt for response shapes. No mapping overhead.
- Postgres dialect handling is built-in, so SQLite → Postgres migration is mostly a connection string change.

**Vector metadata schema** (what each Chroma vector carries):

```python
chroma_collection.add(
    ids=[str(chunk.id)],
    embeddings=[chunk_embedding],
    documents=[chunk.text],                       # enables where_document filtering
    metadatas=[{
        "user_id": chunk.capture.user_id,         # tenant isolation, REQUIRED filter
        "capture_id": chunk.capture_id,           # join back to Postgres
        "source_kind": chunk.source_kind,         # filter by article / transcript / etc.
        "captured_at": chunk.capture.captured_at, # ISO 8601, time-range filtering
    }],
)
```

Rule for what goes in metadata: only fields we'll actually filter or sort on at query time. Don't dump the whole row.

**Similarity metric: cosine, throughout.** Set on the Chroma collection at creation (`metadata={"hnsw:space": "cosine"}`) and on the eventual pgvector index (`USING hnsw (embedding vector_cosine_ops)`). Reason: `sentence-transformers/all-MiniLM-L6-v2` is trained with cosine objective, cosine is magnitude-invariant (good for chunks of varying length), and it's the standard for RAG retrieval.

### B.3 — ChromaDB collection structure ✅ Locked (2026-05-05)

**Decision: three ChromaDB collections — `chunks`, `topics`, `entities`. Each is a single shared collection (not split per user or per source kind).**

| Collection | Stores | Filtered by | Cardinality (1y horizon at 100 students) |
|---|---|---|---|
| `chunks` | One vector per chunk in the SQL `chunks` table | `user_id` metadata at query time | ~18 M vectors |
| `topics` | One vector per row in the SQL `topics` table | None (shared global vocabulary) | ~10 K vectors |
| `entities` | One vector per row in the SQL `entities` table | None (shared global vocabulary) | ~50 K vectors |

**Why one shared `chunks` collection over per-user or per-source-kind:**

- ChromaDB scales fine to multi-million-vector collections. We're nowhere near a ceiling.
- Per-user collections multiply HNSW index memory cost without benefit at our scale.
- Per-source-kind splits hurt retrieval — synthesis queries (use case A) want results across kinds (an article paragraph + a transcript segment + a caption may all be relevant).
- Metadata-filter on `user_id` is fast at our scale (chunks already carry the metadata per B.2).
- Maps cleanly to the eventual pgvector migration (A.1 / A.7 trigger): "one collection with `user_id` filter" → "one `chunks` table with `WHERE user_id = ?`."

**Why `topics` and `entities` get their own collections:**

- B.7 requires fast "find top-K topics semantically similar to this capture" lookup during enrichment. That's a vector query, exactly Chroma's job.
- Keeping topic/entity vectors out of the `chunks` collection means we can query "which topics is this about?" without polluting chunk-search results with topic-row matches.
- They're shared globally (no `user_id` filter) — vocabulary is shared across students.

**Index settings for all three collections:**

```python
client.create_collection(
    name="chunks",  # or "topics" / "entities"
    metadata={"hnsw:space": "cosine"},
)
```

Cosine similarity throughout — sentence-transformers all-MiniLM-L6-v2 is trained with that objective (see B.2).

### B.4 — pgvector index type when we migrate

- **HNSW** (Hierarchical Navigable Small World) — best recall, slower to build, more memory.
- **IVFFlat** — faster to build, less memory, slightly lower recall.

Defer until we actually migrate. Choice depends on chunk count at the time.

### B.5 — Backfill + cutover script ✅ Locked (2026-05-05)

**Decision: `scripts/migrate_jsonl_to_sql.py` — idempotent, streaming reads with batched commits, single-user mapping for historical data.**

| Sub | Decision | Rationale |
|---|---|---|
| **B.5.1 — Idempotent** | Yes. `INSERT OR IGNORE` (SQLite) / `ON CONFLICT DO NOTHING` (Postgres) on primary keys. ChromaDB uses `collection.upsert()` instead of `add()`. | Re-running after a partial failure must be safe. SQLAlchemy Core handles both dialects uniformly. |
| **B.5.2 — Reads + commits** | Streaming reads (line-by-line). Commits every 100 rows. | OOM-safe on large JSONLs. Crash leaves at most 99 uncommitted rows; B.5.1 makes the resume safe. |
| **B.5.3 — Validation failures** | Log to `data/migration_failures.jsonl` with `{source_file, line_number, raw_row, error_reason}`. Continue processing. Print summary at end. | Don't fail the whole migration on one bad row. Operator reviews failures, fixes if possible, re-runs (idempotent). |
| **B.5.4 — User mapping** | Create `users` row with `id=1, email="sabya.bisoyi@gmail.com"`. All historical captures get `user_id=1`. Future students get `id=2, 3, ...`. | Phase 1 was single-user; all historical data is Sabya's. Trivial mapping, future-compatible. |
| **B.5.5 — Order of operations** | `users` → `captures` → `hydrations` → `enrichments` → `chunks` → `embeddings` → `topics` + `entities` → `chunk_topics` + `chunk_entities`. | Foreign key constraints force this order. Steps 5-8 are NEW work (chunks don't exist in JSONL form; must be generated by applying A.5 chunking rules). |
| **B.5.6 — Captures awaiting enrichment** | Migrate the capture row, skip the missing enrichment row, let `enrichment_worker` pick it up post-migration. | Phase 2.5 Fix 1 already made `find_unenriched_capture_ids` idempotent and skip-aware. Natural recovery path. |
| **B.5.7 — Dry-run** | `--dry-run` flag. Reads + validates everything, prints expected counts and time estimates, writes nothing. | Same pattern as Phase 2's `scripts/backfill_enrichment.py --dry-run`. |
| **B.5.8 — Verification** | `--verify` subcommand: row-count comparison (JSONL line count vs SQL row count) + spot-check 5 random capture_ids' full content. Fails loudly on any mismatch. | Safety net before committing to the dual-write window. |

**Speed estimate:** ~5K captures × ~10 chunks each = 50K chunks to embed at 15ms/chunk on M-series CPU = ~12 minutes for a full one-shot migration. Acceptable.

### B.6 — Embedding regeneration policy

When the embedding model changes (today MiniLM → tomorrow BGE-M3), do we:
- Re-embed everything in a one-shot script?
- Lazily re-embed on first query for each chunk?
- Keep both embedding columns in parallel during transition?

### B.7 — Topic / entity ingestion ✅ Locked (2026-05-05)

**Decision: controlled vocabulary that grows organically. The LLM is constrained to prefer reusing existing topics/entities; only allowed to coin new ones when no existing match scores above a similarity threshold.**

This is more sophisticated than free coinage but produces a much cleaner topic graph — dedup happens at ingestion, not after the fact. Required for synthesis quizzes (use case A) to work properly: filtering by `topic_id` only returns coherent results when topic names are stable across captures.

**Per-enrichment flow:**

1. Compute embedding of the new capture's summary (we already have this from sentence-transformers).
2. Query the `topics` ChromaDB collection for the top-K (start with K=30) most semantically similar existing topics. Same for `entities`.
3. Pass that shortlist to the enrichment LLM as part of the prompt:
   > *"These are existing topics relevant to this content. For each topic this capture covers, return its slug if it appears in the list, OR coin a new slug only if no existing topic fits. Explain new coinages briefly."*
4. LLM returns a list of `{kind: "existing"|"new", slug, label, confidence}`.
5. `existing` ones link directly via FK on the `chunk_topics` junction.
6. `new` ones go through normalization (B.7.1), get inserted into `topics`, get embedded, get added to the `topics` ChromaDB collection — they become candidates for future captures.

Same pattern for entities, with `entity_type` extracted alongside.

**Sub-decisions:**

| Sub | Decision | Notes |
|---|---|---|
| **B.7.0 — Vocabulary management** | Controlled, growing organically via the flow above | The bootstrap problem (empty `topics` table at first) self-resolves: the first ~100 captures will coin many; later captures increasingly reuse |
| **B.7.1 — Slug normalization** | Yes — lowercase, strip whitespace, replace whitespace runs with `-`, strip non-alphanumeric (except `-`), cap at 64 chars | Defensive layer alongside embedding-similarity matching. `"Kanban Method"` → `"kanban-method"`. `label` field keeps the human-readable form |
| **B.7.2 — Coinage policy** | Constrained — LLM may only coin a new slug when no existing match scores above 0.75 cosine similarity | Threshold tunable. Higher = more aggressive reuse (more dedup, possibly some false merges). Lower = more permissive (more topics, less dedup) |
| **B.7.3 — Wikidata linking** | Deferred to Phase 5+ enhancement | Add nullable `wikidata_id` column to `entities` then if useful for use case C. Not part of Phase 3 |

**Implementation implications:**

- Schema: `topics` and `entities` tables each get `embedding BLOB` and `coined_at TEXT` columns (already added in A.4).
- Two new ChromaDB collections (`topics`, `entities`) per B.3.
- The enrichment prompt (`backend/knowledge/prompts.py`) restructures from "extract topics" to "given this capture and these candidate existing topics, identify which apply or coin new ones."
- Each enrichment call adds two extra Chroma queries (top-K topics + top-K entities). Sub-millisecond, negligible cost.
- Enrichment prompt grows by ~200-500 input tokens (the candidate shortlists). At Haiku pricing that's roughly $0.0002 extra per capture — real but tiny.
- The migration script (B.5) embeds every existing topic/entity from `enrichments.jsonl` and adds them to the new ChromaDB collections during step 7 of the order in B.5.5.

### B.8 — Hybrid retrieval (BM25 + vector) — Phase 4 enhancement

Pure semantic (vector-only) retrieval can miss queries with rare terms ("Andon cord," "RIPEMD-160," specific person names) where keyword matching would be more decisive. Hybrid retrieval — combining BM25 keyword scoring with vector similarity, then re-ranking — typically beats either alone in published benchmarks.

Postgres supports this in one engine via `tsvector` columns alongside the `pgvector` embedding column. We can add it without touching the chunks-as-retrieval-unit design.

Open questions for Phase 4:
- Score-combination function (Reciprocal Rank Fusion vs weighted sum vs learned re-ranking)?
- Should we expose the BM25 score / vector score to the agent so it can pick a retrieval strategy per query?
- Do we add a `tsvector` column to `chunks` from day one (cheap insurance) or wait until Phase 4?

Likely answer for the last sub-question: add the column now, populate it on chunk insert, defer the actual hybrid-retrieval query logic to Phase 4. The column itself is a few KB per chunk — negligible.

### B.9 — Graph storage / GraphRAG — Phase 5+ open question

The C use case (Deepika clue inference) is the kind of multi-hop reasoning where graph databases (Neo4j, ArangoDB) and the GraphRAG pattern (Microsoft, 2024) sometimes help. We deliberately did **not** add curated edge tables in v1 (see A.3) because most multi-hop reasoning can be done by Claude over retrieved chunks if those chunks are well-tagged.

This becomes worth revisiting if:
- Phase 4 agent quality measurably suffers because the agent can't connect chunks that obviously SHOULD be linked.
- We can demonstrate the failure mode is graph-shaped (e.g., the agent retrieves the right chunks individually but misses the relationship between them).

If we ever do add it, the lightweight path is: add `entity_relations` and `topic_relations` tables to Postgres, populated by an LLM-based extractor. Postgres recursive CTEs handle small-graph traversal fine at our scale; we don't need Neo4j unless the relation count gets very large (which is a long way off).

Reading reference if you want to explore the topic: *From Local to Global: A Graph RAG Approach to Query-Focused Summarization* (Edge et al., Microsoft Research, 2024), https://arxiv.org/abs/2404.16130.

---

## Learning resources for Sabya — chunking + vector DBs

Read the underlined items first; they're the highest-signal-per-minute. Everything below is a real, citeable resource — not LLM-generated content.

### Chunking — start here

- **Greg Kamradt — *5 Levels of Text Splitting for Retrieval*** (YouTube, ~70 min). The single best practical introduction. Walks from naive character-splitting up through semantic chunking with concrete examples. https://www.youtube.com/watch?v=8OJC21T2SL4 (also a notebook on his GitHub).
- **LangChain docs — *Text Splitters***. Covers every off-the-shelf splitter (recursive character, markdown, code, semantic). https://python.langchain.com/docs/concepts/text_splitters/
- **Pinecone Learning Center — *Chunking Strategies***. Practical guide to fixed-size, recursive, semantic, and document-specific chunking with retrieval-quality tradeoffs. https://www.pinecone.io/learn/chunking-strategies/

### Chunking — go deeper

- **LlamaIndex — *Optimizing for Production: Chunk Sizes***. Empirical comparison of chunk sizes on retrieval accuracy. https://docs.llamaindex.ai/en/stable/optimizing/production_rag/
- Anthropic's *Contextual Retrieval* post (2024). How to improve chunk-level retrieval by prepending document-wide context to each chunk before embedding. Directly relevant to our β-with-γ-flavor design. https://www.anthropic.com/news/contextual-retrieval

### Vector databases — start here

- **Pinecone — *Vector Database 101***. Conceptual primer: what an embedding is, what nearest-neighbor search means, why approximate (ANN) is good enough. https://www.pinecone.io/learn/vector-database/
- **ChromaDB official docs** — what we're using locally. https://docs.trychroma.com/
- **pgvector official README** — what we'll use in cloud. https://github.com/pgvector/pgvector

### Vector databases — comparison + benchmarks

- **ANN Benchmarks** — open-source, regularly updated comparison of nearest-neighbor algorithms (HNSW vs IVF vs others). Useful for understanding the speed-vs-recall tradeoff in B.4. https://ann-benchmarks.com/
- **Vector DB comparison spreadsheet** maintained by Vector DB Comparison community. Compares Pinecone, Weaviate, Qdrant, Chroma, pgvector, Milvus on features, pricing, deployment. https://vdbs.superlinked.com/

### Foundational papers (skip if short on time)

- **Karpukhin et al. (2020).** *Dense Passage Retrieval for Open-Domain Question Answering.* EMNLP. https://arxiv.org/abs/2004.04906 — the foundational DPR paper that made vector retrieval mainstream.
- **Reimers & Gurevych (2019).** *Sentence-BERT: Sentence embeddings using Siamese BERT-networks.* EMNLP. https://arxiv.org/abs/1908.10084 — why all-MiniLM-L6-v2 (which we're using) works.
- **Johnson, Douze, Jégou (2017).** *Billion-scale similarity search with GPUs.* https://arxiv.org/abs/1702.08734 — the FAISS paper, foundational for understanding ANN at scale.

### RAG (where this all goes in Phase 4)

- **Anthropic — *Retrieval-Augmented Generation* docs.** https://docs.claude.com/en/docs/build-with-claude/contextual-retrieval
- **LangChain — *RAG tutorial***. https://python.langchain.com/docs/tutorials/rag/
- **Lewis et al. (2020).** *Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks.* NeurIPS. https://arxiv.org/abs/2005.11401 — the original RAG paper.

### Reading order suggestion (~6-8 hours total)

1. Pinecone Vector Database 101 (~30 min)
2. Greg Kamradt's 5 Levels video (~70 min)
3. ChromaDB getting-started (~30 min)
4. pgvector README (~30 min)
5. Pinecone Chunking Strategies (~45 min)
6. Anthropic Contextual Retrieval post (~30 min)
7. ANN Benchmarks site, browse for 30 min to develop intuition

After that you'll have strong opinions on the open Layer B questions, and we can sign off and start building.

---

## Why a Phase 3 design doc and not just code

Same reasoning as Phase 1, Phase 2, and Phase 2.5: design-then-code is faster than refactor-after-mistake. The schema is the most expensive thing in the whole project to change once data exists — every change requires a migration script, every migration script can lose data, every shipping decision after that is constrained by the choices we make here. A few hours of design now saves weeks of "we need to change the schema" later.

---

## Next: re-open Layer B + start building

After Sabya's reading:
1. Walk through Layer B questions B.1 through B.7. Lock answers.
2. Write `docs/phase3-smoke-test.md` with the verification passes (similar to phase2-smoke-test.md).
3. Build, smoke-test, ship.
4. Flip this doc's status to "PHASE 3 LIVE" when end-to-end works.

After that, Phase 4 (the agent + Q&A interface) can begin in earnest — that's where the synthesis quizzes and the Deepika clue game actually become demonstrable.
