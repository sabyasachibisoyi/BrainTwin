#!/usr/bin/env bash
#
# Phase 4.0.6 M.3 — Build the BrainTwin app image for ARM64 (Graviton
# EC2), tag it, push to ECR, and record the tag for BrainTwinCDK's
# deploy.sh to pick up.
#
# Why this script lives in BrainTwin (not BrainTwinCDK)
# -----------------------------------------------------
# It builds *this* repo's code — Dockerfile is here, sources are here,
# the git sha that pins the snapshot tag is this repo's sha. Co-locating
# the build script with what it builds keeps the loop tight: edit code,
# run the script in the same repo, see the result.
#
# The CDK repo (../BrainTwinCDK) owns *deployment* — `cdk deploy`,
# infrastructure changes, and the .last-deploy-tag handoff file. This
# script reaches sideways once (writes the chosen tag into the CDK
# repo's .last-deploy-tag) so that ../BrainTwinCDK/scripts/deploy.sh
# can read its own local file rather than reaching into a sibling repo.
#
# Why this script exists
# ----------------------
# ECR is configured with imageTagMutability: IMMUTABLE
# (BrainTwinCDK/lib/constructs/storage.ts). That means a literal
# ":SNAPSHOT" tag would only work once — the second push collides with
# ImageAlreadyExistsException. We need every tag to be unique. Two
# strategies, picked per-mode:
#
#   - Pre-launch / iterating:  snapshot-<git-sha>   (default mode)
#   - Production releases:     v<MAJOR>.<MINOR>.<PATCH>  (--release flag)
#
# Both modes refuse to clobber an existing ECR tag. Release mode also
# refuses to build from a dirty working tree, since the whole point of
# a tagged release is reproducibility from git.
#
# Semver, for posterity:
#   MAJOR — bumped on breaking API changes; v0.x.x = pre-API-stability.
#   MINOR — bumped on backwards-compatible features.
#   PATCH — bumped on bug fixes / refactors / dep bumps.
#
# Output flow
# -----------
# Writes the chosen tag to ../BrainTwinCDK/.last-deploy-tag (gitignored
# in that repo). Then BrainTwinCDK/scripts/deploy.sh reads it and
# passes the tag to `cdk deploy --context imageTag=<tag>`, which
# threads it through to the EC2 user-data so `docker pull <repo>:<tag>`
# works on instance boot.
#
# Usage
# -----
#   ./scripts/build-and-push.sh                  # snapshot-<git-sha>
#   ./scripts/build-and-push.sh --release v0.1.0 # tagged release
#
# Environment overrides (rarely needed):
#   AWS_PROFILE   default: braintwin
#   AWS_REGION    default: us-west-2
#   ECR_REPO      default: braintwin/app
#   PLATFORM      default: linux/arm64
#   CDK_DIR       default: <this script>/../../BrainTwinCDK

set -euo pipefail

# ----- Config (overridable via env) ------------------------------------
AWS_PROFILE="${AWS_PROFILE:-braintwin}"
AWS_REGION="${AWS_REGION:-us-west-2}"
ECR_REPO="${ECR_REPO:-braintwin/app}"
PLATFORM="${PLATFORM:-linux/arm64}"

# This script lives in BrainTwin/scripts/. The BrainTwin source repo is
# its parent. The CDK repo is a sibling — override CDK_DIR if your
# layout differs.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BRAINTWIN_SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CDK_DIR="${CDK_DIR:-$(cd "$BRAINTWIN_SRC_DIR/../BrainTwinCDK" 2>/dev/null && pwd)}"
TAG_FILE="$CDK_DIR/.last-deploy-tag"

# ----- Parse args ------------------------------------------------------
RELEASE_VERSION=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --release)
      RELEASE_VERSION="${2:-}"
      [[ -n "$RELEASE_VERSION" ]] || {
        echo "ERROR: --release requires a version argument (e.g. v0.1.0)" >&2
        exit 2
      }
      shift 2
      ;;
    -h|--help)
      sed -n '2,50p' "$0"
      exit 0
      ;;
    *)
      echo "ERROR: unknown arg: $1" >&2
      echo "Run with --help for usage." >&2
      exit 2
      ;;
  esac
done

# ----- Pre-flight ------------------------------------------------------
command -v docker >/dev/null || { echo "ERROR: docker not on PATH" >&2; exit 1; }
command -v aws    >/dev/null || { echo "ERROR: aws cli not on PATH" >&2; exit 1; }
command -v git    >/dev/null || { echo "ERROR: git not on PATH" >&2; exit 1; }

[[ -f "$BRAINTWIN_SRC_DIR/Dockerfile" ]] || {
  echo "ERROR: no Dockerfile at $BRAINTWIN_SRC_DIR/Dockerfile" >&2
  echo "This script must run from inside the BrainTwin repo." >&2
  exit 1
}

[[ -n "$CDK_DIR" && -d "$CDK_DIR" ]] || {
  echo "ERROR: BrainTwinCDK repo not found." >&2
  echo "Expected at: $BRAINTWIN_SRC_DIR/../BrainTwinCDK" >&2
  echo "Override with CDK_DIR=/path/to/BrainTwinCDK if your layout differs." >&2
  exit 1
}

