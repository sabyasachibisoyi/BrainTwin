# Phase 4.0.6 M.3 — First cloud deploy

**Purpose:** stand up BrainTwin on AWS for the first time, end-to-end:
ECR + S3 + EC2 + EBS + CloudWatch logs + Telegram bot + FastAPI app
serving on the EC2's loopback. Verify everything via SSM Session
Manager (no SSH, no public ingress yet).

Total time: ~45 min for a clean first run, most of which is the cold
image build (Phase C, ~30 min — torch + the whisper.cpp compile) plus
waiting for CloudFormation to create resources and for the EC2
user-data to finish.

> **You are here:** raw cloud deploy on the Elastic IP. No Cloudflare
> DNS, no `braintwin.net` cert yet — those land in M.4. M.3 success
> means the app answers `/health` and the bot DMs you your Telegram
> user ID when smoke-tested from inside the EC2 via SSM.

---

## 0. Prereqs

- AWS account active (account ID `494567491756`), AWS CLI v2 installed
- AWS profile `braintwin` configured pointing at that account, with
  admin or PowerUser permissions
  ```bash
  aws sts get-caller-identity --profile braintwin
  # → should print the account ID and your IAM principal
  ```
  **If you use AWS SSO / IAM Identity Center** (recommended over
  long-lived static access keys), the session expires periodically
  (default 8–12 hours per Identity Center config). Whenever an AWS
  call fails with `NoCredentials: ... no credentials have been
  configured` or "The SSO session associated with this profile has
  expired", re-authenticate:
  ```bash
  aws sso login --profile braintwin
  ```
  This will open a browser, ask for an 8-char verification code,
  and refresh the session. Then retry the failed command. It's
  worth running this once at the start of any deploy session even
  if you're not sure of the state — costs nothing if the session is
  still valid.

  **If you use static IAM access keys instead**, first-time setup is
  `aws configure --profile braintwin` (it'll prompt for the access
  key ID, secret, region `us-west-2`, output `json`). Get the key
  from IAM Console → Users → your user → Security credentials →
  Create access key. The secret is shown ONCE — copy it immediately.
- Docker Desktop running (for the buildx cross-build to `linux/arm64`)
- **AWS Session Manager Plugin** installed (separate from the AWS CLI;
  `aws ssm start-session` needs it for the WebSocket terminal). On Mac:
  ```bash
  brew install --cask session-manager-plugin
  session-manager-plugin   # verify; should print "installed successfully"
  ```
  Without this, Phase E smoke testing fails with "SessionManagerPlugin
  is not found" the first time you try to open a session.
- Both repos checked out at `~/Desktop/LLM/`:
  - `~/Desktop/LLM/BrainTwin/`  (app code + this runbook)
  - `~/Desktop/LLM/BrainTwinCDK/`  (CDK + scripts)
- A 30+ char random string for `BACKEND_BEARER_TOKEN` (the same one
  the Chrome extension will eventually use)
- Your `ANTHROPIC_API_KEY` ready (Console → API Keys)
- Telegram bot created via @BotFather, bot token in hand
- `BRAINTWIN_ALERT_EMAIL` env var set in the shell that will run
  `cdk deploy` — your real email, NOT committed anywhere:
  ```bash
  export BRAINTWIN_ALERT_EMAIL='you@example.com'
  ```
  Without this, AWS Budget alerts go to a `@example.invalid` placeholder
  (the CDK warns at synth time).

---

## 1. CDK bootstrap (one-time per account+region)

```bash
cd ~/Desktop/LLM/BrainTwinCDK
npx cdk bootstrap aws://494567491756/us-west-2 --profile braintwin
```

This creates the CDK toolkit stack (S3 bucket for asset uploads, IAM
roles for CloudFormation). Idempotent — re-running is a no-op. Skip if
already done.

---

## 2. Phase A — Infrastructure deploy with placeholder image tag

The first deploy is a **two-phase** thing because ECR doesn't exist
yet, so we can't push an image until CDK creates the repo. The compose
template needs a tag, so we use `bootstrap` — the EC2 boots, fails its
`docker pull`, and waits for us to redeploy with a real tag.

