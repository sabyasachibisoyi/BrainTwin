# Phase 4.0.6.1 — Production polish

> **Status:** design, not yet started. Created 2026-06-19 right after
> Phase 4.0.6 shipped to production. Scope is intentionally bounded
> at "smooth the rough edges from 4.0.6" — not "build new features."
> If a polish item grows feature-shaped, kick it to 4.0.7 or 4.1.

## 0. Why polish at all (and why now)

Phase 4.0.6 shipped the BrainTwin product to AWS with TLS,
authenticated origin pulls, backups, restore drills, and OS-level
monitoring. It works. But the M.5 deploy exposed five real
operational gaps that landed as "known good enough, fix later" items
in the §14 invariants section of the main design doc:

1. Routine SSM-parameter additions still require an instance
   replacement (no discovery pattern in the refresh script)
2. We have OS metrics but **zero app-level metrics** — recall
   latency, capture error rate, Anthropic API timing all invisible
3. The user-data script is at 16 KB minus ~150 bytes — one more
   modest change and the deploy breaks on AWS's user-data limit
4. The bot reports `unhealthy` because the inherited Dockerfile
   healthcheck targets uvicorn on :8000 (but the bot is a Telegram
   polling worker, not a web server)
5. CI/CD is manual — every deploy depends on the operator
   remembering the right `--context imageTag=` value and the EBS
   deadlock workaround

Each of these is *small individually*. The reason to bundle them
into a sub-phase is that they share infrastructure (compute.ts,
the refresh script, the deploy.sh entry point) and shipping them
piecemeal would mean three separate deploys with three separate
EBS-deadlock dances. As a polish bundle, the s3.Asset refactor
(M.12) provides headroom; the discovery pattern (M.10) eliminates
the deadlock for SSM changes; and from there everything else is
refresh-only or compose-only.

Doing this *now* — before the eval work (Phase 4.0.5) — is right
because the polish involves the same files we just touched. The
context cost of relearning compute.ts in three weeks is higher than
the cost of finishing it this week.

---

## 1. What's in scope

Five milestones, in dependency order:

| Milestone | What | Where | Risk |
|-----------|------|-------|------|
| **M.0 — bot healthcheck** | Override the inherited Dockerfile healthcheck with a `pgrep`-based process check | `compute.ts` (compose template) + new test | Trivial. Ships via refresh, no instance replacement. |
| **M.12 — s3.Asset refactor** | Move heavy bash scripts (Chroma backup, CW Agent install + config, Caddyfile) out of user-data into S3 assets that user-data downloads on boot | `compute.ts` + new S3 asset bucket grant | Medium. Touches user-data — requires the EBS-deadlock dance once. Pays back ~5-8 KB of headroom. |
| **M.10 — discovery refresh script** | Refresh script reads all `/braintwin/*` SSM params via `get-parameters-by-path` at runtime and templates them into the secrets.env | `compute.ts` (refresh script template) | Medium. Touches user-data → one more deadlock dance. After this, new SSM params ship via `put-secrets.sh` + RunCommand alone. |
| **M.13 — GitHub Actions CI/CD** | Workflow runs tests, builds + pushes images to ECR, calls `cdk deploy` on a release tag | New `.github/workflows/deploy.yml` in both repos | Low for the workflow itself. Medium for the secrets handoff (OIDC vs static keys — see §4). |
| **M.11 — app-level metrics** | FastAPI middleware emits per-route latency + error count to CloudWatch via EMF; a new CloudWatch dashboard surfaces it | `BrainTwin/backend/observability/emf.py` (new) + dashboard JSON in CDK observability construct | Low. App-only change, ships via `build-and-push` + refresh. No CDK churn beyond the dashboard. |

## 1.1 What's deliberately NOT in scope

- **Postgres migration** → Phase 4.0.7
- **Cross-region S3 replication for Litestream** → only matters if
  multi-region becomes a real reliability goal; today it's not
- **Per-user data isolation** → Phase 4.1 (use case A)
- **Langfuse self-hosted** → Phase 4.0.5 (next phase after this)
- **Anything that touches the data model** → app-side change, not
  polish
- **Multi-AZ EC2** → §13 of main design doc, only when one EC2
  hurts. Not yet.

---

## 2. Milestone detail

### 2.0 M.0 — bot healthcheck fix

