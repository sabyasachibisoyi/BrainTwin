# syntax=docker/dockerfile:1.6
#
# DigitalTwin / BrainTwin — single multi-stage Dockerfile.
#
# Two stages:
#   builder  — full toolchain, builds the venv at /opt/venv
#   runtime  — slim, copies just the venv + app source, non-root user
#
# Same image runs both services in docker-compose:
#   app  → uvicorn backend.main:app                  (default CMD)
#   bot  → python -m backend.telegram_bot.bot        (override in compose)
#
# Architectures:
#   - Local (Mac):      docker compose build   → builds for host arch
#   - Cloud (EC2 t4g):  docker buildx build --platform linux/arm64 …
#     (cross-build instructions in BrainTwinCDK/docs/m3-aws-deploy.md, post-M.2)
#
# Phase 4.0.6 M.1 — no Caddy, no Litestream, no S3. Just the app + bot
# proven to boot cleanly under compose with the bearer-token auth gate.

# Base image pinned by digest (not just the moving :3.12-slim-bookworm tag)
# so every build — local and cloud — resolves the identical OS layer.
# Re-pin deliberately with:
#   docker buildx imagetools inspect python:3.12-slim-bookworm --format '{{.Manifest.Digest}}'

# -----------------------------------------------------------------------------
# Stage 1: builder — compile / install all Python deps into /opt/venv
# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:07e0e825dd3c0310411f78579bccf9ac8a47d1ec7de2e9b1aca6f0a7c89225d5 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Build deps for torch wheels (some), chromadb (some), readability-lxml,
# selectolax, asyncpg, etc. Slim image is missing all of these by default.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
        libpq-dev \
        libxml2-dev \
        libxslt1-dev \
        libffi-dev \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Isolated venv so the runtime stage can COPY just one directory.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip wheel setuptools \
 && pip install -r /tmp/requirements.txt

# -----------------------------------------------------------------------------
# Stage 1b: whisper-builder — compile whisper.cpp's `whisper-cli` binary.
#
# Phase 2.5 hydration shells out to whisper-cli (see
# backend/capture/video_transcriber.py). Design §3.9 says the binary ships
# IN the image, not on the host — so the same image transcribes in cloud.
# Pinned to a release tag for reproducibility (review fix #3). The ~250 MB
# model itself is NOT baked in; it lives on the data volume (compose sets
# WHISPER_MODEL_PATH=/data/models/...), downloaded once via setup_whisper.sh.
# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:07e0e825dd3c0310411f78579bccf9ac8a47d1ec7de2e9b1aca6f0a7c89225d5 AS whisper-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        build-essential \
        cmake \
    && rm -rf /var/lib/apt/lists/*

# v1.7.4 — whisper.cpp renamed the CLI example to `whisper-cli` in the 1.7.x
# line; we copy whichever name the build produces to a stable path.
RUN git clone --depth 1 --branch v1.7.4 https://github.com/ggml-org/whisper.cpp /tmp/whisper.cpp
WORKDIR /tmp/whisper.cpp
# BUILD_SHARED_LIBS=OFF → static link libwhisper/libggml into the binary,
# so we copy ONE self-contained file into runtime (no libwhisper.so.1 to
# chase). Server/tests off to keep the build lean.
RUN cmake -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF \
        -DWHISPER_BUILD_TESTS=OFF \
        -DWHISPER_BUILD_SERVER=OFF \
 && cmake --build build -j --config Release \
 && mkdir -p /out \
 && (cp build/bin/whisper-cli /out/whisper-cli 2>/dev/null \
     || cp build/bin/main /out/whisper-cli)

# -----------------------------------------------------------------------------
# Stage 2: runtime — slim image with the venv + the app + a non-root user
# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:07e0e825dd3c0310411f78579bccf9ac8a47d1ec7de2e9b1aca6f0a7c89225d5 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Runtime shared-object dependencies for the wheels we built above.
# - libpq5         → asyncpg (cloud Postgres later)
# - libxml2/xslt   → readability-lxml, selectolax
# - libstdc++6     → torch
# - libgomp1       → whisper.cpp (OpenMP runtime)
# - ffmpeg         → audio conversion for whisper (video_transcriber.py)
# - curl           → HEALTHCHECK
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libxml2 \
        libxslt1.1 \
        libstdc++6 \
        libgomp1 \
        ffmpeg \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root. Fixed UID so the /data bind-mount has predictable ownership
# when mapped from the host.
RUN useradd --create-home --uid 10001 braintwin \
 && mkdir -p /data \
 && chown -R braintwin:braintwin /data

COPY --from=builder /opt/venv /opt/venv

# whisper.cpp CLI at the path config.py defaults to (whisper_binary_path).
COPY --from=whisper-builder /out/whisper-cli /usr/local/bin/whisper-cli

WORKDIR /app
COPY --chown=braintwin:braintwin backend /app/backend

USER braintwin

EXPOSE 8000

# Health is the public route — no bearer token needed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# Default command — the FastAPI app. The `bot` service in
# docker-compose.yml overrides this via `command:`.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