```bash
cd ~/Desktop/LLM/BrainTwinCDK
npx cdk deploy --context imageTag=bootstrap --profile braintwin --require-approval never
```

This takes ~10 min. Watch the CFN events scroll by. At synth time
you'll see a warning:

```
[Warning at /BrainTwinStack-us-west-2] imageTag is the placeholder
'bootstrap' — the EC2 user-data will fail to pull a real image.
```

That's expected — we'll fix it in Phase D.

**What gets created in Phase A:**

- VPC `10.10.0.0/16` + public subnet + route table + Internet Gateway
- Security Group with 15 Cloudflare CIDR ingress rules on :443 (no :22)
- Elastic IP (the static public IPv4 you'll later point DNS at)
- EC2 t4g.small + 20 GiB encrypted gp3 EBS
- ECR repo `braintwin/app` (empty)
- S3 bucket `braintwin-state-494567491756-us-west-2`
- 4 SSM parameter NAMES declared (values not populated yet)
- CloudWatch log groups `/braintwin/app` and `/braintwin/bot`
- AWS Budget with 4 thresholds, alerts to `$BRAINTWIN_ALERT_EMAIL`
- DLM policy for daily EBS snapshots

Once `cdk deploy` finishes, note the outputs it prints:

```
BrainTwinStack-us-west-2.NetworkElasticIpAddress = 54.x.y.z
BrainTwinStack-us-west-2.ComputePrimaryInstanceId = i-0abc123…
BrainTwinStack-us-west-2.ConfiguredImageTag = bootstrap
```

The instance ID is what you'll use for SSM Session Manager. The EIP
is what M.4 will point DNS at.

---

## 3. Phase B — Populate SSM secrets

The CDK declares the four parameter NAMES but cannot create
SecureStrings (CloudFormation limitation — see `secrets.ts` docstring).
Run the helper from BrainTwinCDK:

```bash
cd ~/Desktop/LLM/BrainTwinCDK
./scripts/put-secrets.sh
```

It'll prompt for each value (silent input — no echo, no shell history,
no `ps`-visible argv leak). Paste:

