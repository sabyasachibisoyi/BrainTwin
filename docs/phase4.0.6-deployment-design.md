# Phase 4.0.6 — Cloud Deployment (AWS)

> **Status as of 2026-06-10 — DESIGN UPDATED (region + diagrams + branding).**
>
> Update summary (2026-06-10):
>   1. **Region:** Primary region is now **`us-west-2` (Oregon/PDX)** for
>      Seattle-based development latency. `ap-south-1` (Mumbai) is
>      **NOT** deployed on day one — the CDK stack is region-parameterized
>      so a second region is `cdk deploy --context region=...`, but
>      active-active is a Phase 5+ decision (§3.0).
>   2. **HLD diagrams:** New §6 — Python `diagrams` library for the
>      topology, Mermaid for flows, `cdk-dia` later for auto-verification.
>      All version-controlled in `docs/diagrams/`. Miro rejected for
>      canonical diagrams.
>   3. **Product naming:** New §11 — customer-facing brand is
>      **DigitalTwin**; codebase / internal stays **BrainTwin**.
>      Domain: `digitaltwin.app` / `.me` / `.io`. No code rename;
>      the swap is at presentation surfaces only.
>
> Previous revision (2026-06-09 senior-eng review): budget reworked
> around the real post-July-2025 AWS credit rules (Paid Plan at
> signup, ~12-month runway, NOT 23); server-side auth pulled forward
> into M.1 so the API is never publicly exposed unauthenticated;
> Telegram bot added as an explicit deployed service (it was the #1
> pain point but had no deploy milestone); instance bumped to
> t4g.small (1 GiB was the riskiest call in v1 of this doc);
> docker-compose locked as the deployment unit; litestream/S3
> lifecycle conflict fixed; Cloudflare TLS mode pinned; SSH replaced
> with SSM Session Manager; CDK locked as the IaC choice.
>
> Phase 4.0 (Vague Recall) shipped on the laptop. Before going to
> Phase 4.0.5 (Eval) or Phase 4.1 (Synthesis Quizzes), this slice
> takes BrainTwin off the laptop and onto a cloud-hosted backend so:
>
> - Captures work 24/7 regardless of laptop state
> - The Chrome extension and Telegram bot reach the same backend
>   from any machine
> - The eval harness in 4.0.5 can run against a stable production
>   target, not a flaky local one
> - There is a real portfolio bullet: "Deployed BrainTwin on AWS as
>   infrastructure-as-code"
>
> Scope: this doc covers WHY (the cloud choice), WHAT (the services
> selected and rejected), and HOW (the deployment steps). It does NOT
> cover the multi-user auth story — single-user remains the v1
> contract per Phase 3 decision A.2 / B.5.4. Multi-user lands with
> Phase 4.1's use-case-A work.

---

## 1. Why cloud at all (and why now)

The Phase 4.0 ship validated that the agent is usable when it's up.
What it didn't change: the laptop is the single point of failure.
Three concrete pains drove this phase:

1. **Telegram forwards get lost when the laptop sleeps.** The bot
   polls Telegram from the laptop, so a closed lid = a silent
   capture outage.
2. **The backend is only up when the laptop is.** Honest framing:
   the Chrome extension lives on the laptop anyway, so cloud hosting
   alone doesn't make *recall* laptop-independent — that lands with
   M.6 (Telegram `/recall`). The headline win today is capture
   continuity; a stable backend is the prerequisite for phone-side
   recall later.
3. **Phase 4.0.5 (eval) is impossible to build well against a
   laptop-hosted backend.** The eval discipline needs a stable
   target so test runs are comparable; laptop sleeps / restarts
   make every run a different beast.

Cloud hosting solves all three. The remaining question is which
cloud and how.

---

## 2. Why AWS over alternatives

Four options were considered seriously. AWS wins on portfolio value;
the others lose for specific reasons documented here so this
decision isn't re-litigated.

### 2.1 The chosen path: AWS

Locked because:

- **Universal portfolio signal.** "Deployed on AWS" reads cleanly
  on a resume to every hiring manager at a product company —
  AWS is the default cloud at startups, hyperscalers, and most
  global tech employers.
- **Real credits, with real fine print.** The post-July-2025
  restructure gives new accounts **$100 at signup + up to $100 more
  for completing onboarding activities**. Critical rule verified
  2026-06-09: on the **Free Plan**, the account is **auto-closed
  after 6 months or when credits run out — whichever comes first**
  (90-day grace, then resources deleted, unused credits lost).
  That's unacceptable for a system that must stay up. **Decision:
  sign up on the Paid Plan** — the same $200 in credits apply,
  the account persists, and credits expire ~12 months after
  issuance. Realistic free runway: **~12 months**, not 23.
  The GitHub Student Pack's AWS benefit historically routed through
  AWS Educate, which stopped general credit grants in 2023 —
  **do not count that money until it's visible in the account.**
- **Always-free services on top.** Lambda 1M req/month, DynamoDB
  25GB, CloudWatch metrics — perpetual, no expiry, on top of the
  $200.
- **Most mature deployment ecosystem.** Every IaC tool (Terraform,
  CDK, Pulumi) has first-class AWS support. Every CI/CD platform
  (GitHub Actions, CircleCI) has AWS deployment recipes.
- **Region presence near operator.** `us-west-2` (Oregon / PDX) for
  Seattle-based development gives sub-50ms latency to the laptop and
  Chrome extension. `ap-south-1` is a one-line CDK switch when needed.
  See §3.0 for the multi-region story.

### 2.2 Considered and rejected