The bot service inherits the app Dockerfile's
`HEALTHCHECK curl localhost:8000/health` even though the bot
doesn't run a web server. Result: `docker compose ps` perpetually
shows `Up (unhealthy)`, the `condition: service_healthy`
dependencies from §M.3.a aren't satisfied for "bot is dependency,"
and a future Prometheus or CloudWatch healthcheck integration
would alert false-positive forever.

**Patch:** in the compose template under `bot:`, add:

```yaml
    healthcheck:
      test: ["CMD-SHELL", "grep -l backend.telegram_bot /proc/*/cmdline >/dev/null 2>&1"]
```

**Why grep-on-/proc and not pgrep:** the first attempt used `pgrep -f`,
which is the more conventional answer. But the `python:slim` base
image we use doesn't ship `procps` — `pgrep` is missing, the
healthcheck always errors, the bot stays unhealthy. `/proc/<pid>/cmdline`
is a kernel-provided pseudo-file present on every running Linux
container; grepping it for the bot's command-line substring works
without adding any image dependencies. The match pattern is broad
enough to survive a future submodule rename inside
`backend.telegram_bot` but specific enough to never false-positive
on another container. Defaults for interval / timeout / retries
(30s / 30s / 3) are inherited from docker compose — explicit lines
elided to save user-data bytes.

**Test:** add an assertion in `compute.test.ts` that the bot service
block contains both `/proc/*/cmdline` and `backend.telegram_bot`.

**Ship path:** `./scripts/deploy.sh` — user-data changes, so this
*is* the EBS-deadlock dance once. If we bundle M.0 with M.12
(below), it's one dance instead of two.

### 2.1 M.12 — s3.Asset refactor

The 16 KB user-data limit is a real constraint and we're 150 bytes
under it. The CW Agent JSON config, the Chroma backup script, the
Caddyfile, and the Litestream YAML together consume ~6 KB of that
budget. None of them are templated against runtime values that
weren't already available at synth time — they're inert config
files we *happen* to be emitting from user-data because the
original M.3 build did it that way.

Move them to `s3.Asset` constructs. CDK uploads each asset to a
versioned S3 prefix in the bootstrap bucket; user-data downloads
them at boot via `aws s3 cp` (the instance role already has S3
read, see storage.ts).

**Files to externalize:**
- `/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json`
- `/usr/local/bin/braintwin-chroma-backup.sh`
- `/etc/systemd/system/braintwin-chroma-backup.{service,timer}`
- `/etc/caddy/Caddyfile`
- `/etc/braintwin/litestream.yml`
- `/etc/braintwin/docker-compose.yml` (the static skeleton; the
  refresh script still templates the dynamic bits)

**What stays in user-data:**
- Base package install (apt-get, Docker repo)
- EBS mount + ownership
- Caddy AOP cert download (genuinely runtime — Cloudflare URL)
- The refresh script (executes dynamic SSM-templated values; can
  fetch the static templates from S3)
- The boot-time `/usr/local/bin/braintwin-refresh.sh` execution

**Expected savings:** ~6-8 KB. Comfortably under 8 KB user-data
afterward. That's room for three more years of normal infra
evolution.

