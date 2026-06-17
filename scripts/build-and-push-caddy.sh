#!/usr/bin/env bash
#
# Phase 4.0.6 M.4.a — Build the BrainTwin Caddy image (xcaddy with
# caddy-dns/cloudflare plugin), tag it, push to ECR.
#
# Why a separate script (not folded into build-and-push.sh)
# ---------------------------------------------------------
# Caddy and the app evolve on completely independent cadences:
#
#   - The app changes every time you edit backend/ — multiple times a
#     day during active development.
#   - Caddy changes when you bump the upstream Caddy version (every
#     few months) or when the cloudflare-dns plugin gets a fix.
#
# Folding them into one script would mean you rebuild both every time
# you want to push an app change — wasting buildx time and ECR storage
# on identical Caddy layers. Two scripts, two ECR repos, two tag
# scopes. Their lifecycles don't have to align.
#
# Tag scheme
# ----------
# Pre-launch / iterating (default): caddy-<upstream-version>-snapshot-<git-sha>
#   e.g. caddy-2.7.6-snapshot-abc1234
#
# Release builds: caddy-<upstream-version>-v<MAJOR>.<MINOR>.<PATCH>
#   e.g. caddy-2.7.6-v0.1.0
#   --release v0.1.0 enforces clean tree + semver format.
#
# Both schemes encode the upstream Caddy version so an operator can
# spot a stale Caddy by looking at the running image tag. Bumping the
# Caddy version means editing the ARG in caddy/Dockerfile and rerunning.
#
# Output flow
# -----------
# Writes the chosen tag to ../BrainTwinCDK/.last-deploy-caddy-tag
# (analogous to .last-deploy-tag for the app image). BrainTwinCDK/
# scripts/deploy.sh reads BOTH files and passes the tags to cdk
# deploy via --context appImageTag=… and --context caddyImageTag=….
#
# Usage
# -----
#   ./scripts/build-and-push-caddy.sh                  # snapshot-<git-sha>
#   ./scripts/build-and-push-caddy.sh --release v0.1.0 # tagged release
#
# Environment overrides:
#   AWS_PROFILE     default: braintwin
#   AWS_REGION      default: us-west-2
#   ECR_REPO        default: braintwin/caddy
#   PLATFORM        default: linux/arm64
#   CDK_DIR         default: <this script>/../../BrainTwinCDK

set -euo pipefail

# ----- Config ----------------------------------------------------------
AWS_PROFILE="${AWS_PROFILE:-braintwin}"
AWS_REGION="${AWS_REGION:-us-west-2}"
ECR_REPO="${ECR_REPO:-braintwin/caddy}"
PLATFORM="${PLATFORM:-linux/arm64}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BRAINTWIN_SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CADDY_DIR="$BRAINTWIN_SRC_DIR/caddy"
CDK_DIR="${CDK_DIR:-$(cd "$BRAINTWIN_SRC_DIR/../BrainTwinCDK" 2>/dev/null && pwd)}"
TAG_FILE="$CDK_DIR/.last-deploy-caddy-tag"

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

[[ -f "$CADDY_DIR/Dockerfile" ]] || {
  echo "ERROR: no Dockerfile at $CADDY_DIR/Dockerfile" >&2
  echo "This script must run from inside the BrainTwin repo." >&2
  exit 1
}

[[ -n "$CDK_DIR" && -d "$CDK_DIR" ]] || {
  echo "ERROR: BrainTwinCDK repo not found." >&2
  echo "Expected at: $BRAINTWIN_SRC_DIR/../BrainTwinCDK" >&2
  echo "Override with CDK_DIR=/path/to/BrainTwinCDK if your layout differs." >&2
  exit 1
}

echo "==> Resolving AWS account via profile '$AWS_PROFILE'..."
ACCOUNT_ID="$(aws sts get-caller-identity \
  --profile "$AWS_PROFILE" \
  --query Account \
  --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
echo "    account=$ACCOUNT_ID  registry=$REGISTRY"