**Azure** — Strong for BFSI / Indian IT services target audience and
the always-free `Azure for Students` ($100/year, no card) is
arguably the simplest student deal in the market. But our target is
**product companies**, where AWS is more universal. We will still
apply for Azure for Students as a backup credit source.

**GCP** — $300 trial credit is decent but the free tier afterwards
is thinner than AWS's always-free list. Smaller portfolio signal
unless targeting ML / data-engineering roles specifically.

**Fly.io** — Best ergonomics; would have been the answer if this
were purely a personal project. Lost on portfolio: most hiring
managers don't recognize the name, so the resume bullet doesn't land.

**Self-hosted home server + Cloudflare Tunnel** — Free monthly cost,
but uptime depends on home internet and electricity. Ops burden
(security patches, hardware) isn't useful portfolio learning. Hard
no for a project that needs to be reachable from anywhere.

**Oracle Cloud Always Free** — Generous compute (2 AMD VMs + 4 ARM
cores) at literally $0 forever. Tempting purely on cost. Rejected
because nobody hires for Oracle Cloud skills.

---

## 3. AWS services selection

The locked architecture is **single-VM, everything-in-one,
SQLite-on-EBS**. The Phase 3 design already keeps the schema
Postgres-ready, so the SQLite-vs-RDS choice can flip later without
schema changes.

### 3.0 Regions: us-west-2 primary, multi-region CDK from day one

The previous revision assumed Mumbai (`ap-south-1`) for Indian-context
latency. The user is now developing from Seattle, so:

| Aspect | Decision |
|--------|----------|
| **Primary region** | **`us-west-2` (Oregon / PDX)** — closest to Seattle, sub-50ms latency from the operator's laptop |
| **Single AZ within primary** | `us-west-2a` for v1 (cheapest, simplest) |
| **Secondary region** | `ap-south-1` (Mumbai) — **NOT deployed on day one**. Configured as a CDK target but not provisioned. |
| **CDK shape** | **Region-parameterized stack.** `bin/braintwin.ts` reads region from CDK context (or environment) so `cdk deploy` defaults to `us-west-2`, and `cdk deploy --context region=ap-south-1` would deploy a separate parallel stack to Mumbai. No code change needed to add a region. |
| **When to actually deploy a second region** | When use case A (multi-user quizzes) goes live AND user concentration in another geography justifies the ~2x cost. Until then, a warm Mumbai stack is a $15/month tax with no return. Documented as Phase 5+. |
| **Why not active-active now** | Would roughly double the bill (extra EC2, EBS, cross-region S3 replication, Route 53 latency routing). For a single user, the win is zero. The CDK parameterization gives us the *option* without the bill. |
| **Data residency** | Single-region for now means user data lives in `us-west-2` only. Acceptable per Phase 3 design A.2 (multi-tenant from day one, but data-residency boundaries are a use-case-A concern). |

**Cost delta `us-west-2` vs `ap-south-1`**: roughly equivalent for the
services we use (within ~5%). No material difference; the choice is
on latency.

### 3.1 Compute: EC2 `t4g.small` (single instance, all-in-one)

| Aspect | Decision |
|--------|----------|
| Instance type | `t4g.small` (2 vCPU Graviton/ARM, 2 GiB RAM) |
| Region | **`us-west-2` (Oregon / PDX)** primary — see §3.0 for the region story and CDK multi-region shape |
| AZ | `us-west-2a` — single AZ for v1; no multi-AZ until use case A goes multi-user |
| OS | Amazon Linux 2023 (arm64) |
| Why not ECS Fargate | Fargate is NOT in the free tier — costs ~$9/month minimum. A single EC2 box is cheaper AND the "I ran a containerized FastAPI app on EC2 + Docker" bullet is still solid resume material. |
| Why not t3.micro (1 GiB) | This was v1 of this doc and it was the riskiest call in it. sentence-transformers pulls in PyTorch (~0.5–1 GB RSS with the model resident), Chroma keeps its HNSW index in RAM, and whisper.cpp spikes during transcription. On 1 GiB that means living in swap on EBS and an eventual OOM-kill of uvicorn mid-transcription. Swap is a strategy for occasional spikes, not for a resident model. Graviton pricing makes t4g.small land within ~$1–2/month of t3.micro in `us-west-2` — double the RAM for roughly the same money. |
| ARM consequence | Docker image built for `linux/arm64` via `docker buildx`. Everything in the stack (torch CPU wheels, sentence-transformers, whisper.cpp, ffmpeg) has aarch64 support. Cross-building from the laptop is itself a nice portfolio detail. |
| CPU credit mode | `standard`, NOT `unlimited` — whisper runs will burn burst credits, and unlimited mode silently bills overages. Better to throttle than to surprise-bill. |
| RAM strategy | 2 GiB physical + 2 GiB swap file on EBS as headroom, not as the plan. |
| App runtime constraint | **uvicorn runs with exactly 1 worker.** `ConversationStore` is in-process memory — at `--workers 2` multi-turn refinement breaks intermittently (the refinement turn lands on the other worker). Single-user load doesn't need more; this constraint is load-bearing and documented here so nobody "tunes" it away. |

### 3.2 Database: SQLite on EBS (not RDS — for now)

