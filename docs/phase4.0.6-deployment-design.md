# Phase 4.0.6 — Cloud Deployment (AWS)

> **Status as of 2026-06-04 — DESIGN IN REVIEW.**
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
2. **Recall is only usable when the laptop is on** — i.e., when the
   user is in front of the laptop, defeating most of the
   memory-prosthetic value.
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
- **Generous combined credits.** The new Free Plan (post-July 2025
  restructure) gives **$200 in credits over 6 months**. The
  GitHub Student Pack adds **~$100 more** in AWS credits when
  approved. Total: ~$300 of runway, enough to host BrainTwin's
  v1 architecture for ~12 months at $0 out-of-pocket.
- **Always-free services on top.** Lambda 1M req/month, DynamoDB
  25GB, CloudWatch metrics — perpetual, no expiry, on top of the
  $200.
- **Most mature deployment ecosystem.** Every IaC tool (Terraform,
  CDK, Pulumi) has first-class AWS support. Every CI/CD platform
  (GitHub Actions, CircleCI) has AWS deployment recipes.
- **Mumbai region (`ap-south-1`).** Sub-100ms latency for Indian
  capture / recall traffic.

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

### 3.1 Compute: EC2 `t3.micro` (single instance, all-in-one)

| Aspect | Decision |
|--------|----------|
| Instance type | `t3.micro` (2 vCPU burstable, 1 GiB RAM) |
| Region | `ap-south-1` (Mumbai) |
| AZ | Single AZ — no multi-AZ until use case A goes multi-user |
| OS | Amazon Linux 2023 |
| Why not ECS Fargate | Fargate is NOT in the free tier — costs ~$9/month minimum. EC2 t3.micro at ~$7.50/month after free credits is cheaper AND the "I ran a containerized FastAPI app on EC2 + Docker" bullet is still solid resume material. |
| Why not t3.small | Doubles cost (~$15/month). 1 GiB is tight but we have swap (§3.6) to compensate. |
| RAM strategy | 2 GiB swap file on EBS. Lets sentence-transformers + Chroma + whisper.cpp coexist; rare swap-thrash is acceptable for single-user load. |

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
| Lifecycle | Move backups to Glacier IA after 30 days, delete after 180 days |
| What goes here | Litestream's SQLite WAL stream, image uploads, the Chroma index nightly snapshot |
| Why not EFS | EFS is overkill for single-instance access. S3 is cheaper for backup-shaped workloads. |

### 3.4 Networking & HTTPS