# Verify the ECR Caddy repo exists. Created by BrainTwinCDK
# (storage.ts caddyRepo). If this fails, the operator hasn't run cdk
# deploy on the M.4.a branch yet.
if ! aws ecr describe-repositories \
     --profile "$AWS_PROFILE" \
     --region "$AWS_REGION" \
     --repository-names "$ECR_REPO" \
     >/dev/null 2>&1; then
  echo "ERROR: ECR repo '$ECR_REPO' not found in $AWS_REGION." >&2
  echo "Created by BrainTwinCDK storage.ts (M.4.a) - run 'cdk deploy' there first." >&2
  exit 1
fi

# ----- Read Caddy upstream version from the Dockerfile -----------------
# Single source of truth: the ARG CADDY_VERSION=… line. Bumping the
# Caddy version is a one-line edit; the tag follows.
CADDY_VERSION="$(awk -F= '/^ARG CADDY_VERSION=/{print $2; exit}' "$CADDY_DIR/Dockerfile")"
if [[ -z "$CADDY_VERSION" ]]; then
  echo "ERROR: could not parse CADDY_VERSION from $CADDY_DIR/Dockerfile" >&2
  exit 1
fi
echo "    caddy_version=$CADDY_VERSION (from Dockerfile)"

# ----- Pick a tag ------------------------------------------------------
# Provenance is this repo's git sha (BrainTwin owns the Dockerfile),
# not the Caddy upstream commit. Bump Caddy version → bump the
# Dockerfile ARG → new sha → new immutable tag. No collisions.
cd "$BRAINTWIN_SRC_DIR"
GIT_SHA="$(git rev-parse --short=7 HEAD)"

if [[ -n "$RELEASE_VERSION" ]]; then
  if ! [[ "$RELEASE_VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "ERROR: --release must match v<MAJOR>.<MINOR>.<PATCH> (got: $RELEASE_VERSION)" >&2
    exit 1
  fi
  if [[ -n "$(git status --porcelain)" ]]; then
    echo "ERROR: working tree is dirty - release builds must be reproducible from git." >&2
    git status --short >&2
    exit 1
  fi
  TAG="caddy-${CADDY_VERSION}-${RELEASE_VERSION}"
  echo "==> Release build: $TAG  (from $GIT_SHA)"
else
  if [[ -n "$(git status --porcelain)" ]]; then
    TAG="caddy-${CADDY_VERSION}-snapshot-${GIT_SHA}-dirty-$(date +%Y%m%d%H%M%S)"
    echo "==> Snapshot build (dirty tree): $TAG"
    echo "    NOTE: built from uncommitted changes - not reproducible from git." >&2
  else
    TAG="caddy-${CADDY_VERSION}-snapshot-${GIT_SHA}"
    echo "==> Snapshot build: $TAG"
  fi
fi

# Refuse to clobber an existing immutable ECR tag.
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
    echo "No new commits since the last snapshot push? Make a commit to advance the sha." >&2
  fi
  exit 1
fi

FULL_REF="$REGISTRY/$ECR_REPO:$TAG"

# ----- Docker login to ECR --------------------------------------------
echo "==> docker login -> $REGISTRY"
aws ecr get-login-password \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

# ----- buildx cross-build + push --------------------------------------
# Reuse the same named builder as build-and-push.sh — there's no
# benefit to a separate builder, layer caches don't overlap anyway.
if ! docker buildx inspect braintwin-builder >/dev/null 2>&1; then
  echo "==> Creating buildx builder 'braintwin-builder'..."
  docker buildx create --name braintwin-builder --use
fi
docker buildx use braintwin-builder >/dev/null

echo "==> Building $PLATFORM image -> $FULL_REF"
docker buildx build \
  --platform "$PLATFORM" \
  --tag "$FULL_REF" \
  --push \
  --file "$CADDY_DIR/Dockerfile" \
  "$CADDY_DIR"

# ----- Record the tag for deploy.sh -----------------------------------
echo "$TAG" > "$TAG_FILE"

cat <<EOF

==> Pushed: $FULL_REF
==> Tag recorded at: $TAG_FILE

Next step (from BrainTwinCDK):
  cd $CDK_DIR && ./scripts/deploy.sh
EOF