| Aspect | Decision |
|--------|----------|
| v1 storage | SQLite file on the EC2's persistent EBS volume |
| Storage layout | `data/braintwin.db` + `data/chroma/` + `data/images/` + whisper model — same paths as local |
| Backups | Continuous WAL replication to S3 via **litestream** (§3.5) |
| Why not RDS Postgres now | Free RDS db.t3.micro is in Legacy Free Tier (pre-July 2025) — new accounts don't get the 12-month free. ~$13/month adds 30% to monthly burn and the user data fits in SQLite for the foreseeable future. |
| Migration path | Phase 3 A.7 already made schema Postgres-compatible. When RDS is right, it's a connection-string change in `database_url`. Documented as Phase 4.0.7 (Postgres migration). |

### 3.3 Object storage: S3

| Aspect | Decision |
|--------|----------|
| Bucket name | `braintwin-prod-{account-id}` (or single-user equivalent) |
| Versioning | Enabled — protects against accidental delete |
| Lifecycle | **Scoped by prefix.** Glacier-after-30-days / delete-after-180 applies ONLY to the nightly tarball prefixes (`chroma/`, `images/`). The `litestream/` prefix is **excluded** — litestream needs its generations instantly readable or `litestream restore` breaks (or pays Glacier retrieval); litestream's own retention settings manage that prefix. |
| What goes here | Litestream's SQLite WAL stream (own prefix, own retention), image uploads, the Chroma index nightly snapshot |
| Why not EFS | EFS is overkill for single-instance access. S3 is cheaper for backup-shaped workloads. |

### 3.3.1 Container registry: ECR (private)

| Aspect | Decision |
|--------|----------|
| Registry | One private ECR repo, `braintwin` |
| Image size | Expect 2–4 GB with torch + ffmpeg + whisper.cpp. Use the **CPU-only torch wheel** (`--index-url https://download.pytorch.org/whl/cpu`) to roughly halve it. |
| Cost | $0.10/GB-month storage — pennies, but it's in the budget table now (it was missing in v1 of this doc). |
| Hygiene | Lifecycle policy: keep last 5 images, expire untagged. `docker system prune` on the host periodically — image layers accumulate on the 30 GiB EBS volume. |

### 3.4 Networking & HTTPS

