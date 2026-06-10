# Flow — Recall path (happy path, first turn)

How a vague query ("that article about Llama 4 quirks") becomes a
ranked list of capture cards.

This is the path implemented in Phase 4 M.2–M.4. Two retrieval branches
run in parallel, results are fused via RRF, then a Sonnet pass reranks
the top-K with reasoning, and the response carries an answer + cards
back to the popup.

```mermaid
sequenceDiagram
    autonumber
    participant U as User<br/>(Remember tab)
    participant Ext as recall.js<br/>(Chrome popup)
    participant CF as Cloudflare
    participant API as FastAPI<br/>POST /recall
    participant Rec as Recaller
    participant Ret as RetrievalService
    participant Chr as Chroma<br/>(vector)
    participant FTS as SQLite FTS5<br/>(BM25)
    participant Son as Sonnet 4.5<br/>(Anthropic API)
    participant Conv as ConversationStore<br/>(in-process TTL)

    U->>Ext: types "that article about Llama 4 quirks"<br/>+ submit
    Ext->>CF: POST /recall<br/>Bearer + {query}
    CF->>API: HTTPS
    API->>Rec: recall(query=…, conv_id=None)

    Rec->>Ret: retrieve(query, k=20)
    par parallel branches
        Ret->>Chr: query(vector, k=20)
        Chr-->>Ret: 20 vector hits
    and
        Ret->>FTS: MATCH(query) ORDER BY rank
        FTS-->>Ret: 20 BM25 hits
    end
    Ret->>Ret: RRF fusion (k=60)<br/>→ top-K candidates
    Ret-->>Rec: 10 fused candidates

    Rec->>Son: rerank prompt<br/>(candidates + query)
    Note over Son: System prompt:<br/>"You are a memory recall assistant"<br/>+ JSON schema constraint
    Son-->>Rec: {answer, results[].score,<br/>results[].why_this_matches,<br/>no_match: false}

    Rec->>Conv: create conversation_id<br/>store {query, candidates,<br/>chosen_ids}
    Conv-->>Rec: conversation_id

    Rec-->>API: RecallResponse
    API-->>Ext: 200 JSON
    Ext->>U: renders answer<br/>+ result cards<br/>+ "Refining…" badge
```

## Fork: no-match path

If Sonnet returns `no_match=true`, the closest-miss prompt is invoked
(separate Sonnet call) to surface ONE courtesy card with low confidence.
The popup shows the "Not confident this is it…" banner.

## Latency budget

| Step | Target | Hard cap |
|------|--------|----------|
| RetrievalService (parallel + fuse) | <200ms | 500ms |
| Sonnet rerank | <3s | 10s |
| Total /recall p95 | <3.5s | 12s (popup timeout: 30s) |
