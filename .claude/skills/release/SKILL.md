---
name: release
description: Cut a BrainTwin production release — review the pending diff across the BrainTwin app repo and the sibling BrainTwinCDK infra repo, commit on a release branch, build + push immutable release images to ECR (app + Caddy), deploy to AWS (in-place refresh, gated on a clean cdk diff), then push branches/tags to GitHub. Use when the user says "cut a release", "ship to AWS", "release vX.Y.Z", or "do the release flow".
---

# BrainTwin release skill

Drives the end-to-end release of BrainTwin to AWS. Two repos move together:

- **BrainTwin** (this repo) — app code, Dockerfiles, build scripts.
  `../BrainTwin` from here.
- **BrainTwinCDK** — the CDK stack + `deploy.sh`. Sibling dir: `../BrainTwinCDK`.

The build scripts live in BrainTwin (they build BrainTwin's code) and write
the chosen image tags **sideways** into `BrainTwinCDK/.last-deploy-tag` and
`.last-deploy-caddy-tag`. `BrainTwinCDK/scripts/deploy.sh` reads those files.

## The one invariant that governs everything

> **Any change to EC2 user-data forces an instance replacement, and the single
> RETAIN EBS data volume can't move between instances in one CFN transaction →
> the deploy deadlocks.** (design doc `§14.1`).

Routine releases (image-tag bumps, secret-value rotation) **never** touch
user-data → clean in-place refresh, no instance churn. But anything that edits
`BrainTwinCDK/lib/constructs/compute.ts` user-data (new container, new SSM
fetch *line*, EBS/cloud-init changes) **requires the manual EBS-unblock dance**
(terminate the old EC2 so CFN can attach the volume to the new one).

**Always run `cdk diff` before deploying. If it shows a LaunchTemplate /
user-data / instance replacement, STOP and tell the user it needs the manual
unblock — do not let `deploy.sh` churn the instance unattended.**

## Authentication — do this FIRST (needs the operator)

Both AWS and GitHub require interactive auth that Claude cannot perform on the
operator's behalf. Confirm these before the irreversible steps:

- **AWS (SSO):** the `braintwin` profile is AWS IAM Identity Center (SSO). The
  token expires (~8h). The aws CLI uses it fine, but `cdk` may fail with
  "no credentials have been configured" / "Unable to resolve AWS account" even
  when the CLI works. If either happens, have the operator run (in their own
  shell, e.g. `! aws sso login --profile braintwin`):
  ```bash
  aws sso login --profile braintwin
  ```
  Verify with `aws sts get-caller-identity --profile braintwin`.
- **`cdk` can't read SSO?** If `cdk diff/deploy` still can't authenticate after
  `aws sso login` (old CLI ↔ SDK SSO-cache mismatch), fall back to the
  **SSM-direct image refresh** for app-only releases (see step 4b): update the
  `/braintwin/image_tag` + `/braintwin/caddy_image_tag` params with the aws CLI
  and trigger `braintwin-refresh.sh` over SSM RunCommand. This is byte-for-byte
  what `deploy.sh` does after `cdk deploy`, and it CANNOT touch user-data, so
  it's the safest app-only path.
- **GitHub:** `git push` / `gh pr create` need the operator's credentials and
  approval. Don't assume push will succeed unattended; if it prompts or fails
  on auth, ask the operator to authenticate (`gh auth login` or their git
  credential helper) and retry.

## Versioning

- App + Caddy images share the `vX.Y.Z` release scheme (`--release vX.Y.Z`).
  - App tag: `v0.1.0`. Caddy tag: `caddy-<upstream>-v0.1.0` (script derives it).
- `v0.x` = pre-API-stability. MAJOR=breaking API, MINOR=feature, PATCH=fix.
- Release builds **refuse a dirty tree** (reproducible-from-git) → commit first.
- ECR is `IMMUTABLE` → a tag can't be reused. Bump the version to re-release.
- The extension `manifest.json` version is independent of the image version.

## Checklist

Confirm the **version** and **deploy scope** with the user before any
irreversible step (ECR push / AWS deploy / GitHub push). Then:

### 1. Review (read-only)
- `git -C ../BrainTwin status` and `git -C ../BrainTwinCDK status`.
- Read the full pending diff in both repos. Call out: anything touching
  `compute.ts` user-data (triggers `§14` deadlock), secret handling, CORS/auth,
  and the extension `BACKEND_URL`/`config.js`.
- Report findings. Do **not** fix unless asked — releases ship what's reviewed.

### 2. Branch + commit (both repos)
- `git checkout -b release/vX.Y.Z` in each repo (or the user's branch name).
- Commit with a message that lists the milestones included and flags any
  known `§14` user-data consequence.

### 3. Build + push images (from BrainTwin, clean tree)
```bash
./scripts/build-and-push.sh --release vX.Y.Z        # app  → braintwin/app
./scripts/build-and-push-caddy.sh --release vX.Y.Z  # caddy → braintwin/caddy
```
- These cross-build `linux/arm64` (Graviton). Heavy (torch + whisper.cpp) —
  **run in the background** and monitor; preflight (AWS auth, ECR repo exists,
  tag collision) fails fast in the first few lines.
- Each writes its tag into `../BrainTwinCDK/.last-deploy-{,caddy-}tag`.
- Skip the Caddy build if `caddy/Dockerfile` is unchanged (it bumps every few
  months, not every release) — just reuse the existing `.last-deploy-caddy-tag`.

### 4. Deploy (gated)
```bash
cd ../BrainTwinCDK && npx cdk diff --profile braintwin --context region=us-west-2
```
- **Clean diff (only SSM param values / no instance replacement)** → safe:
  ```bash
  ./scripts/deploy.sh --require-approval never
  ```
  This updates the `/braintwin/image_tag` + `/braintwin/caddy_image_tag` SSM
  params and triggers the in-place refresh over SSM RunCommand (container swap,
  no churn).
- **Diff shows user-data / LaunchTemplate replacement** → STOP. Report that
  this deploy needs the manual EBS unblock and confirm scope with the user
  before proceeding.

#### 4b. SSM-direct refresh (fallback when cdk can't auth, app-only)
Only the two image-tag params change; never touches user-data, so it cannot
trigger an instance replacement. Use the working aws CLI + `braintwin` profile:
```bash
aws ssm put-parameter --profile braintwin --region us-west-2 --overwrite \
  --name /braintwin/image_tag --type String --value vX.Y.Z
aws ssm put-parameter --profile braintwin --region us-west-2 --overwrite \
  --name /braintwin/caddy_image_tag --type String --value caddy-<ver>-vX.Y.Z
# find the instance, then refresh:
INSTANCE=$(aws cloudformation describe-stack-resources \
  --stack-name BrainTwinStack-us-west-2 --profile braintwin --region us-west-2 \
  --query "StackResources[?ResourceType=='AWS::EC2::Instance'].PhysicalResourceId" --output text)
aws ssm send-command --document-name AWS-RunShellScript --instance-ids "$INSTANCE" \
  --comment "BrainTwin app image refresh (vX.Y.Z)" \
  --parameters 'commands=["/usr/local/bin/braintwin-refresh.sh"]' \
  --profile braintwin --region us-west-2
```
Then poll `aws ssm get-command-invocation` for `Success`.

### 5. Push to GitHub (both repos) — needs operator auth
- Push the release branch in each repo. If push prompts/fails on auth, ask the
  operator to authenticate and retry (see Authentication above).
- Tag the release commit `git tag -a vX.Y.Z -m "…"` and push tags (keeps the
  git tag aligned with the image tag).
- **Open a PR to merge each release branch into `main`** (`gh pr create --base main`).
  This is the default — the operator reviews/merges. Don't push straight to main.

### 6. Post-deploy smoke test
- `curl -fsS https://api.braintwin.net/` should return 200 (public health route).
- Confirm `/openapi.json` is **404** in prod (docs hardening, M.4.1).
- Optionally tail the in-place refresh result from `deploy.sh` output.

### 7. Report
- Image tags pushed, deploy outcome, PR/tag URLs, and any `§14` follow-up.

## Defaults (override via env on the scripts)
- `AWS_PROFILE=braintwin`, `AWS_REGION=us-west-2`, `PLATFORM=linux/arm64`
- App ECR repo `braintwin/app`; Caddy `braintwin/caddy`.
- Stack `BrainTwinStack-us-west-2`.
