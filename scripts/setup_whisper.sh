#!/usr/bin/env bash
# BrainTwin Phase 2.5 Fix 3 — one-time Whisper setup.
#
# Installs whisper.cpp via Homebrew (gives you `whisper-cli` on PATH)
# and downloads the small.en model into data/models/. Idempotent —
# safe to re-run; skips work that's already done.
#
# Usage (from repo root):
#   bash scripts/setup_whisper.sh
#
# After this completes, restart the backend so it picks up the new
# binary + model. Verify with:
#   curl http://127.0.0.1:8000/stats   # should still be reachable
# and forward an IG/FB reel — `data/hydrations.jsonl` should gain a
# row with tier="video_transcript".

set -euo pipefail

# ---- Locate repo root regardless of where the script is invoked from
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
MODELS_DIR="${REPO_ROOT}/data/models"
MODEL_NAME="ggml-small.en.bin"
MODEL_PATH="${MODELS_DIR}/${MODEL_NAME}"
# Hugging Face mirror — official whisper.cpp model distribution
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/${MODEL_NAME}"

say() { printf "\033[1;36m==>\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m %s\n" "$*" >&2; }
fail() { printf "\033[1;31m[fail]\033[0m %s\n" "$*" >&2; exit 1; }

# ---- 1. Homebrew + whisper-cpp ---------------------------------------

if ! command -v brew >/dev/null 2>&1; then
  fail "Homebrew not found. Install it first from https://brew.sh, then re-run."
fi

if command -v whisper-cli >/dev/null 2>&1; then
  say "whisper-cli already on PATH at $(command -v whisper-cli) — skipping install."
else
  say "Installing whisper-cpp via Homebrew (this may take a few minutes)..."
  brew install whisper-cpp
fi

# whisper.cpp itself can't decode m4a/webm/opus — yt-dlp gives us those
# formats, so ffmpeg is mandatory as a pre-conversion step. Some
# whisper-cpp Homebrew versions don't pull ffmpeg in as a hard dep.
if command -v ffmpeg >/dev/null 2>&1; then
  say "ffmpeg already on PATH at $(command -v ffmpeg) — skipping install."
else
  say "Installing ffmpeg (whisper needs it to convert m4a → wav)..."
  brew install ffmpeg
fi

# Where brew put it. Confirm the binary is reachable so the backend's
# config default (/opt/homebrew/bin/whisper-cli) actually works.
WHISPER_BIN="$(command -v whisper-cli || true)"
if [[ -z "${WHISPER_BIN}" ]]; then
  fail "whisper-cli still not on PATH after install. Check 'brew doctor'."
fi
say "whisper-cli ready at ${WHISPER_BIN}"
if [[ "${WHISPER_BIN}" != "/opt/homebrew/bin/whisper-cli" ]]; then
  warn "Note: backend default expects /opt/homebrew/bin/whisper-cli."
  warn "If your binary lives at ${WHISPER_BIN}, set WHISPER_BINARY_PATH in .env."
fi

# ---- 2. Download the model ------------------------------------------

mkdir -p "${MODELS_DIR}"

if [[ -f "${MODEL_PATH}" ]]; then
  size_mb=$(( $(stat -f%z "${MODEL_PATH}" 2>/dev/null || stat -c%s "${MODEL_PATH}") / 1024 / 1024 ))
  if (( size_mb > 200 )); then
    say "${MODEL_NAME} already present (${size_mb} MB) — skipping download."
  else
    warn "${MODEL_NAME} present but only ${size_mb} MB — re-downloading."
    rm -f "${MODEL_PATH}"
  fi
fi

if [[ ! -f "${MODEL_PATH}" ]]; then
  say "Downloading ${MODEL_NAME} (~244 MB) into data/models/..."
  if command -v curl >/dev/null 2>&1; then
    curl -L --fail --progress-bar -o "${MODEL_PATH}" "${MODEL_URL}"
  elif command -v wget >/dev/null 2>&1; then
    wget --show-progress -O "${MODEL_PATH}" "${MODEL_URL}"
  else
    fail "Neither curl nor wget available — install one and re-run."
  fi
fi

# ---- 3. Sanity-check whisper actually runs ---------------------------

say "Sanity-checking whisper-cli with --help..."
"${WHISPER_BIN}" --help >/dev/null 2>&1 || warn "whisper-cli --help returned non-zero. Check the install."

# ---- 4. Confirm yt-dlp ---------------------------------------------

if python3 -c "import yt_dlp" 2>/dev/null; then
  say "yt-dlp Python lib already installed."
else
  warn "yt-dlp not importable. Run: pip install -r requirements.txt (or pip install yt-dlp)."
fi

# ---- Done ----------------------------------------------------------

say "Whisper setup complete."
echo
echo "Binary : ${WHISPER_BIN}"
echo "Model  : ${MODEL_PATH}"
echo
echo "Next: restart the backend (uvicorn) and forward an IG/FB reel from your phone."
echo "Watch:  tail -f data/hydrations.jsonl"
echo "Expect: a row with tier=\"video_transcript\" and a non-zero clean_text_after_chars."
