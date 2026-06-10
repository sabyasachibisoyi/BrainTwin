# Flow — Failure modes (fault-isolated degradation)

Where each component can fail and how the system degrades. The design
goal is **never a hard outage on /recall** — a working component should
keep working even when its sibling fails.

```mermaid
flowchart LR
    Q([User query]) --> API[/POST /recall/]

    API --> RetSvc{RetrievalService}

    RetSvc -- "vector branch" --> Chr[(Chroma)]
    RetSvc -- "BM25 branch" --> FTS[(SQLite FTS5)]

    Chr -- ok --> Fuse[RRF fusion]
    FTS -- ok --> Fuse
    Chr -- "fail / OOM" --> BM25Only["BM25-only path<br/>(degraded confidence)"]
    FTS -- "fail / locked" --> VecOnly["Vector-only path<br/>(degraded confidence)"]
    BM25Only --> Fuse
    VecOnly --> Fuse

    Fuse --> Rerank{Sonnet rerank}
    Rerank -- "200 OK" --> Resp[Card response<br/>+ answer]
    Rerank -- "503 / 429 / timeout" --> Rule["Rule-based rerank<br/>(no LLM answer field)"]
    Rule --> RespNoAnswer[Card response<br/>answer = null<br/>warning banner]

    Resp --> U([User sees results])
    RespNoAnswer --> U

    API -. "bearer missing/<br/>wrong" .-> Reject([401 Unauthorized])

    subgraph Health["External monitors"]
        UR[UptimeRobot] -->|GET /health 60s| HealthEP[/health]
        Budget[AWS Budgets] -->|threshold| Email[email alert]
    end
```

## Component failures and what happens

| Component down | Behavior | Recovery |
|----------------|----------|----------|
| Chroma | BM25-only rerank; `confidence` capped at 70%; banner "vector search degraded" | Restart container; Chroma rebuilds from EBS data dir |
| SQLite | API returns 503 — recall can't work without text. Captures via Telegram bot retried with backoff | Litestream restore (see `flow-backup-restore.md`) |
| Sonnet 4.5 | Rule-based rerank: RRF top-5 surfaced as-is, `answer` is null, popup shows "ranker degraded" banner | Sonnet recovery is upstream; retry on next query |
| Anthropic API down entirely | Same as Sonnet failure path | Wait it out / try again |
| EC2 instance | `/health` flaps, UptimeRobot pages, Caddy returns 502 via Cloudflare | Restart instance via SSM; data persists on EBS |
| EBS volume | Hard outage — captures and chunks unavailable | Restore from latest DLM snapshot + Litestream WAL replay |
| Litestream | Captures still land in local SQLite; WAL backup is degraded but live serving unaffected. **Restic-style warning to operator** | Restart container; bootstrap from S3 |
| Caddy | TLS termination dies; Cloudflare shows 521 to clients | SSM in, `docker compose restart caddy`; Let's Encrypt cert in cached volume |
| Cloudflare | DNS still resolves; direct EC2 IP works but no TLS termination | Wait out CF incident; do NOT bypass to direct origin (it would expose origin IP) |
| ECR | Existing image still running. New deploys block until ECR is back | Wait; no live serving impact |
| SSM Parameter Store | App can't fetch new secrets at boot. Already-running containers unaffected | Cached env vars persist for the running process; recover on next deploy |

## Bearer token failure modes

| Cause | Status | Client behavior |
|-------|--------|-----------------|
| Missing `Authorization` header | 401 | Extension toast: "Auth not configured. Re-paste your token." |
| Token doesn't match SSM-stored value | 401 | Same toast |
| Token matches but app paused | 503 | Pause is local-only; if cloud is up, this shouldn't happen |

## Where the alarms go

- **/health failing** → UptimeRobot email + push notification to phone
- **AWS Budgets > threshold** → email to operator
- **CloudWatch Logs error rate spike** → (Phase 4.0.6.1) Cloudwatch alarm
- **Litestream WAL lag > 60s** → (deferred to Phase 4.0.7) Cloudwatch
  alarm; for now, manual check during smoke test