1. `/braintwin/anthropic_key` — your `sk-ant-…` key
2. `/braintwin/bearer_token` — the 30+ char string
3. `/braintwin/telegram_token` — the Telegram bot token from @BotFather
4. `/braintwin/cloudflare_api_token` — paste anything for now (only
   used in M.4 for Caddy's ACME DNS-01 challenge); you can populate
   the real value when registering the zone API token

Verify:

```bash
aws ssm get-parameters \
  --names /braintwin/anthropic_key /braintwin/bearer_token \
          /braintwin/telegram_token /braintwin/cloudflare_api_token \
  --with-decryption \
  --query 'Parameters[*].Name' \
  --profile braintwin \
  --region us-west-2
```

Should print all four names. If any are missing, re-run `put-secrets.sh`.

---

## 4. Phase C — Build and push the image

```bash
cd ~/Desktop/LLM/BrainTwin
./scripts/build-and-push.sh
```

Expected sequence:

1. AWS profile resolves, account printed
2. ECR repo existence verified (would have errored in Phase A days)
3. Tag picked: `snapshot-<git-sha>` (clean tree) or `snapshot-<sha>-dirty-<ts>` (uncommitted)
4. `docker login` to ECR
5. `docker buildx build --platform linux/arm64 --push` — this takes
   ~30 min on a cold cache: the pip layer (torch + chromadb) and the
   whisper.cpp compile run concurrently under QEMU emulation (~28 min
   and ~18 min respectively), then ~5 min to export + push. Rebuilds
   that only touch `backend/` are ~30 sec (cached layers). Don't assume
   it's hung at the 10-minute mark — the whisper-builder stage is slow
   under emulation.
   - Note: the Dockerfile builds whisper.cpp with `-DGGML_NATIVE=OFF`
     so ggml doesn't probe the (emulated) build host's CPU — that probe
     emits an `-mcpu=native+…` flag the container GCC rejects, which
     otherwise fails the cross-build. The result is a portable armv8-a
     binary, correct for the Graviton run host.
6. The tag is recorded to `../BrainTwinCDK/.last-deploy-tag`

Final output:

```
==> Pushed: 494567491756.dkr.ecr.us-west-2.amazonaws.com/braintwin/app:snapshot-abc1234
==> Tag recorded at: /Users/sabyasachibisoyi/Desktop/LLM/BrainTwinCDK/.last-deploy-tag
```

---

## 5. Phase D — Re-deploy with the real tag

```bash
cd ~/Desktop/LLM/BrainTwinCDK
./scripts/deploy.sh
```

This reads `.last-deploy-tag` and runs:

```bash
npx cdk deploy --context imageTag=snapshot-abc1234 --profile braintwin
```

Before deploying, the script verifies the tag exists in ECR (Codex
review fix — fail-fast vs discover at EC2 boot).

**Important:** changing `imageTag` modifies the EC2's LaunchTemplate
user-data, which forces a **CloudFormation replacement** of the
instance. CFN terminates the bootstrap-tag EC2 and creates a new one
with the real-tag user-data. The new instance boots, finds the
existing EBS volume (matched by 20 GiB size + ext4 label), mounts it
in place — no data loss, the volume is RETAINED.

This takes ~5 min for CFN + ~3 min for the new EC2's user-data to
finish (Docker install + ECR pull + compose up). Once `cdk deploy`
returns:

```
BrainTwinStack-us-west-2.ConfiguredImageTag = snapshot-abc1234
```

The instance ID will be DIFFERENT from Phase A — that's expected.

---

## 6. Phase E — Smoke test via SSM Session Manager

Get the new instance ID:

```bash
aws ec2 describe-instances \
  --filters 'Name=tag:Name,Values=BrainTwin-EC2-0' 'Name=instance-state-name,Values=running' \
  --query 'Reservations[0].Instances[0].InstanceId' \
  --output text \
  --profile braintwin \
  --region us-west-2
```

Open an SSM session:

```bash
aws ssm start-session --target i-0abc… --profile braintwin --region us-west-2
```

You'll get a shell on the EC2 as `ssm-user`. From there:

### 6.1 Check user-data finished cleanly

```bash
sudo tail -30 /var/log/braintwin-userdata.log
```

Last line should be `== braintwin user-data complete ==`. If the script
aborted, the lines above show what failed. Most common causes:
- ECR login failure → IAM permission issue (rerun `cdk deploy`)
- SSM get-parameter failure → secret not populated yet (see Phase B)
- docker pull failure → image not pushed (see Phase C)

### 6.2 Confirm both containers are running

```bash
sudo docker compose -f /etc/braintwin/docker-compose.yml ps
```

Expected output (after ~60s — there's a `start_period` on the app
healthcheck):

```
NAME              IMAGE                       STATUS                   PORTS
braintwin-app     494567491756.dkr…           Up 1 minute (healthy)    127.0.0.1:8000->8000/tcp
braintwin-bot     494567491756.dkr…           Up 1 minute
```

If `braintwin-app` is `unhealthy`, check `sudo docker logs braintwin-app`.

### 6.3 Hit `/health` and `/`

```bash
curl -fsS http://127.0.0.1:8000/health
# → {"status":"ok"}

curl -fsS http://127.0.0.1:8000/
# → {"name":"BrainTwin","status":"running","version":"0.1.0"}
```

### 6.4 Confirm the bearer-token gate works

Without the header → 401:

```bash
curl -i -X POST http://127.0.0.1:8000/recall \
  -H 'Content-Type: application/json' \
  -d '{"query":"anything"}'
# → HTTP/1.1 401 Unauthorized
```

With the real token (read from the secrets.env that user-data just
wrote — only root can read it):

```bash
sudo cat /etc/braintwin/secrets.env | grep BACKEND_BEARER_TOKEN
# Then:
TOKEN='<paste the token>'
curl -i -X POST http://127.0.0.1:8000/recall \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"query":"anything"}'
# → 200 (or 503 "no captures yet" — both prove auth passed)
```

### 6.5 Telegram bot — send `/start`

On your phone, message your bot `/start`. Expected reply:

> 👋 Hi <name>.
> Your Telegram user ID is 123456789.
> To activate BrainTwin, paste this ID into ALLOWED_TELEGRAM_USER_IDS
> in your .env, then restart the bot.

The bot is rejecting you because `ALLOWED_TELEGRAM_USER_IDS` isn't set
— that's expected for the first deploy. To allowlist yourself:

```bash
# Inside the SSM session:
sudo bash -c 'echo "ALLOWED_TELEGRAM_USER_IDS=123456789" >> /etc/braintwin/secrets.env'
sudo docker compose -f /etc/braintwin/docker-compose.yml restart bot
```

(A cleaner approach — add `/braintwin/allowed_telegram_user_ids` as
a 5th SSM parameter — is deferred. For M.3 the manual-append is fine.)

Now `/start` should greet you as activated, and forwarding an article
to the bot should land a capture.

### 6.6 Exit the session

```bash
exit
```

---

## 7. Read the CloudWatch logs from your Mac

From your laptop (no SSM session needed):

```bash
# App stdout
aws logs tail /braintwin/app --follow --profile braintwin --region us-west-2

# Bot stdout
aws logs tail /braintwin/bot --follow --profile braintwin --region us-west-2
```

Both should be streaming live as you exercise the API.

If `aws logs tail` returns "No log streams" right after deploy, wait
60s and retry — the Docker awslogs driver creates streams lazily on
first log line.

---

## 8. Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `cdk bootstrap` / `cdk deploy` / `aws *` errors `NoCredentials: ... no credentials have been configured` | SSO session expired (or never logged in) | `aws sso login --profile braintwin`, then retry. Each AWS-call failure with this error means the session just timed out — same fix, no need to investigate further. |
| `aws sts get-caller-identity` returns "The SSO session associated with this profile has expired" | Same as above, explicit form | `aws sso login --profile braintwin` |
| `cdk deploy` errors "tag immutable" | Pushed the same tag twice | Make a commit (advances sha) → rerun `build-and-push.sh` |
| `cdk deploy` errors "Resource of type AWS::Logs::LogGroup with identifier /braintwin/app already exists" | Orphaned RETAIN resources from a prior failed deploy | See §9.1 — delete the orphan log groups (and the S3 bucket if it exists) before retrying |
| `aws ssm start-session` errors "SessionManagerPlugin is not found" | The plugin is a separate install from the AWS CLI | `brew install --cask session-manager-plugin` (Mac) — see §0 prereqs |
| `cdk deploy` errors "image not found" | Forgot Phase C | Run `build-and-push.sh` first |
| User-data log ends mid-script (no "complete" line) | A command in the script returned nonzero under `set -e` | `sudo tail -100` the log to find the failed step |
| `docker compose ps` shows `Restarting` loop on app | App can't reach SSM secrets (IAM regression?) | Check `sudo docker logs braintwin-app` for the first failure |
| `aws logs tail` returns AccessDenied | Profile not the admin one | `--profile braintwin` |
| Bot replies "User ID 123… not authorized" | `ALLOWED_TELEGRAM_USER_IDS` not set | See Phase E.5 |
| Public IP from your Mac → connection refused | Caddy isn't running yet (M.4) | Smoke-test via SSM only until M.4 |

---

## 9. Rollback

If M.3 demonstrates a deal-breaker problem, you can take everything
down without losing data:

```bash
cd ~/Desktop/LLM/BrainTwinCDK
npx cdk destroy --profile braintwin --force
```

This removes:
- EC2 instance (and its root disk)
- Elastic IP (frees it back to the AWS pool — the new one on next
  deploy will have a different IP)
- VPC + subnets + SG
- ECR repo + images (the `emptyOnDelete: true` setting empties it)
- CloudWatch log groups → no, these have `RETAIN` — they survive
- AWS Budget
- DLM policy

What **survives**:
- **EBS data volume** (`RETAIN` policy) — your captures, Chroma index,
  images. Next `cdk deploy` will pick it up by tag/size, no data loss.
- **S3 state bucket** (`RETAIN` policy) — backups don't get nuked.
- **SSM SecureString parameters** — not created by CDK so not destroyed
  by it; populated values stay.
- **CloudWatch log history** — 30-day retention applies, but the
  groups themselves survive.

So `cdk destroy && cdk deploy` is safe to use during M.3 debugging.

### 9.1 Re-deploy gotcha: RETAIN resources collide on second create

The first time you re-deploy after a `cdk destroy` (or after a failed
first deploy that left the stack in `ROLLBACK_COMPLETE` and you ran
`delete-stack`), CFN will fail change-set validation with:

```
Resource of type 'AWS::Logs::LogGroup' with identifier
'/braintwin/app' already exists.
```

That's the RETAIN policy biting. The log groups (and the S3 state
bucket, and any data-volume EBS that got created) survive the
destroy as orphans, and CFN refuses to create new resources with
the same names.

