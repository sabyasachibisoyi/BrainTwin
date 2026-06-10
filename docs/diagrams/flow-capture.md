# Flow — Capture path

How a page read in Chrome becomes a row in SQLite + a vector in Chroma.

Triggered when the user dwells ≥30s on a page; the content script posts
to `/capture`, the backend stores text + metadata, and an enrichment
worker chunks + embeds asynchronously.

```mermaid
sequenceDiagram
    autonumber
    participant U as User (Chrome)
    participant CS as Content script
    participant SW as Service worker<br/>(background.js)
    participant CF as Cloudflare<br/>(digitaltwin.app)
    participant Cad as Caddy<br/>(EC2 :443)
    participant API as FastAPI app<br/>(:8000)
    participant SQL as SQLite<br/>(EBS)
    participant Chr as Chroma<br/>(EBS, local)
    participant S3 as S3<br/>(images)
    participant LS as Litestream

    U->>CS: scrolls + dwells ≥30s
    CS->>SW: postMessage({url, title, text, dwell_seconds})
    SW->>CF: POST /capture<br/>Authorization: Bearer <token>
    Note over CF: TLS termination,<br/>DDoS, WAF
    CF->>Cad: HTTPS :443
    Cad->>API: HTTP :8000 (loopback)

    rect rgb(245, 245, 255)
        Note over API: Sync path — must finish<br/>before HTTP 200
        API->>API: validate bearer token
        API->>SQL: INSERT captures(url, text, ts, …)
        SQL-->>LS: WAL frame appended
        API-->>SW: 200 {status, capture_id, persisted}
    end

    par async enrichment worker
        API->>API: chunker.split(text)
        API->>API: embedder.encode(chunks)<br/>(local model on EBS)
        API->>Chr: upsert(chunk_ids, vectors,<br/>metadata)
        API->>SQL: INSERT chunks(capture_id, text, …)
        SQL-->>LS: WAL frame appended
    and async image upload (if page had screenshot)
        API->>S3: PUT images/{capture_id}.png
    end

    Note over LS,S3: Litestream streams WAL<br/>frames to S3 within seconds
    LS->>S3: PUT litestream/wal/segment.bin
```

## Failure-mode notes

- **Token rejected** — API returns 401 immediately; the content script
  drops the capture and surfaces a one-time toast. See
  `flow-failure-modes.md`.
- **Embedder OOM on large pages** — enrichment retries with a smaller
  chunk size; the SQL row is already persisted so retrieval still finds
  it by FTS5 even before vectors land.
- **S3 PUT fails** — image upload is best-effort; capture is not rolled
  back, image just isn't recoverable on later restore.
- **Litestream lag** — captures land in SQLite first, replication is
  async. RPO is "seconds, not zero". Acceptable for v1 single-user.