# Sanity-check the AWS profile resolves before doing anything else —
# otherwise the operator wastes a buildx run waiting for an auth error.
echo "==> Resolving AWS account via profile '$AWS_PROFILE'…"
ACCOUNT_ID="$(aws sts get-caller-identity \
  --profile "$AWS_PROFILE" \
  --query Account \
  --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
echo "    account=$ACCOUNT_ID  registry=$REGISTRY"

# Verify the ECR repo exists before building. describe-images (below)
# can't tell "repo missing" from "tag missing" — both exit non-zero — so
# without this check a not-yet-deployed repo would let the script build
# the whole image and only fail at `buildx --push` with an opaque
# RepositoryNotFoundException, minutes later. The repo is created by
# BrainTwinCDK (storage.ts); run `cdk deploy` first if this fails.
if ! aws ecr describe-repositories \
     --profile "$AWS_PROFILE" \
     --region "$AWS_REGION" \
     --repository-names "$ECR_REPO" \
     >/dev/null 2>&1; then
  echo "ERROR: ECR repo '$ECR_REPO' not found in $AWS_REGION." >&2
  echo "It's created by BrainTwinCDK — run 'cdk deploy' there first." >&2
  exit 1
fi

# ----- Pick a tag ------------------------------------------------------
# Use this repo's git sha — the image's provenance lives here.
cd "$BRAINTWIN_SRC_DIR"
GIT_SHA="$(git rev-parse --short=7 HEAD)"

if [[ -n "$RELEASE_VERSION" ]]; then
  # Release mode — strict guardrails.
  if ! [[ "$RELEASE_VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "ERROR: --release must match v<MAJOR>.<MINOR>.<PATCH> (got: $RELEASE_VERSION)" >&2
    exit 1
  fi
  if [[ -n "$(git status --porcelain)" ]]; then
    echo "ERROR: working tree is dirty — release builds must be reproducible from git." >&2
    echo "Commit or stash, then retry." >&2
    git status --short >&2
    exit 1
  fi
  TAG="$RELEASE_VERSION"
  echo "==> Release build: $TAG  (from $GIT_SHA)"
else
  # Snapshot mode tags on the git sha alone — but the sha doesn't capture
  # uncommitted changes. Without a dirty marker, two different builds from
  # the same commit (with different working-tree edits) would collide on
  # the same immutable tag, and the push would fail with a misleading "no
  # new commits?" message even though you DID change files. Append a
  # short dirty suffix so each uncommitted state gets its own tag and the
  # image's provenance stays honest about not being reproducible from git.
  if [[ -n "$(git status --porcelain)" ]]; then
    TAG="snapshot-$GIT_SHA-dirty-$(date +%Y%m%d%H%M%S)"
    echo "==> Snapshot build (dirty tree): $TAG"
    echo "    NOTE: built from uncommitted changes — not reproducible from git." >&2
  else
    TAG="snapshot-$GIT_SHA"
    echo "==> Snapshot build: $TAG"
  fi
fi

# Refuse to clobber an existing immutable ECR tag — describe-images
# returns success iff the tag exists. We treat both modes the same: if
# the tag is already in ECR, abort and ask the operator to advance.
if aws ecr describe-images \
     --profile "$AWS_PROFILE" \
     --region "$AWS_REGION" \
     --repository-name "$ECR_REPO" \
     --image-ids "imageTag=$TAG" \
     >/dev/null 2>&1; then
  echo "ERROR: tag '$TAG' already exists in $ECR_REPO." >&2
  if [[ -n "$RELEASE_VERSION" ]]; then
    echo "Bump the version (e.g. v0.1.1) and retry." >&2
  else
    echo "No new commits since the last snapshot push?" >&2
    echo "Make a commit (or amend) to advance the sha, then retry." >&2
  fi
  exit 1
fi

FULL_REF="$REGISTRY/$ECR_REPO:$TAG"

# ----- Docker login to ECR --------------------------------------------
# Token is good for 12h; piping into --password-stdin keeps it off argv
# (so `ps` can't see it — same hygiene as scripts/put-secrets.sh in
# the CDK repo).
echo "==> docker login → $REGISTRY"
aws ecr get-login-password \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

# ----- buildx cross-build + push --------------------------------------
# Intel Macs need buildx to produce a linux/arm64 image for Graviton.
# A named builder is faster on subsequent runs (cached layers persist)
# and keeps this script idempotent.
if ! docker buildx inspect braintwin-builder >/dev/null 2>&1; then
  echo "==> Creating buildx builder 'braintwin-builder'…"
  docker buildx create --name braintwin-builder --use
fi
docker buildx use braintwin-builder >/dev/null

echo "==> Building $PLATFORM image → $FULL_REF"
docker buildx build \
  --platform "$PLATFORM" \
  --tag "$FULL_REF" \
  --push \
  --file "$BRAINTWIN_SRC_DIR/Dockerfile" \
  "$BRAINTWIN_SRC_DIR"

# ----- Record the tag for BrainTwinCDK/scripts/deploy.sh --------------
echo "$TAG" > "$TAG_FILE"

cat <<EOF

==> Pushed: $FULL_REF
==> Tag recorded at: $TAG_FILE

Next step (run from BrainTwinCDK):
  cd $CDK_DIR && ./scripts/deploy.sh
EOF