**Risk:** medium. We need to be careful that the S3 asset paths in
user-data resolve correctly at boot (they're CDK tokens) and that
the IAM grant covers the bootstrap bucket's asset prefix, not just
our state bucket.

**Tests:** assert that the relevant files no longer appear in the
synthesized user-data; assert the new `aws s3 cp` lines do; assert
the asset bundle hash is deterministic (regression-friendly).

### 2.2 M.10 — discovery refresh script

Today the refresh script hardcodes the five SSM parameter names
(image_tag, caddy_image_tag, anthropic_key, bearer_token,
telegram_token, cloudflare_api_token, allowed_telegram_user_ids).
Adding a sixth requires editing user-data → instance replacement
→ EBS-deadlock dance.

The discovery pattern: refresh script reads everything under
`/braintwin/*` at boot/refresh time and renders into the env file.

```bash
# Inside braintwin-refresh.sh (sketch)
aws ssm get-parameters-by-path \
  --path /braintwin/ \
  --recursive \
  --with-decryption \
  --query 'Parameters[*].[Name,Value]' \
  --output text |
  while IFS=$'\t' read -r name value; do
    key=$(basename "$name" | tr '[:lower:]-' '[:upper:]_')
    echo "$key=$value" >> /etc/braintwin/secrets.env.tmp
  done
```

After this lands, adding a new secret is a three-command flow with
zero instance churn:

```bash
./scripts/put-secret.sh new_thing_key  # adds /braintwin/new_thing_key
./scripts/refresh.sh                   # SSM RunCommand → refresh
# Restart only the container that needs the new env var
```

The downside: the secrets.env file becomes the source of truth for
*what env vars exist* rather than the CDK. We trade explicitness
for flexibility. For a 1-2 person project that trade is the right
call; in a 20-person org you'd want the explicit list.

**Test:** new tests in `compute.test.ts` that the refresh script
uses `get-parameters-by-path` with the right path + flags, that
the image-tag params are filtered out of the loop, that the three
alias renames are present, and that the defensive grep-check
covers all five expected env var names. New test in
`secrets.test.ts` that the IAM grant scopes `ssm:GetParametersByPath`
to `/braintwin/*` (and not `*`).

**What shipped (2026-06-23):**

| Decision | What we did |
|----------|------------|
| Two-phase or full migration | **Full migration to discovery.** Backwards-compat hardcoded fetches would have been confusing dead code. The grep-check defence catches the worst-case "missing param" failure mode loudly. |
| Where the env-var rename happens | **Refresh script alias table.** Three renames live inline as a `case "$key" in ANTHROPIC_KEY) key=ANTHROPIC_API_KEY ;; …` block. The alternative — renaming the SSM params themselves — would have required a deploy that touches both put-secrets.sh and the app's env-var contract. The alias keeps the SSM-side stable. |
| Skipping the image-tag params | **`case $base in image_tag\|caddy_image_tag) continue ;;`** at the top of the loop. They live under `/braintwin/` so `get-parameters-by-path --recursive` returns them; without the skip they'd leak into secrets.env as `IMAGE_TAG=` lines. |
| Defensive "all required keys present" check | **Post-loop `for k in … ; do grep -q "^${k}=" ; done` with FATAL exit.** Catches the "forgot put-secrets.sh on a fresh account" or "typo on rotation" cases at boot rather than letting a container start with a missing env. |
| User-data byte cost | **+459 bytes vs M.12** (15,504 → 15,963; margin 880 → 421 bytes). The discovery loop is slightly larger than 5 hardcoded fetches because of the case statement + defensive check. Acceptable. |

**Risk:** medium. Same EBS-deadlock dance as M.12. After M.10
lands, adding a 6th secret is `./scripts/put-secrets.sh new_thing
+ ./scripts/deploy.sh` — refresh-only, no instance churn. Renaming
an EXISTING secret's env-var name still touches the alias table
(and thus user-data); design doc §14.9 records that nuance.

### 2.3 M.13 — GitHub Actions CI/CD

Manual deploys today follow:

```bash
cd BrainTwin && ./scripts/build-and-push.sh    # ECR push
cd ../BrainTwinCDK && ./scripts/deploy.sh      # cdk deploy with --context imageTag=<tag>
```

The risks: forgetting to run tests first, deploying the wrong
imageTag, forgetting the Caddy image bump when Caddy changes.

**Target workflow** (one in each repo):

**BrainTwin/.github/workflows/build-and-push.yml** — runs on
tagged commits matching `v*`:
1. Checkout
2. Set up Python + ruff/mypy/pytest
3. Run the backend test suite
4. Configure AWS credentials via OIDC (no static keys)
5. ECR login
6. Build + push the app image with tag `<sha>-<git tag>`
7. Output the imageTag to the workflow summary

**BrainTwinCDK/.github/workflows/deploy.yml** — runs on the same
tag pattern:
1. Checkout
2. Set up Node + `npm ci`
3. `npm test` (the jest suite)
4. Configure AWS credentials via OIDC
5. `./scripts/deploy.sh` (which reads the imageTag from the latest
   ECR image with that tag pattern)
6. Run a smoke check: `curl -fsS https://api.braintwin.net/health`
   with the bearer token

**OIDC setup (one-time):**
- Create an IAM OIDC provider for GitHub Actions
- Create a deploy role with `cdk-deploy`-scoped permissions, trust
  policy restricted to specific repo + branch
- No long-lived AWS keys in GitHub Secrets

**Test:** workflow files validate via `actionlint`. Lower priority
to *test* — these run in CI itself.

**Risk:** low for the workflow YAML; medium for the OIDC trust
config (a too-loose trust policy lets any GitHub repo assume the
role). Mitigation: review the trust policy condition closely.

**Time:** 1 day. Could be 2 if we hit OIDC trust-policy snags.

### 2.4 M.11 — app-level metrics via EMF

We have OS metrics (CPU, memory, disk) via CloudWatch Agent. We
have nothing on:

- /capture endpoint latency p50/p95/p99
- /recall endpoint latency p50/p95/p99
- Per-endpoint error rates
- Anthropic API call latency
- Anthropic API token consumption per request
- Chroma query latency
- SQLite query timing

EMF (Embedded Metric Format) lets the app log JSON with a special
`_aws.CloudWatchMetrics` block; CloudWatch picks the metrics out of
the log line at ingestion. No extra API calls, no separate metric
publishing path. The same log group already in use just gets richer.

**File to add:** `backend/observability/emf.py`

```python
# Sketch
class EMFMiddleware:
    async def __call__(self, request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        latency_ms = (time.monotonic() - start) * 1000
        log_emf(
            namespace="BrainTwin/App",
            dimensions={"route": request.url.path, "method": request.method, "status": str(response.status_code)},
            metrics={"latency_ms": latency_ms, "count": 1},
        )
        return response
```

Wrap Anthropic and Chroma calls with a context manager that emits
similar EMF lines. (Keep the surface small for v1.)

**Dashboard:** add a CloudWatch dashboard to the Observability
construct in CDK — widgets for latency p95 per route, error rate
per route, Anthropic token spend per hour.

**Tests:** unit test that EMF middleware emits the right log
structure given a synthetic request. Snapshot test the dashboard
JSON.

**Risk:** low. App-only code path; no infra churn beyond the
dashboard (which is a refresh-friendly resource).

**Time:** 2 days including the dashboard.

### 2.4.1 What shipped (2026-06-23)

Hit all four checkpoints from the design above. Notes on each
choice:

| Choice | What we did |
|--------|-------------|
| EMF lib vs raw JSON | **Raw JSON.** ~150 lines in `backend/observability/emf.py` (emit_metric helper, `timed` async context manager, `EMFMiddleware` ASGI middleware). Zero new deps. |
| Where the middleware lands | **`app.add_middleware(EMFMiddleware)` AFTER CORSMiddleware** in `backend/main.py`. Order matters — middlewares apply in reverse-add, so EMFMiddleware wraps CORS too, capturing CORS rejection latency and status in the metric. |
| What gets instrumented | **3 surfaces:** HTTP via middleware (every request), Anthropic via `timed(...)` around the two `messages.create` calls in LLMClient (`enrich` + `complete_json`), Chroma via `timed(...)` around the vector query in `RetrievalService._run_vector`. BM25 stays uninstrumented — it's just SQLite, well-covered by existing logs. |
| Where errors land | **`error` dimension on `timed` context manager.** "none" on success, `type(exc).__name__` on raise. Dashboard error widgets filter on `error != "none"` rather than maintaining a separate counter. |
| Dimensions | HTTP: `route` (template, NOT raw path — `/items/{id}` not `/items/42`), `method`, `status`. Anthropic: `endpoint` (enrich vs complete_json), `model`, `error`. Chroma: `collection`, `top_k`, `error`. |
| Dashboard structure | One CDK dashboard named `BrainTwin-App`, 3 rows: HTTP latency (`/capture`, `/recall` side-by-side, p50/p95/p99), request count + 5xx by route, Anthropic latency by endpoint + Chroma latency. Output `AppDashboardUrl` gives the operator a click-through URL. |
| Token-count tracking | **Deferred.** Anthropic's `response.usage` has input/output token counts; `emit_metric` already accepts `extra_metrics`. Wiring is ~5 lines and would let us graph spend per route. Deferred only because it adds Anthropic-SDK coupling to the metric path (`response.usage.input_tokens`) — worth doing but worth its own small PR. |
| Test surface | 9 EMF behaviour checks (3 emit_metric, 3 timed, 3 middleware) + 4 CDK dashboard regression tests (count, namespace+metric names, pinned dims, output URL). Stdlib smoke-script in this commit confirmed all 9; pytest run on the Mac is the canonical verification. |
| User-data impact | **Zero.** App-only change. The dashboard is a CFN resource that drops in via routine refresh deploys — no EBS-deadlock dance. |

### 2.4.2 Follow-up (2026-07-01) — Anthropic AuthenticationError alarm

The M.11 dashboard makes failures *visible* (widget row 3.5, "Anthropic
errors by class") but doesn't *page* on them. Anthropic credit is
recharged manually — no auto-renewal, deliberate blast-radius cap —
so a silent "credit hit zero at 2am" would surface as a friend
saying "recall doesn't work anymore." The alarm closes that gap.

| Choice | What we did |
|--------|-------------|
| Alarm scope | **`error=AuthenticationError` only.** Anthropic SDK maps 401 → `AuthenticationError`; that's what surfaces when prepaid credit is exhausted OR the API key is invalid/revoked. Deterministic, needs human action, very low false-positive rate. RateLimit / connection errors self-recover and aren't alarm-worthy. |
| Metric shape | **`FILL(m1, 0) + FILL(m2, 0)` MathExpression** over two explicit `Metric` refs — one per `(endpoint, model)` tuple (`enrich × Haiku` and `complete_json × Sonnet`), each with `statistic=SampleCount` and `dimensions.error=AuthenticationError`. First tried `SUM(SEARCH(...))` — CloudFormation rejected it at deploy time with *"SEARCH is not supported on Metric Alarms"* (alarms need deterministic bindings; SEARCH's variable metric set fails that contract). SEARCH-based widgets stay on the dashboard where they're fine. Model-name coupling is the trade-off: a Sonnet/Haiku bump needs updating **both** the alarm metric and the row-3 success-latency widget (both places already pin the same strings; `backend/config.py` docstring flags the coupling). |
| Threshold | **≥ 1 in a 5-minute window** (`evaluationPeriods=1`, `datapointsToAlarm=1`, `GreaterThanOrEqualToThreshold=1`). Any auth error is real. `treatMissingData: NOT_BREACHING` so the alarm stays OK when the app is idle (rare-event metric). |
| Delivery | **New SNS topic** `BrainTwin-Alerts` in the observability construct. Alarm action wires to it; email subscription is auto-added if `BRAINTWIN_ALERT_EMAIL` is set at synth time (same env var Budgets uses — one inbox for all ops alerts). If unset the topic exists with no subscribers so the alarm is still visible in the console. |
| Fix path baked into the description | Alarm `AlarmDescription` names the two most-likely causes and links `https://console.anthropic.com/settings/billing` — the SNS email body then reads as "here's what to do" rather than "here's a state change." |
| Dashboard widget delta | New row 3.5 renders SampleCount per 5-min window for **five** error classes (AuthenticationError, RateLimitError, APIConnectionError, APIStatusError, BadRequestError) via SEARCH expressions. Shows *why* calls are failing when the alarm fires — a real "credit out" event should light up AuthenticationError specifically, not the others. If credit-exhaustion ever surfaces as a different class (SDK-version drift, Anthropic-side error-code changes), the widget shows which class to swap into the alarm. |
| Cost impact | Alarm past the 10-free tier: **~$0.10 / month**. Zero-subscriber SNS topic: free. Total additional CloudWatch bill: ~$0.10 / month, comfortably inside task #87's ceiling. |
| Test surface | 10 new CDK tests: SNS topic present, no subscription when env var unset (jest.setup.ts pins `""`), alarm count, name, MathExpression contains `SEARCH`+`anthropic_latency_ms`+`AuthenticationError`+`SampleCount`, threshold/comparison/eval periods, `NOT_BREACHING`, alarm action references the topic, description mentions credit + `console.anthropic.com`, outputs advertise both topic ARN and alarm name. All pass alongside the existing 28. |
| User-data impact | **Zero.** Ships via routine `cdk deploy` refresh — no EC2 replacement, no EBS-deadlock dance. |
| Deploy prerequisite | Set `BRAINTWIN_ALERT_EMAIL=sabya.bisoyi@gmail.com` before `./scripts/deploy.sh`. Same env var also fixes the pre-existing "budget alerts going to `@example.invalid`" warning that has been dormant since M.2.h. Two birds, one deploy. |

Files touched: `lib/constructs/observability.ts` (imports + two public
fields + dashboard row + SNS topic + alarm + outputs);
`test/constructs/observability.test.ts` (10 new tests in a new
`Anthropic AuthenticationError alarm` describe block + 1 extended
dashboard test for the error widget). Nothing in `BrainTwin/backend/`
— the metric was already emitted by M.11's `timed()` wrapper; this
delta just makes it visible + actionable.

---

## 3. Sequencing and dependencies

The right order is:

```
   M.0 ──┐
   M.12 ─┼─── single user-data deploy ─── EBS deadlock dance ─── unlocked
   M.10 ─┘                                                          │
                                                                    ▼
                                          M.13 (CI/CD) ←────────────┤
                                                                    │
                                          M.11 (metrics) ←──────────┘
```

M.0 + M.12 + M.10 bundle into **one** user-data deploy so we pay
the EBS-deadlock cost exactly once. After that everything ships
via refresh.

M.13 and M.11 are independent and parallelizable, but in practice
do M.13 first — once CI/CD is wired, the M.11 ship loop becomes
"merge → tag → done" instead of "merge → build locally → push →
deploy locally."

## 3.1 Deploy plan for the user-data bundle

1. Make the three changes in source (M.0, M.12, M.10).
2. Run `npm test` — all tests green.
3. Run `cdk diff` — confirm the change is what we expect.
4. Run `./scripts/deploy.sh`. Expect the deploy to fail with the
   EBS-already-attached error on the new instance.
5. Terminate the old instance:
   `aws ec2 terminate-instances --instance-ids <i-old> --profile braintwin --region us-west-2`
6. CloudFormation continues, attaches EBS to new instance, completes.
7. SSM into the new EC2, verify all four containers come up
   healthy (including the bot now reporting `healthy`).
8. Run the M.5 backup verification §1 + §2 to confirm Litestream +
   Chroma still work.
9. Run a capture from the extension and a recall from the popup
   to confirm app paths still work end-to-end.

Estimated time: 30 min including the EBS dance.

---

## 4. Open decisions

| Decision | Options | Recommendation |
|----------|---------|----------------|
| **OIDC vs static keys for GitHub Actions** | OIDC (no secrets) or static IAM user keys in GH Secrets | OIDC. One-time setup pain, lifetime safety upside. |
| **Single workflow per repo or split** | One workflow per repo with all steps, OR split build (BrainTwin) + deploy (BrainTwinCDK) | Split — the imageTag handoff between repos works cleanly via ECR's "list latest image by tag" |
| **EMF library: aws-embedded-metrics or raw JSON** | aws-embedded-metrics-python (official, adds dependency) or raw JSON dict (zero-dep, ~20 lines of code) | Raw JSON. EMF is a documented log format; the official library adds a dep for ~20 lines of code we control. |
| **Dashboard: CDK construct or JSON-as-asset** | CDK Dashboard L2 (typed, verbose) or JSON file uploaded as s3.Asset | CDK construct. Diffable, version-controlled, and consistent with the rest of the stack. |
| **Whether to keep the old hardcoded SSM list during M.10** | Remove immediately or keep both for one deploy | Keep both. Discovery layer reads everything; the old hardcoded fetches remain as a safety net until we've seen a refresh cycle work. Remove in 4.0.6.2 or whenever you next touch this. |

---

## 5. Success criteria

The phase is done when:

- `docker compose ps` shows all 4 containers `Up (healthy)` on the
  current EC2
- User-data plaintext is **under 10 KB** (vs 16 KB today)
- Adding a new SSM secret takes **zero user-data changes** and
  **zero instance replacements** — demonstrated by adding a
  test parameter via `put-secret.sh` and watching refresh pick it up
- A tagged commit on either repo's main branch triggers a green
  GitHub Actions run that lands a new image tag in ECR (BrainTwin)
  or a new stack version (BrainTwinCDK)
- CloudWatch has a **BrainTwin/App** namespace populated with per-
  route latency, error rate, and Anthropic API timing
- A new CloudWatch dashboard shows all of the above in one view
- The M.5 backup runbook still works end-to-end (regression check)
- §14 of the main design doc gets a new subsection: §14.7
  "user-data discipline post-discovery-pattern" capturing the new
  invariants

---

## 6. What 4.0.6.2 might still contain (if we discover gaps)

This is a placeholder list, expected to grow as 4.0.6.1 reveals
real production gaps:

- Alerting that goes somewhere I'll actually see (currently AWS
  Budget emails; need at least one alarm → PagerDuty or
  PushOver or just SMS)
- Synthetic monitoring (cron-style health probe from an external
  point)
- A scripted EBS snapshot restore drill (we've documented it; not
  yet drilled monthly)
- **M.12.b — minimal-user-data architecture** (see §6.1 below)

Decide whether to fold these into 4.0.6.1 once the main five
items are in flight.

### 6.1 M.12.b — minimal-user-data architecture

M.12 v1 moved the 4 truly-static files (CW Agent JSON, chroma
backup script + systemd units) to s3.Asset. It saved ~830 bytes
and bought margin from 50 → 880 bytes. The remaining ~15 KB of
user-data is structured roughly as:

| Block | Approx bytes | Externalizable? |
|-------|---|---|
| set/log wrapper + apt-get awscli + IMDS region | ~500 | No — bootstrap minimum |
| Docker install | ~700 | Yes (static, no CDK tokens) |
| EBS detect + mkfs + mount + chown | ~500 | Yes (static) |
| Caddy dirs + AOP CA cert fetch | ~600 | Yes (static; fetch is network-side anyway) |
| Refresh script body (includes compose + litestream.yml templates) | ~5000 | Hard (heavily CDK-token-templated) |
| Caddyfile | ~1500 | Hard (2 CDK config substitutions) |
| litestream.yml | ~300 | Medium (refresh-time $-vars) |
| Other config writes (secrets.env writer skeleton, etc.) | ~6000 | Mixed |

**Target architecture:**

```
user-data (~500-800 bytes):
  set + log + apt-get awscli + region from IMDS
  export <CDK config as env vars>
  aws s3 cp s3://<bootAsset>/braintwin-boot.sh /tmp/
  exec /tmp/braintwin-boot.sh
```

All the static blocks + the refresh script consolidate into
`assets/braintwin-boot.sh`. CDK token-substituted values
(image-tag SSM path, repo URIs, log group names, public
hostname, etc.) get passed in as **exported env vars** by
user-data right before the asset runs. The asset itself stays
template-token-free — it reads `$BRAINTWIN_*` env vars and
substitutes via `envsubst` or shell expansion.

**Why this is a separate phase, not part of M.12 v1:**

- The env-var contract between user-data and boot.sh is a real
  interface that deserves design (which names, which scope,
  which defaults).
- Validation is harder — the synthesized user-data tells you
  less; the asset content tells you more. Tests need to read
  both layers.
- The refresh script touches docker-compose templating which is
  itself coupled to image-tag refresh semantics. Want M.10
  (discovery refresh) to land first so M.12.b inherits a cleaner
  refresh-script shape.

**Estimated time:** 1-2 days including the env-var contract
design + test rework. Saves ~13 KB of user-data — effectively
removing the 16 KB ceiling as a class of bug.

**Recommended sequencing:** M.10 first (discovery refresh script
removes one source of refresh-script churn), then M.12.b. By
then we may also know whether the headroom is genuinely needed
or if M.12 v1's 880 bytes is "good enough forever."

---

## 7. References

- Main deployment design: `phase4.0.6-deployment-design.md`
- §14 invariants (the EBS-deadlock, IMDS hop limit, container
  UID × cap_drop pitfalls): same doc, near the bottom
- M.3 first-cloud-deploy runbook: `phase4.0.6-M3-first-cloud-deploy.md`
- M.5 backup + restore runbook: `phase4.0.6-M5-backup-restore.md`
- Tracker tasks: #64 (M.10), #66 (M.11), #67 (M.12) — to be split
  to add M.0 (bot healthcheck) and M.13 (CI/CD)

---

*Author: Sabya (with Claude as design partner). Created 2026-06-19
the day after Phase 4.0.6 shipped to production. Scope intentionally
bounded at "smooth what 4.0.6 left rough" — Phase 4.0.5 (eval) and
Phase 4.0.7 (Postgres) are next.*

*Revised 2026-06-22: M.0 (bot healthcheck) and M.12 v1 (s3.Asset for
4 static files) shipped. A code-review pass caught three real bugs
on top of the initial M.12 work — the cmdline-grep self-match (made
the M.0 healthcheck cosmetic), the missing `s3cp_retry` helper
(would have failed boot on any transient S3 hiccup), and the
untracked `assets/` directory (would have failed any clean-clone
deploy). All three landed as fix(compute) hardening on top of M.12.
Captured the first two as new §14.7 / §14.8 invariants in the main
deployment design doc. M.12.b (task #72) captured in §6.1 for the
"minimal-user-data" follow-up.*