| Aspect | Decision |
|--------|----------|
| Subnet | Single public subnet — NO private subnet, NO NAT Gateway. NAT Gateway is $32/month and would dominate the bill. |
| Security group | Allow inbound 22 (SSH from operator IP only), 80, 443. Allow all outbound. |
| Public IP | **Elastic IP** attached to the instance (free as long as it's attached; $4/month if unattached and idle). |
| HTTPS terminator | **Caddy** running on the EC2 box itself. Auto-renews Let's Encrypt certs. Reverse-proxies to the FastAPI container. |
| Why not ALB | ALB is $16/month minimum. For a single backend on a single box, Caddy + Let's Encrypt is functionally equivalent and free. |
| DNS | **Cloudflare** in front (free tier). Gives DDoS protection + DNS + caching. Domain via Namecheap (~$10/year, free via Student Pack). |
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
| Storage | Token generated once, stored in SSM Parameter Store, distributed to Chrome extension + Telegram bot via env config |
| Why not OAuth | Multi-user OAuth is overkill for single-user v1. Designing speculative auth is one of the easier ways to waste a week. When use case A goes live, we swap this out — the seam already lives in `/recall`'s `user_id=DEFAULT_USER_ID` line, and adding a FastAPI auth dependency is a one-file change. |
| Token rotation | Manual for now. Operator runs a script that generates a new token + updates SSM + redistributes to clients. Phase 5+ automates. |

### 3.8 Monitoring & cost alerts

| Aspect | Decision |
|--------|----------|
| App logs | CloudWatch Logs, log group `/braintwin/prod/app`. 5 GiB free, well within budget. |
| Metrics | Built-in EC2 metrics (CPU, network) + custom CloudWatch metrics for `/recall` request count + latency (already structured in main.py logs) |
| Health check | A simple `GET /health` poll from an external uptime monitor (UptimeRobot free tier) — alerts to email if down |
| **Budget alerts** | **CRITICAL.** Set up via AWS Budgets at $50, $100, $150, $180 thresholds. Email alerts. Set within 5 minutes of account creation, before anything else. |
| Anthropic spend cap | Set in Anthropic dashboard separately. $20/month hard cap for v1. |

### 3.9 Whisper.cpp + yt-dlp on the cloud host

These are system binaries the Phase 2.5 hydration depends on.
Bundled inside the Dockerfile (§5.1) so they live in the container,
not on the host. Whisper model (~250 MB) sits on the EBS volume so
container restarts don't re-download it.

---

## 4. Infrastructure as Code — Terraform vs CDK

This is the most open decision in the doc; here's the honest case
for each so we can pick.

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

### 5.1 M.1 — Containerize the app (laptop-only, no cloud yet)

- Write `Dockerfile` that:
  - Starts from `python:3.11-slim`
  - Installs whisper.cpp + yt-dlp + ffmpeg
  - Installs Python deps from `requirements.txt`
  - Copies `backend/` + `scripts/`
  - Runs `uvicorn backend.main:app --host 0.0.0.0 --port 8000`
- Write `docker-compose.yml` for local testing with mounted volumes
- Verify `pytest tests/` still passes inside the container
- Add `.dockerignore` to keep image size sane

**No cloud touched yet.** This is the unit of deployment, validated locally.

### 5.2 M.2 — CDK scaffold (no deploy)

- `npm init` + `cdk init app --language typescript` in a new `infra/` directory
- Define the stack: VPC (default), Security Group, EC2 instance with user-data, EBS volume, Elastic IP, S3 bucket, SSM parameters, CloudWatch log group, IAM role attached to EC2
- Run `cdk synth` — outputs CloudFormation template
- Cost estimate via `cdk-cost-estimator` or hand-tally — should show <$30/month
- **Do not deploy yet.** Verify the synth looks right.

### 5.3 M.3 — First deploy

- `aws configure` with credentials (use the IAM user, not root)
- `cdk bootstrap` (one-time per account/region)
- `cdk deploy` — creates everything
- SSH into the new EC2 box
- Install Docker (likely via user-data in the CDK), pull the BrainTwin image from a private ECR repo
- Run the container with `--restart unless-stopped` and env vars from SSM
- Smoke test via `curl` from laptop

### 5.4 M.4 — Caddy + Cloudflare + domain

- Add Caddy to the Dockerfile as a sidecar or run it on the host
- Point Cloudflare DNS at the Elastic IP
- Let's Encrypt auto-issues cert
- Verify `https://braintwin.app/health` returns 200

### 5.5 M.5 — Litestream backup

- Install litestream binary in the container
- Configure `litestream.yml` pointing at the S3 bucket
- Run `litestream replicate` alongside uvicorn (supervisor or systemd-style)
- Test: simulate DB loss, run `litestream restore`, verify recovery

### 5.6 M.6 — Monitoring + cost alerts

- AWS Budgets: $50 / $100 / $150 / $180 thresholds
- UptimeRobot: monitor `/health`, alert on 2+ consecutive failures
- CloudWatch dashboard with EC2 CPU, network, disk, plus app request count
- Anthropic dashboard: monthly spend cap

### 5.7 M.7 — Update Chrome extension + Telegram bot

- Change `BACKEND_URL` in `extension/content.js` and `extension/recall.js`
  to the cloud URL
- Add `Authorization: Bearer <token>` header to all requests
- Same for the Telegram bot's env config
- Reload extension; test end-to-end

### 5.8 M.8 — Documentation + smoke test

- Write `docs/phase4.0.6-deployment-smoke-test.md` (numbered
  verification checklist, like phase3-smoke-test.md)
- Update README's "Setup" section to mention both local + cloud
- Run the smoke test end-to-end

---

## 6. Budget reality check

Hard numbers based on the AWS new free tier (post-July 2025):

| Item | $/month | Source |
|------|---------|--------|
| EC2 t3.micro 24/7 | $7.50 | AWS pricing |
| EBS gp3 30 GiB | $2.40 | AWS pricing |
| Elastic IP (attached, free) | $0 | AWS pricing |
| S3 storage + requests | ~$1 | scale-dependent, low for v1 |
| Data transfer out | ~$1 | small, mostly Anthropic API calls |
| Route 53 / DNS | $0 | Cloudflare free |
| Caddy + Let's Encrypt | $0 | open-source |
| SSM Parameter Store | $0 | free up to 10K params |
| CloudWatch Logs | $0 | well under 5 GiB free |
| Domain | $1 | $12/year amortized; free via Student Pack |
| **TOTAL** | **~$13/month** | |

**Credit runway:**

- AWS Free Plan: $200 / 6 months
- GitHub Student Pack AWS: ~$100 (after approval)
- **Combined: ~$300**
- **Runway at $13/month: ~23 months**

Plus the Anthropic API costs (~$5-15/month for the user's usage),
which are unaffected by AWS deployment.

---

## 7. What's deferred to later sub-phases

| Item | When |
|------|------|
| Postgres migration | Phase 4.0.7 — when SQLite starts hurting or when use case A makes multi-user real |
| ECS Fargate | When monthly traffic justifies the cost upgrade |
| ALB + multi-AZ | When use case A goes live and reliability matters more than cost |
| GitHub Actions CI/CD pipeline | Phase 4.0.6.1 — once the manual deploy works, automate it |
| OAuth + per-user accounts | Phase 4.1 (use case A) |
| Production Langfuse self-hosted | Phase 4.0.5 (eval) — runs on the same EC2 or a sidecar |

---

## 8. Decisions explicitly NOT locked yet

These need your sign-off before M.1 starts:

1. **CDK TypeScript vs Terraform** — see §4. My vote: CDK now,
   Terraform as a follow-up portfolio side-quest.
2. **Domain name** — needs you to claim one via Namecheap once
   Student Pack approves. `braintwin.app` / `braintwin.me` /
   `braintwin.in` are all candidates.
3. **Region final** — `ap-south-1` (Mumbai) is the assumption.
   Verify latency from your typical work location is acceptable
   (it should be).
4. **CI/CD via GitHub Actions or manual `cdk deploy` for v1?** —
   manual is faster to ship; GitHub Actions adds resume value but
   is one more thing to break.

---

## 9. Success criteria for Phase 4.0.6

The phase is shippable when:

- `https://<your-domain>/recall` answers from the cloud, returns
  the same shape as local
- A capture posted from the Chrome extension to the cloud URL is
  retrievable via recall on the cloud URL within a minute
- Litestream restore verifies — can recover the DB to a fresh EC2
  instance in under 5 minutes
- AWS Budgets has email alerts wired
- The infra is reproducible: `cdk destroy && cdk deploy` rebuilds
  the same stack from scratch (modulo data, which restores from
  S3)
- One operator runbook in `docs/phase4.0.6-deployment-smoke-test.md`
  has been followed end-to-end at least once

---

## 10. Next docs after Phase 4.0.6 ships

- `phase4.0.6-deployment-smoke-test.md` — the operator runbook
- `phase4.0.7-postgres-migration.md` — when SQLite starts hurting
- `phase4.0.5-eval-design.md` — now unblocked; eval runs against
  the cloud target, traces flow into Langfuse self-hosted on the
  same EC2

---

*Author: Sabya (with Claude as design partner). Decisions captured
2026-06-04 from conversation around AWS free tier restructure +
portfolio audience targeting.*