**Fix for first-deploy debug cycles** (when nothing real is in the
orphans yet) — delete them manually before retrying:

```bash
# Log groups
aws logs delete-log-group --log-group-name /braintwin/app \
  --profile braintwin --region us-west-2
aws logs delete-log-group --log-group-name /braintwin/bot \
  --profile braintwin --region us-west-2

# S3 state bucket — only if the prior deploy got past Network construct
# (it usually does, since storage.ts runs before compute.ts in the
# instantiation order). --force empties the bucket first.
aws s3 rb s3://braintwin-state-494567491756-us-west-2 --force \
  --profile braintwin --region us-west-2 || true

# Orphan EBS data volumes — find by Project tag, delete by ID
aws ec2 describe-volumes \
  --filters 'Name=tag:Project,Values=BrainTwin' 'Name=status,Values=available' \
  --query 'Volumes[*].[VolumeId]' --output text \
  --profile braintwin --region us-west-2 \
  | xargs -r -n1 aws ec2 delete-volume --profile braintwin --region us-west-2 --volume-id
```

**For production debug cycles** (real captures in the EBS, real
logs you don't want to lose) — do NOT delete the orphans. Use
`cdk import` to attach the existing resources to the new stack
instead. That's a one-off ceremony each time but preserves the
data continuity that's the whole point of RETAIN.

---

## 10. M.3 acceptance checklist

- [ ] `cdk deploy` completes without CFN errors
- [ ] `put-secrets.sh` populates all 4 parameters
- [ ] `build-and-push.sh` pushes a `snapshot-<sha>` tag to ECR
- [ ] `deploy.sh` runs `cdk deploy` with the real tag and CFN updates
- [ ] `aws ssm start-session` opens a shell on the EC2
- [ ] `/var/log/braintwin-userdata.log` ends with "complete"
- [ ] `docker compose ps` shows both `app` (healthy) and `bot` running
- [ ] `curl http://127.0.0.1:8000/health` returns 200
- [ ] `/recall` without bearer returns 401; with bearer returns 200/503
- [ ] Telegram bot replies on `/start`
- [ ] `aws logs tail /braintwin/app` streams live log lines
- [ ] `BRAINTWIN_ALERT_EMAIL` got the AWS Budget welcome email

---

## 11. What's deferred to later milestones

- **M.4 — Caddy + Cloudflare + cert**: Caddy reverse proxy in front of
  the app on :443, Cloudflare DNS pointing at the EIP, ACME DNS-01
  cert via Cloudflare API token, Authenticated Origin Pulls.
- **M.5 — Litestream**: SQLite WAL streaming to S3 for RPO ≤ 60s.
- **M.6 — CloudWatch Agent**: system-level metrics (CPU, memory, disk)
  shipped to CloudWatch as well as the per-service stdout logs.
- **`/braintwin/allowed_telegram_user_ids` as a 5th SSM parameter**:
  cleaner than `secrets.env` append; ship in a follow-up.
- **Bot restart on secret rotation**: today, rotating a secret in SSM
  requires re-deploying the stack (EC2 replacement) for the new value
  to flow into the env. A boot-time hook + periodic re-fetch is a
  Phase 4.0.6.1 concern.