| Aspect | Decision |
|--------|----------|
| Subnet | Single public subnet — NO private subnet, NO NAT Gateway. NAT Gateway is $32/month and would dominate the bill. |
| Shell access | **SSM Session Manager — NO port 22, no SSH keypair at all.** The instance role already exists for Parameter Store; Session Manager rides the same agent. Zero open SSH ports is a stronger security posture (and resume bullet) than "SSH from operator IP". |
| Security group | Inbound: 80 + 443 only. **Until M.7 cutover: restricted to the operator's IP.** After M.7: restricted to **Cloudflare's published IP ranges** (so nobody bypasses Cloudflare and hits the origin directly). Outbound: all. |
| Public IP | **Elastic IP** attached to the instance (free as long as it's attached; $4/month if unattached and idle). |
| HTTPS terminator | **Caddy** on the box. Reverse-proxies to the FastAPI container. |
| TLS mode (pinned) | Cloudflare **proxied (orange-cloud)** + SSL mode **Full (strict)**. Because the origin sits behind the proxy, Caddy uses the **DNS-01 challenge via a Cloudflare API token** (scoped to this zone) instead of HTTP-01. This combination is what actually delivers the DDoS-protection claim — DNS-only (grey-cloud) would not. |
| Why not ALB | ALB is $16/month minimum. For a single backend on a single box, Caddy + Let's Encrypt is functionally equivalent and free. |
| DNS | **Cloudflare** free tier. Domain via Namecheap (~$10/year, free via Student Pack). |
| Domain | `braintwin.app` or `braintwin.me` — TBD when Student Pack lands. |

### 3.5 Backups: litestream → S3

[litestream](https://litestream.io) is a small Go binary that
streams SQLite's WAL to S3 in near-real-time. It's the de facto
standard for serious SQLite-in-production deployments.

| Aspect | Decision |
|--------|----------|
| What's backed up | `data/braintwin.db` (SQLite). Chroma index is a separate concern (§3.5.1). |
| Replication latency | ~1 second (continuous WAL streaming) |
| Restore time | A few seconds for our DB size; minutes when it grows |
| Schedule | Continuous, plus nightly snapshot for point-in-time recovery convenience |

**3.5.1 Chroma backup.** Chroma's persistent store is a directory
of files. Not as elegant as SQLite for incremental backup. Plan: a
nightly cron job tar.gz's `data/chroma/` and uploads to S3 with a
date suffix. Acceptable: if we lose a day of Chroma, we just
re-embed from the captures table (which is the source of truth).

**3.5.2 Image backup.** Captured images are content-addressed by
SHA256 hash (per Phase 2 design). Sync `data/images/` to S3 nightly;
re-derive from URL fetch if a single file is lost.

**3.5.3 EBS snapshots (belt-and-suspenders).** Daily EBS snapshot
via **Data Lifecycle Manager**, retain 7. One CDK construct,
~free at this volume size, and it covers everything litestream
doesn't: Chroma between nightlies, the whisper model, Docker state,
host config. Litestream remains the primary recovery path; the
snapshot is the "I broke the box itself" path.

### 3.6 Secrets management: SSM Parameter Store (NOT Secrets Manager)

| Aspect | Decision |
|--------|----------|
| Secret store | **AWS SSM Parameter Store** with `SecureString` type (KMS-encrypted, **free**) |
| Secrets stored | `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `BACKEND_API_TOKEN` (the bearer token Chrome + Telegram bot send) |
| Naming | `/braintwin/prod/anthropic_api_key`, etc. |
| Why not Secrets Manager | Secrets Manager is $0.40/secret/month. SSM Parameter Store SecureString is functionally equivalent for our needs and **free**. The portfolio bullet "I used AWS-native secrets management" works equally for both. |
| Access | EC2 instance role with `ssm:GetParameter` on `/braintwin/prod/*` |

### 3.7 Auth: single Bearer token

| Aspect | Decision |
|--------|----------|
| Mechanism | Static API token in `Authorization: Bearer <token>` header |
| **Server side ships FIRST** | The FastAPI dependency that *checks* the token is an **M.1 deliverable** (~20 lines, testable locally with pytest), NOT an afterthought of the client cutover. v1 of this doc only had clients *sending* the header (M.7) with no milestone enforcing it server-side — which would have meant a publicly exposed `/recall` from M.3 to M.7: an open proxy to the Anthropic key and a readable corpus. Scanners find fresh public IPs within hours. Sequencing rule: **the API is never reachable from the open internet without auth.** Belt-and-suspenders: the security group stays operator-IP-only until M.7 anyway (§3.4). |
| CORS at cutover | Tighten `allow_origins` from `*` to `chrome-extension://<extension-id>` and ensure `Authorization` is in `allow_headers` — the bearer header makes requests non-simple, so preflights must pass. |
| Storage | Token generated once, stored in SSM Parameter Store, distributed to Chrome extension + Telegram bot via env config |
| Why not OAuth | Multi-user OAuth is overkill for single-user v1. Designing speculative auth is one of the easier ways to waste a week. When use case A goes live, we swap this out — the seam already lives in `/recall`'s `user_id=DEFAULT_USER_ID` line. |
| Token rotation | Manual for now. Operator runs a script that generates a new token + updates SSM + redistributes to clients. Phase 5+ automates. |

### 3.8 Monitoring & cost alerts

| Aspect | Decision |
|--------|----------|
| App logs | CloudWatch Logs, log group `/braintwin/prod/app`. 5 GiB free, well within budget. |
| Metrics | Built-in EC2 metrics (CPU, network) are free. For `/recall` request count + latency, query the structured app logs with **CloudWatch Logs Insights** instead of publishing custom metrics — custom metrics are $0.30/metric/month after the first 10, and at single-user volume Insights queries over the logs we already ship answer the same questions for $0. |
| Health check | A simple `GET /health` poll from an external uptime monitor (UptimeRobot free tier) — alerts to email if down |
| **Budget alerts** | **CRITICAL.** Set up via AWS Budgets at $50, $100, $150, $180 thresholds. Email alerts. Set within 5 minutes of account creation, before anything else. |
| Anthropic spend cap | Set in Anthropic dashboard separately. $20/month hard cap for v1. |

### 3.9 Whisper.cpp + yt-dlp on the cloud host

These are system binaries the Phase 2.5 hydration depends on.
Bundled inside the Dockerfile (§5.1) so they live in the container,
not on the host. Whisper model (~250 MB) sits on the EBS volume so
container restarts don't re-download it.

### 3.10 Process model: docker-compose is the deployment unit

v1 of this doc had two gaps here: the Telegram bot — the #1 pain
point in §1 — had **no deploy milestone at all** (it's a separate
`run_polling` process in `backend/telegram_bot/bot.py`, it does not
start with uvicorn), and Caddy's home was left as "sidecar or host?"
Both resolve the same way: **one `docker-compose.yml` on the EC2
host defines every process on the box.**

| Service | Image | Role |
|---------|-------|------|
| `app` | `braintwin` (ECR) | uvicorn, 1 worker (§3.1), port 8000 internal |
| `bot` | same image, different command | `python -m backend.telegram_bot.bot` — Telegram long-polling |
| `caddy` | `caddy:2` | TLS termination (DNS-01 via Cloudflare token), reverse-proxy → `app:8000` |
| `litestream` | `litestream/litestream` | continuous WAL replication → S3 |

All services `restart: unless-stopped`, env from SSM at boot.
One image, one compose file, four processes — the runbook story is
"the compose file IS the box's process tree." No multi-process
containers, no supervisord.

---

## 4. Infrastructure as Code — Terraform vs CDK

> **LOCKED 2026-06-09: AWS CDK with TypeScript.** The §4.3 analysis
> held up in review; the §4.4 "do both" split is rejected as
> overengineering for v1 (Terraform stays available as a later
> standalone portfolio piece). The trade-off analysis below is kept
> for the record.

### 4.1 The two real options

**Terraform (HCL)** — The market-standard IaC tool.

- ✅ Multi-cloud (AWS, Azure, GCP, Cloudflare, GitHub, almost anything)
- ✅ Universally recognized — every infra job description mentions it
- ✅ Declarative; state file is the source of truth
- ✅ Massive community modules library
- ⚠️ HCL is a custom DSL — limited programming logic
- ⚠️ State management is its own learning curve (S3 backend + DynamoDB lock)
- 📊 **Job postings mentioning Terraform:** dominant. Most "DevOps Engineer" / "Platform Engineer" listings list it as required.

**AWS CDK (TypeScript)** — Modern AWS-native IaC.

- ✅ Real programming language (TypeScript) — loops, conditions, modules, type checking
- ✅ Type safety catches mistakes at write time, not deploy time
- ✅ Constructs library lets you reuse infra patterns the way you'd reuse code
- ✅ Owned by AWS, gets new service support immediately
- ⚠️ AWS-only. If we ever go multi-cloud, CDK doesn't follow.
- ⚠️ Synthesizes to CloudFormation under the hood — CloudFormation's quirks become your quirks
- 📊 **Job postings mentioning CDK:** growing fast at modern AWS shops (Datadog, Stripe, fintechs). Less universal than Terraform but signals modern-AWS competence.

### 4.2 What this project is and what would suit it

BrainTwin is:

- AWS-only for the foreseeable future
- A single small stack (~10-12 resources)
- A learning-vehicle for the user
- Going to grow more sophisticated as multi-user / quizzes come online

Given the AWS-only commitment, CDK's "AWS-only" downside doesn't
bite us. CDK's upsides (real language, type safety, constructs) help
with the "will grow more sophisticated" reality.

### 4.3 My honest recommendation: **AWS CDK with TypeScript**

Three reasons:

1. **You'll learn more.** TypeScript-with-constructs is closer to the
   skills product-company senior engineers use day-to-day than HCL
   is. The "I wrote my infra as a TypeScript program" framing reads
   as more sophisticated than "I wrote .tf files."
2. **Type safety is real.** CDK catches "you forgot to attach the
   instance role" or "this security group rule has the wrong port"
   at `cdk synth` time. Terraform usually catches it at `terraform
   apply` time, which is slower feedback.
3. **AWS-only is fine for now.** When we ever want multi-cloud,
   we'd rewrite IaC anyway — Terraform's claim of "write once, run
   anywhere" is more theoretical than practical.

Counter-argument worth taking seriously: **Terraform's job-posting
dominance might matter more than CDK's modernness**. If the user is
applying primarily to companies with established platform teams,
"3 years Terraform" appears in more job specs than "CDK."

### 4.4 Recommended split (best of both?)

If we want both signals: **CDK for the BrainTwin stack**, then write
a small **Terraform module** for one specific concern (say, the S3
bucket + IAM policy + Cloudflare DNS records) as a separate repo to
have a "I've used Terraform" code sample.

This is overengineered for v1. My pragmatic vote: **CDK now,
Terraform later as a follow-up portfolio piece**.

### 4.5 What we DON'T do

- **No Pulumi.** Smaller community, harder to claim portfolio value.
- **No ClickOps.** Doing it via the AWS console is fine for a quick
  first deploy, but the moment we're tearing down + redeploying,
  IaC pays for itself. Goal: every resource in the cloud account
  is defined in code.

---

## 5. Deployment steps (high level)

Each step is its own milestone within Phase 4.0.6, in order.

### 5.1 M.1 — Containerize + server-side auth (laptop-only, no cloud yet)

- **Bearer-token auth dependency in FastAPI** (~20 lines): reads
  `BACKEND_API_TOKEN` from env; applied to `/capture` and `/recall`;
  `/health` stays open. If the env var is unset (local dev), auth is
  disabled — cloud always sets it. pytest coverage: 401 without
  header, 401 with wrong token, 200 with right token, /health open.
  **This ships before any cloud resource exists** (§3.7 sequencing
  rule).
- Write `Dockerfile`:
  - Starts from `python:3.11-slim`
  - Installs whisper.cpp + yt-dlp + ffmpeg
  - **CPU-only torch wheel** (`--index-url .../whl/cpu`) — halves image size
  - Installs Python deps from `requirements.txt`
  - Copies `backend/` + `scripts/`
  - Default command: `uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers 1`
- Build for **`linux/arm64`** via `docker buildx` (target is t4g, §3.1);
  verify it also runs on the laptop (amd64 or Apple Silicon) for local testing
- Write `docker-compose.yml` with the §3.10 service set — `app` and
  `bot` testable locally now; `caddy` + `litestream` slots filled in
  M.4/M.5
- Verify `pytest tests/` still passes inside the container
- Add `.dockerignore` to keep image size sane

**No cloud touched yet.** The compose file is the unit of deployment, validated locally.

### 5.2 M.2 — CDK scaffold (no deploy)

- `npm init` + `cdk init app --language typescript` in a new `infra/` directory
- Define the stack: VPC (default), Security Group (80/443 from operator IP only — no port 22, §3.4), EC2 `t4g.small` with user-data (Docker + compose install, SSM agent), EBS volume, **DLM daily snapshot policy (§3.5.3)**, Elastic IP, S3 bucket with **prefix-scoped lifecycle rules (§3.3)**, **ECR repo (§3.3.1)**, SSM parameters, CloudWatch log group, IAM role (SSM Parameter Store read + Session Manager + ECR pull + S3 litestream prefix write)
- No EC2 keypair resource exists in the stack — Session Manager only
- Run `cdk synth` — outputs CloudFormation template
- Cost estimate via hand-tally against §6 — should show <$30/month
- **Do not deploy yet.** Verify the synth looks right.

### 5.3 M.3 — First deploy

- **Account signup on the Paid Plan** (NOT Free Plan — §2.1; Free
  Plan accounts auto-close at 6 months). Budget alerts within the
  first 5 minutes (§3.8), before anything else.
- `aws configure` with credentials (use an IAM user, not root)
- `cdk bootstrap` (one-time per account/region)
- `cdk deploy` — creates everything; security group is operator-IP-only at this point
- Connect via **SSM Session Manager** (no SSH)
- `docker compose pull && docker compose up -d` — **`app` AND `bot`
  services both running** (the bot is why this phase exists; v1 of
  this doc forgot to deploy it)
- `BACKEND_API_TOKEN` set from SSM — auth enforced from the first boot
- Smoke test via `curl` (with and without the bearer token) from laptop;
  forward a Telegram message and confirm it lands in the captures table

### 5.4 M.4 — Caddy + Cloudflare + domain

- Add the `caddy` service to compose (§3.10)
- Cloudflare: zone for the domain, **proxied (orange-cloud)** A record
  → Elastic IP, SSL mode **Full (strict)**
- Scoped Cloudflare API token into SSM; Caddy issues the Let's Encrypt
  cert via **DNS-01** (HTTP-01 doesn't play well behind the proxy)
- Verify `https://braintwin.app/health` returns 200 through Cloudflare

### 5.5 M.5 — Litestream backup

- Add the `litestream` service to compose (own container, §3.10 —
  not bundled alongside uvicorn)
- Configure `litestream.yml` pointing at the S3 bucket's `litestream/`
  prefix; retention managed by litestream, not S3 lifecycle (§3.3)
- Confirm the app opens SQLite in WAL mode (litestream requirement)
- **Restore drill, with timings:** simulate DB loss, `litestream
  restore` to a fresh path, verify row counts match. Record "restore
  took X minutes" in the smoke-test doc — recoverability with numbers
  is the portfolio sentence.

### 5.6 M.6 — Monitoring + cost alerts

- AWS Budgets: $50 / $100 / $150 / $180 thresholds
- UptimeRobot: monitor `/health`, alert on 2+ consecutive failures
- CloudWatch dashboard with EC2 CPU, network, disk, plus app request count
- Anthropic dashboard: monthly spend cap

### 5.7 M.7 — Client cutover (Chrome extension) + open the front door

- **Centralize `BACKEND_URL` first** — it's currently duplicated in
  `extension/content.js` and `extension/recall.js`; move it to one
  shared config so this is the last time a URL change touches two files
- Point it at the cloud URL; add `Authorization: Bearer <token>` to
  all extension requests (the bot already runs server-side with its
  env config since M.3)
- Tighten backend CORS per §3.7 (extension origin, `Authorization`
  in allowed headers)
- Flip the security group from operator-IP-only to Cloudflare IP
  ranges (§3.4) — this is the moment the API faces the internet,
  and auth has been enforced since M.3
- Reload extension; test capture + recall end-to-end from the cloud

### 5.8 M.8 — Documentation + smoke test

- Write `docs/phase4.0.6-deployment-smoke-test.md` (numbered
  verification checklist, like phase3-smoke-test.md)
- Update README's "Setup" section to mention both local + cloud
- Run the smoke test end-to-end

---

## 6. High-level design (HLD) — diagrams in the codebase

> Added 2026-06-10 per request. The goal: one canonical architecture
> picture plus per-flow sequence diagrams, both lives in `docs/diagrams/`
> so they're version-controlled, PR-reviewable, and regenerable.

### 6.1 Three-layer diagram strategy

| Layer | Tool | What it captures | Output |
|-------|------|-----------------|--------|
| **Topology** | Python [`diagrams`](https://diagrams.mingrammer.com) library (mingrammer/diagrams) | The big static architecture picture: EC2, EBS, S3, ECR, SSM, CloudWatch, Cloudflare, Anthropic API. Official AWS icons. | `architecture.png` |
| **Flows** | [Mermaid](https://mermaid.js.org) (markdown-embedded sequence + flowchart diagrams) | Per-request lifecycle: capture path, recall path, refinement turn, failure modes (Sonnet down, Chroma down, etc.). GitHub renders natively. | inline in markdown |
| **Verification** | [`cdk-dia`](https://github.com/pistazie/cdk-dia) (post-M.2) | Auto-generated topology from the actual CDK code — proves the manual diagram matches what's deployed. | `cdk-generated.png` |

### 6.2 Why this combination

- **Python `diagrams`** wins on code-as-diagram + AWS icon fidelity.
  The script that produces the PNG IS the documentation. Edit the
  script, regenerate, PR with both the script change and the PNG diff.
- **Mermaid** wins on per-flow detail. Sequence diagrams for "user
  hits /recall → vector + BM25 → RRF → Sonnet → response" are the
  artifact a future contributor reads to understand what calls what.
  Mermaid renders in GitHub markdown so the doc itself is the view.
- **cdk-dia** wins on truth-vs-claim drift. The Python `diagrams`
  picture is the architect's intent; `cdk-dia` is what's actually
  in the cloud. If they diverge, something's drifted.

### 6.3 Why not Miro / Lucidchart / drawio

- **Miro** — Excellent whiteboard, terrible for repo. Not
  version-controlled, can't be reviewed in a PR, can't be grepped.
  Fine for ad-hoc brainstorming; rejected as the canonical source.
- **Lucidchart** — Same issues as Miro, plus paid.
- **drawio** — `.drawio` XML can be checked in, but the editor is
  GUI-only so PR review shows blob diffs, not human-readable changes.
  Acceptable backup if `diagrams`/Mermaid fail us, not the primary.

### 6.4 What lives where

```
docs/diagrams/
├── README.md                   # folder guide + regenerate/maintenance instructions
├── architecture.py             # Python `diagrams` script — source of truth for topology
├── architecture.png            # generated; committed alongside the script
├── flow-capture.md             # Mermaid sequence diagram for the /capture path
├── flow-recall.md              # Mermaid sequence diagram for /recall + Sonnet rerank
├── flow-refinement.md          # Mermaid for multi-turn refinement (U.3)
├── flow-failure-modes.md       # Mermaid for fault-isolated ranker degradation, Sonnet failures
├── flow-backup-restore.md      # Mermaid for the M.5 litestream + DLM restore drill
└── cdk-generated.png           # post-M.2: auto-generated by `cdk-dia` from the CDK code
```

### 6.5 Architecture script outline (to land in M.0)

The `architecture.py` script will use the official AWS provider in
`diagrams` and show the layered topology:

- **External actors:** Chrome extension, Telegram bot (mobile),
  external Anthropic API
- **Edge:** Cloudflare (DDoS / DNS / TLS)
- **Compute:** EC2 t4g.small in `us-west-2a` with docker-compose
  (`app`, `bot`, `caddy`, `litestream` services)
- **Storage:** EBS gp3 volume (SQLite + Chroma + whisper model),
  S3 bucket (litestream WAL prefix, nightly Chroma tarballs, image
  backups), ECR (the BrainTwin image)
- **Config / Secrets:** SSM Parameter Store
- **Observability:** CloudWatch Logs + Budgets + UptimeRobot (external)
- **Operator access:** SSM Session Manager (no SSH, no port 22)

Each node labelled with its CDK construct name so the picture maps
1:1 to `infra/lib/braintwin-stack.ts`.

### 6.6 Sequence diagrams that earn their keep

Five Mermaid flows worth the time, all in `docs/diagrams/flow-*.md`:

1. **Capture path** — Chrome extension → /capture → enrichment
   worker → SQL + Chroma + S3 (image upload)
2. **Recall happy path** — /recall → RetrievalService (Chroma +
   FTS5 in parallel) → RRF fusion → Sonnet rerank → response
3. **Recall refinement turn** — second call with conversation_id,
   showing the "no fresh retrieval" branch from U.3
4. **Failure mode: Sonnet 503** — degraded rule-based rerank,
   bm25-only / vector-only paths
5. **Backup + restore drill** — litestream WAL → S3, restore to a
   fresh EBS volume — for the M.5 restore exercise

These give a new contributor the on-ramp from "what is this thing"
to "I can find where to make my change" in 15 minutes.

### 6.7 Setup cost

- `pip install diagrams` + Graphviz installed on the dev laptop.
  One-time, ~5 min.
- Mermaid: zero setup; GitHub renders it natively.
- `cdk-dia`: deferred until after M.2 (CDK exists). Installed via npm,
  runs against the CDK assembly.

### 6.8 Maintenance contract

- The diagrams are source-of-truth for review. PR descriptions that
  add or remove infrastructure resources MUST update either
  `architecture.py` or the appropriate flow doc.
- `architecture.png` is regenerated on every change to
  `architecture.py` and both committed together.
- A CI check (Phase 4.0.6.1, after the GitHub Actions pipeline is up)
  will fail if `architecture.png` is stale relative to its script.

---

## 7. Budget reality check

Hard numbers (estimates; verify against the AWS calculator for
`us-west-2` before M.3):

| Item | $/month | Source |
|------|---------|--------|
| EC2 t4g.small 24/7 (`us-west-2`) | ~$12 | AWS pricing — within ~$1–2 of t3.micro thanks to Graviton pricing (§3.1) |
| EBS gp3 30 GiB | $2.40 | AWS pricing |
| EBS snapshots (DLM, 7-day retention) | ~$0.50 | incremental, small at this size |
| ECR private repo (~3 GB image) | ~$0.30 | $0.10/GB-month (§3.3.1) |
| Elastic IP (attached, free) | $0 | AWS pricing |
| S3 storage + requests | ~$1 | scale-dependent, low for v1 |
| Data transfer out | ~$1 | small, mostly Anthropic API calls |
| Route 53 / DNS | $0 | Cloudflare free |
| Caddy + Let's Encrypt | $0 | open-source |
| SSM Parameter Store + Session Manager | $0 | free tiers |
| CloudWatch Logs (+ Logs Insights queries) | $0 | well under 5 GiB free; no custom metrics (§3.8) |
| Domain | $1 | $12/year amortized; free via Student Pack |
| **TOTAL** | **~$15/month** | |

**Credit runway (corrected 2026-06-09 — v1 of this doc claimed 23
months, which was wrong):**

- Signup is on the **Paid Plan** (§2.1) — Free Plan accounts are
  auto-closed at 6 months, which kills a production system.
- Credits: $100 at signup + up to $100 for onboarding activities,
  expiring **~12 months after issuance**.
- GitHub Student Pack AWS credits: **not counted** until visible in
  the account (the AWS Educate grant path largely ended in 2023).
- $200 at ~$15/month covers ~13 months of spend, but the 12-month
  expiry is the binding constraint: **realistic runway ≈ 12 months
  at $0 out-of-pocket, then ~$15/month real money.**

Plus the Anthropic API costs (~$5-15/month for the user's usage),
which are unaffected by AWS deployment and are real money from
day one.

---

## 8. What's deferred to later sub-phases

| Item | When |
|------|------|
| Postgres migration | Phase 4.0.7 — when SQLite starts hurting or when use case A makes multi-user real |
| ECS Fargate | When monthly traffic justifies the cost upgrade |
| ALB + multi-AZ | When use case A goes live and reliability matters more than cost |
| GitHub Actions CI/CD pipeline | Phase 4.0.6.1 — once the manual deploy works, automate it |
| OAuth + per-user accounts | Phase 4.1 (use case A) |
| Production Langfuse self-hosted | Phase 4.0.5 (eval) — runs on the same EC2 or a sidecar |

---

## 9. Decisions explicitly NOT locked yet

These need your sign-off before M.1 starts (CDK-vs-Terraform was
resolved in review — locked to CDK TypeScript, §4; region resolved
2026-06-10 — locked to `us-west-2`, §3.0):

1. **Domain name** — needs you to claim one via Namecheap once
   Student Pack approves. **Preferred: `digitaltwin.app`** (see §11
   for the customer brand split); fallbacks `digitaltwin.me` /
   `digitaltwin.io`. `braintwin.*` is no longer the public-facing
   pick.
2. **CI/CD via GitHub Actions or manual `cdk deploy` for v1?** —
   manual is faster to ship; GitHub Actions adds resume value but
   is one more thing to break.

---

## 10. Success criteria for Phase 4.0.6

The phase is shippable when:

- `https://<your-domain>/recall` answers from the cloud, returns
  the same shape as local — and returns **401 without the bearer
  token**
- A capture posted from the Chrome extension to the cloud URL is
  retrievable via recall on the cloud URL within a minute
- A Telegram forward sent while the laptop is closed lands in the
  captures table (the §1 pain point, demonstrated)
- Litestream restore drill done **with recorded timings** — "DB
  restored to a fresh instance in X minutes" written into the
  smoke-test doc
- AWS Budgets has email alerts wired
- The infra is reproducible: `cdk destroy && cdk deploy` rebuilds
  the same stack from scratch (modulo data, which restores from
  S3)
- One operator runbook in `docs/phase4.0.6-deployment-smoke-test.md`
  has been followed end-to-end at least once

---

## 11. Product naming — DigitalTwin (public) vs BrainTwin (internal)

> Added 2026-06-10 per request. "DigitalTwin" reads more clearly to
> a stranger than "BrainTwin", which sounds biotech / clinical.
> But renaming the whole codebase is busywork that buys nothing —
> internal code, docs, branches, commit history all stay as
> **BrainTwin**. The split is at the presentation surface only.

### 11.1 The split

| Layer | Name | Why |
|-------|------|-----|
| Codebase / repo / commit messages / internal docs | **BrainTwin** | Stable identifier the team knows. Renames are a refactor tax with zero feature value. |
| Customer-facing UI (extension popup, web pages, Telegram bot greetings) | **DigitalTwin** | Cleaner brand. "Your digital twin remembers what you read" is a one-line elevator pitch. |
| Domain | **`digitaltwin.app`** (preferred) | Matches public brand. `.me` / `.io` are fallbacks. |
| AWS resources / CDK stack name | `BrainTwin*` | Internal — never seen by users. |

### 11.2 Files to flip when the brand surface changes

Only the following user-visible strings flip from "BrainTwin" to
"DigitalTwin". Everything else stays:

| File | Current string | New string |
|------|---------------|-----------|
| `extension/manifest.json` → `name` | `"BrainTwin"` | `"DigitalTwin"` |
| `extension/manifest.json` → `description` | `"Captures what you read…"` | unchanged, possibly soften wording |
| `extension/manifest.json` → `action.default_title` | `"BrainTwin"` | `"DigitalTwin"` |
| `extension/popup.html` → `<h1>BrainTwin</h1>` | `BrainTwin` | `DigitalTwin` |
| `app/main.py` → FastAPI `title=` | `"BrainTwin"` | `"DigitalTwin"` |
| `app/main.py` → `/health` response `{"service": "BrainTwin"}` | `BrainTwin` | `DigitalTwin` |
| `telegram_bot/bot.py` → greeting messages and `/start` text | `BrainTwin` | `DigitalTwin` |
| Cloudflare DNS / Caddy site block | `braintwin.*` | `digitaltwin.*` |

Anything not in that table — Python module names, AWS resource
names in CDK, README, CHANGELOG, internal phase docs (including
this one) — stays as BrainTwin.

### 11.3 What's explicitly NOT changing

- Repo name on GitHub — stays `BrainTwin`.
- Docker image names in ECR — stays `braintwin-*`.
- CDK stack names — stays `BrainTwinStack*`.
- SQLite DB filename, Chroma collection name — stay BrainTwin-flavored.
- S3 bucket names — stay BrainTwin-flavored.
- Python package layout (`app/`, `agents/`, `services/`) — stays.
- All phase doc filenames (`phase4.0.6-…md` etc.) — stay.

### 11.4 Migration order

The brand surface flip happens **after** the cloud deploy works, not
before. Concrete sequencing:

1. Phase 4.0.6 ships under whatever subdomain (`api.<your-domain>`).
   Internal name in code = BrainTwin; serves successfully.
2. You buy `digitaltwin.app` (or fallback) via Namecheap once
   Student Pack credit covers it.
3. Cloudflare DNS update — point `digitaltwin.app` and
   `api.digitaltwin.app` at the EC2.
4. Brand-surface PR: flip the rows in §11.2, ship Chrome extension v0.5
   to the store under the new public name.

This sequencing keeps M.1–M.6 about infra correctness, not branding.

### 11.5 If you change your mind later

If you ever do want to rename the codebase to DigitalTwin, the
refactor is mechanical: `git grep -i braintwin` gives the full
delta. Easier to defer that decision until you've shipped, gotten
real feedback on the name, and have headroom for cleanup work.

---

## 12. Next docs after Phase 4.0.6 ships

- `phase4.0.6-deployment-smoke-test.md` — the operator runbook
- `phase4.0.7-postgres-migration.md` — when SQLite starts hurting
- `phase4.0.5-eval-design.md` — now unblocked; eval runs against
  the cloud target, traces flow into Langfuse self-hosted on the
  same EC2

---

*Author: Sabya (with Claude as design partner). Decisions captured
2026-06-04 from conversation around AWS free tier restructure +
portfolio audience targeting. Revised 2026-06-10: region
(`us-west-2`), HLD diagram strategy, and DigitalTwin/BrainTwin
brand split.*
