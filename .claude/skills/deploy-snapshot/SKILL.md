---
name: deploy-snapshot
description: Deploy the current working-tree changes to AWS as a SNAPSHOT image for testing on the live box — no release tag, no version bump, no PR required. Builds with build-and-push.sh in snapshot mode (snapshot-<git-sha>, dirty trees allowed), pushes to ECR, does the in-place refresh, smoke-tests. Use when the user says "deploy my changes to test", "push a snapshot to the cloud", "deploy WIP", "try this on the box". For a tagged production release, use /release instead.
---

# BrainTwin deploy-snapshot skill

The fast iteration loop: get the working-tree changes onto the live AWS box to
test them, **without** the ceremony of a release. This is the sibling of the
`/release` skill — it shares the deploy mechanics (auth, the EBS-deadlock gate,
the SSM-direct fallback, the two-repo layout) but skips versioning, git tags,
and PRs.

## deploy-snapshot vs. /release — pick the right one

| | `/deploy-snapshot` (this) | `/release` |
|---|---|---|
| Purpose | Test active changes on the live box | Cut a production release |
| Image tag | `snapshot-<git-sha>` (immutable) | `vX.Y.Z` |
| Dirty tree | **Allowed** → `snapshot-<sha>-dirty-<timestamp>` | Refused (must commit) |
| Version decision | None | Recommend-then-confirm semver |
| Test gate | Fast subset locally (advisory) | **Full** suite, blocking |
| Git tag / PR | No | Tag `vX.Y.Z` + PR to `main` |
| Cadence | Many times a day | Rare |

When the changes are proven good on the box, promote them with `/release`
(commit clean → tagged image → PR). A snapshot is never the thing you "ship";
it's the thing you "try".

## Shared mechanics (same as /release — see that skill for detail)

- **Auth first.** `aws sso login --profile braintwin` (SSO token ~8h). The aws
  CLI may work while `cdk` can't read the SSO cache — if so, use the SSM-direct
  refresh below.
- **EBS-deadlock gate (`§14`).** Any change to `BrainTwinCDK/lib/constructs/
  compute.ts` user-data forces an instance replacement that deadlocks on the
  single RETAIN EBS volume. Snapshots are almost always app-code only (no
  user-data change) → clean in-place refresh. **Always `cdk diff` first; if it
  shows a LaunchTemplate/user-data replacement, STOP** — that's a `/release`-
  grade change needing the manual unblock, not a casual snapshot.
- Defaults: `AWS_PROFILE=braintwin`, region `us-west-2`, `linux/arm64`, stack
  `BrainTwinStack-us-west-2`, ECR `braintwin/app` + `braintwin/caddy`.

## Checklist

### 1. Sanity check (light — this is iteration, not a release)
- `git status` so you know what's going out. Committing is **optional**: a
  clean tree gives a reproducible `snapshot-<sha>` tag; a dirty tree builds
  `snapshot-<sha>-dirty-<timestamp>` (fine for a throwaway test, not
  reproducible from git).
- Run the **fast** test subset before shipping (cheap insurance):
  ```bash
  BRAINTWIN_FAST_TESTS=1 env/bin/pytest -q --disable-warnings
  ```
  Advisory, not blocking — the full suite is `/release`'s job. If it's red,
  tell the operator and ask whether to deploy anyway.
- Note any `compute.ts` user-data change → see the EBS gate; likely STOP.

### 2. Build + push the snapshot image (from BrainTwin)
```bash
./scripts/build-and-push.sh          # snapshot-<git-sha> (or -dirty-<ts>)
```
- Heavy arm64 cross-build (torch + whisper.cpp) — **run in the background**;
  preflight (AWS auth, ECR repo, tag collision) fails fast in the first lines.
- Writes the tag to `../BrainTwinCDK/.last-deploy-tag`.
- **Skip the Caddy build** unless `caddy/Dockerfile` changed — reuse the
  existing `../BrainTwinCDK/.last-deploy-caddy-tag`. (Only run
  `./scripts/build-and-push-caddy.sh` if the edge image actually changed.)
- A clean tree with no new commits since the last snapshot → the script
  refuses (tag collision). Make a commit to advance the sha, or it's already
  deployed.

### 3. Deploy the snapshot (gated, same as /release)
```bash
cd ../BrainTwinCDK && npx cdk diff --profile braintwin --context region=us-west-2
```
- **Clean diff** (only SSM param values) → `./scripts/deploy.sh --require-approval never`.
- **Diff shows user-data/instance replacement** → STOP (EBS deadlock; not a
  snapshot-grade change).
- **`cdk` can't authenticate (SSO)** → SSM-direct refresh (cannot touch
  user-data; safest app-only path). Use the snapshot tag from `.last-deploy-tag`:
  ```bash
  TAG=$(cat ../BrainTwinCDK/.last-deploy-tag)
  aws ssm put-parameter --profile braintwin --region us-west-2 --overwrite \
    --name /braintwin/image_tag --type String --value "$TAG"
  INSTANCE=$(aws cloudformation describe-stack-resources \
    --stack-name BrainTwinStack-us-west-2 --profile braintwin --region us-west-2 \
    --query "StackResources[?ResourceType=='AWS::EC2::Instance'].PhysicalResourceId" --output text)
  aws ssm send-command --document-name AWS-RunShellScript --instance-ids "$INSTANCE" \
    --comment "BrainTwin snapshot refresh ($TAG)" \
    --parameters 'commands=["/usr/local/bin/braintwin-refresh.sh"]' \
    --profile braintwin --region us-west-2
  ```
  Poll `aws ssm get-command-invocation` for `Success`.

### 4. Smoke test
- `curl -fsS https://api.braintwin.net/` → 200.
- Exercise whatever the change touched (e.g. POST `/capture` or `/recall` with
  the bearer token) so the test is about the actual change, not just liveness.

### 5. Report + how to undo / promote
- State the deployed snapshot tag and the smoke-test result.
- **Rollback** (the previous image is still in ECR, immutable): set
  `/braintwin/image_tag` back to the prior tag and re-run the refresh. Capture
  the previous tag *before* step 3 if a quick rollback matters:
  ```bash
  PREV=$(aws ssm get-parameter --name /braintwin/image_tag --profile braintwin \
    --region us-west-2 --query Parameter.Value --output text)
  ```
- **Promote**: once the snapshot proves good, run `/release` to cut a clean
  `vX.Y.Z` from a committed tree (tagged image + PR to `main`).

## Note: snapshots accumulate in ECR
Every snapshot is an immutable tag and stays in `braintwin/app` until pruned.
Over many iterations these pile up. Not urgent for a single operator, but worth
an ECR lifecycle policy eventually (e.g. expire untagged / keep last N
snapshots). Flag it if the repo looks crowded; don't delete tags ad hoc here.
